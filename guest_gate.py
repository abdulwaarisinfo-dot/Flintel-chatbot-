"""
guest_gate.py — server-side limit on GENUINE searches for visitors who are not signed in.

A guest may complete GUEST_SEARCH_LIMIT real searches (default 1). The count lives in Mongo, keyed by
the guest's persistent identity (the owner_key that index.get_anon_id() resolves from the signed
`fl_gid` cookie), so refreshing, opening another tab or restarting the browser cannot reset it. The
browser never supplies or edits any of this state; JavaScript only reacts to the server's answers.

One document per guest in `flintel_guest_gate` (_id = owner_key):

    completed        number of genuine searches whose answer was actually saved
    inflight         the one reservation currently being worked on, or null:
                     {token, topic_key, chat_id, started_at}
    tokens           the last reservation tokens (a completion is only counted for a token that was
                     really reserved — an old message that predates the gate can never be counted)
    completed_tokens the last tokens already counted (a repeated completion counts once)
    last_chat_id     the conversation of the latest completed search (used to resume it after sign-in)

Lifecycle (every transition is a single atomic Mongo update):

    reserve   right before a genuine search is accepted (before its credit reservation / job enqueue).
              Succeeds only while completed < limit AND no other live reservation exists. Two tabs or a
              double click: exactly one wins. The same token again is a resume, not a second search.
              A reservation older than the stale limit may be taken over (the search is presumed dead).
    complete  after the real answer was saved by a search that did NOT time out. Counts once per token, only
              while completed < limit, and only if the token still OWNS the in-flight slot (or the slot is empty,
              e.g. an automatic retry of a search whose first attempt was released). All of that is one atomic
              update. A search that finishes after a stale takeover finds the slot held by a newer token: it is
              answered "superseded", counts nothing and cannot touch the newer reservation. Likewise a late
              release only ever matches its own token.
    release   when the search ended without a usable answer (error, scan timeout, timeout fallback, the
              credit reservation refused, an exception before the job was queued). Only the token that
              owns the reservation can release it, so a late release from an old search cannot free a
              newer one. Nothing is counted.
    stale     an unfinished reservation older than GUEST_INFLIGHT_STALE_SECONDS stops blocking.
    sign-in   a guest who signs in while a search is in flight keeps that reservation: the chat message is tagged
              with the guest id (guest_gate_owner), so the search finishes under the account but its outcome is
              still reported to the SAME reservation — completed = counted against the guest identity, failed =
              released by its own token. The account is never charged and the search is not re-run.
    work      every path that produces an answer asks the gate FIRST (on_work_start: reserve/resume with the search's
    start     own token — a background retry cannot start a second independent search while another one is admitted,
              and a superseded retry is abandoned without any model call) and again right before the answer is saved
              or streamed (on_answer_ready: counting is the commit, so a superseded worker's answer is discarded,
              never saved and never shown). A per-search work lease (acquire_work / end_work) lets only ONE runner generate
              a search's answer at a time: a second tab, a retry or the worker waits for / replays the stored answer
              instead of making a second model call. The lease is given back in a finally and expires on its own.
              The lease is renewed while the runner waits and right before each model call (renew_work: only the holder
              can); complete() takes the holder's run id so the lease check and the count are ONE atomic write (a runner
              whose lease was taken over cannot count), and the answer itself is stored with a conditional update
              (index.save_answer_for_gate: first writer wins, and for guests it is stored before any chunk is streamed).
              This fences stale counting, storing and delivery; it does not make model execution exactly-once.

A client disconnect does NOT release a reservation: the search may still finish (a refresh resumes the
same stream). There is deliberately no cap on failed attempts.

Fail-closed: if the store cannot be read or written, reserve() answers "unavailable" (the route shows a
temporary error, never the sign-up popup). The hooks that run inside the search pipeline never raise.

Known limitation: the identity is a first-party cookie. Clearing cookies, another browser or incognito
mode produce a new identity (and one more guest search). No IP address or fingerprint is stored or used.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

log = logging.getLogger("flintel.guest_gate")

ALLOW, AUTH_REQUIRED, BUSY, UNAVAILABLE, IDENTITY = "allow", "auth_required", "busy", "unavailable", "identity_required"
TOKEN_HISTORY = 20


class Decision:
    """kind: allow | auth_required | busy | unavailable | identity_required. `reserved` is True when the caller now holds the
    reservation; `resumed` is True when that reservation already existed under the same token (a retry of a
    request that is still being worked on) — the retry must not be able to release it."""
    __slots__ = ("kind", "chat_id", "reserved", "resumed")

    def __init__(self, kind, chat_id=None, reserved=False, resumed=False):
        self.kind, self.chat_id, self.reserved, self.resumed = kind, chat_id, reserved, resumed

    def __repr__(self):
        return f"Decision({self.kind!r}, chat_id={self.chat_id!r}, reserved={self.reserved}, resumed={self.resumed})"


def is_guest_owner(owner_key) -> bool:
    """Guests are keyed by their random id; every registered account is keyed by its e-mail address."""
    return isinstance(owner_key, str) and bool(owner_key) and "@" not in owner_key


def _cfg():
    import config
    return config


def configured_limit() -> int:
    try:
        return max(0, int(getattr(_cfg(), "GUEST_SEARCH_LIMIT", 1)))
    except (TypeError, ValueError):
        return 1                                   # an unusable value never switches the gate off


def configured_work_lease_seconds() -> int:
    """How long a runner that is generating one search's answer excludes a second runner (crash recovery bound)."""
    try:
        rt = int(getattr(_cfg(), "RESPONSE_TIMEOUT", 30))
    except (TypeError, ValueError):
        rt = 30
    return max(30, min(max(120, rt + 90), configured_stale_seconds() - 30))


def configured_stale_seconds() -> int:
    try:
        return max(60, int(getattr(_cfg(), "GUEST_INFLIGHT_STALE_SECONDS", 900)))
    except (TypeError, ValueError):
        return 900


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _is_duplicate_key(exc) -> bool:
    return type(exc).__name__ == "DuplicateKeyError"


class GuestGate:
    def __init__(self, coll, clock=None, limit=None, stale_seconds=None, work_lease_seconds=None):
        self.coll = coll
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self._limit, self._stale, self._lease = limit, stale_seconds, work_lease_seconds

    @property
    def limit(self) -> int:
        return self._limit if self._limit is not None else configured_limit()

    @property
    def work_lease_seconds(self) -> int:
        return self._lease if self._lease is not None else configured_work_lease_seconds()

    @property
    def stale_seconds(self) -> int:
        return self._stale if self._stale is not None else configured_stale_seconds()

    # ── reads ────────────────────────────────────────────────────────────────
    def _live_inflight(self, doc):
        inflight = (doc or {}).get("inflight")
        if not isinstance(inflight, dict):
            return None
        started = _aware(inflight.get("started_at"))
        if started is None or (self.now() - started).total_seconds() > self.stale_seconds:
            return None
        return inflight

    def state(self, owner_key) -> dict:
        """{"limit", "completed", "used", "inflight", "last_chat_id"}. Raises on a store error."""
        doc = self.coll.find_one({"_id": owner_key}) or {}
        completed = int(doc.get("completed") or 0)
        limit = self.limit
        return {"limit": limit, "completed": completed, "used": limit > 0 and completed >= limit,
                "inflight": self._live_inflight(doc) is not None, "last_chat_id": doc.get("last_chat_id")}

    def peek(self, owner_key) -> Decision:
        """Read-only early answer (no write). The atomic reserve() stays the only authority."""
        if self.limit <= 0:
            return Decision(ALLOW)
        try:
            state = self.state(owner_key)
        except Exception as exc:
            log.warning(f"guest gate read failed: {type(exc).__name__}")
            return Decision(UNAVAILABLE)
        if state["used"]:
            return Decision(AUTH_REQUIRED, state["last_chat_id"])
        if state["inflight"]:
            return Decision(BUSY)
        return Decision(ALLOW)

    # ── transitions ──────────────────────────────────────────────────────────
    def reserve(self, owner_key, token, topic_key=None, chat_id=None) -> Decision:
        limit = self.limit
        if limit <= 0:
            return Decision(ALLOW)
        now = self.now()
        cutoff = now - timedelta(seconds=self.stale_seconds)
        inflight = {"token": token, "topic_key": topic_key, "chat_id": chat_id, "started_at": now}
        push = {"tokens": {"$each": [token], "$slice": -TOKEN_HISTORY}}
        try:
            try:
                self.coll.insert_one({"_id": owner_key, "completed": 0, "completed_tokens": [], "tokens": [token],
                                      "inflight": inflight, "created_at": now, "updated_at": now})
                return Decision(ALLOW, reserved=True)
            except Exception as exc:
                if not _is_duplicate_key(exc):
                    raise
            before = self.coll.find_one_and_update(
                {"_id": owner_key, "completed": {"$lt": limit}, "completed_tokens": {"$ne": token},
                 "$or": [{"inflight": None}, {"inflight.token": token}, {"inflight.started_at": {"$lte": cutoff}}]},
                {"$set": {"inflight": inflight, "updated_at": now}, "$push": push},
                return_document=ReturnDocument.BEFORE)
            if before is not None:
                prior = before.get("inflight")
                resumed = isinstance(prior, dict) and prior.get("token") == token
                return Decision(ALLOW, reserved=True, resumed=resumed)
            doc = self.coll.find_one({"_id": owner_key}) or {}
            if token in (doc.get("completed_tokens") or []):
                return Decision(ALLOW, doc.get("last_chat_id"), resumed=True)    # already counted: never takes the slot again
            if int(doc.get("completed") or 0) >= limit:
                return Decision(AUTH_REQUIRED, doc.get("last_chat_id"))
            return Decision(BUSY, (doc.get("inflight") or {}).get("chat_id"))
        except Exception as exc:
            log.warning(f"guest gate reserve failed: {type(exc).__name__}")
            return Decision(UNAVAILABLE)

    def bind_chat(self, owner_key, token, chat_id) -> bool:
        """Records the conversation of an existing reservation (paths that learn the chat after the reserve)."""
        try:
            result = self.coll.update_one({"_id": owner_key, "inflight.token": token}, {"$set": {"inflight.chat_id": chat_id}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate bind failed: {type(exc).__name__}")
            return False

    def complete(self, owner_key, token, chat_id=None, topic_key=None, run=None) -> str:
        """Counts one finished genuine search. Returns
          "completed"    counted (exactly once per token, and never beyond the limit),
          "duplicate"    this token was already counted, or was never reserved,
          "superseded"   a newer reservation owns the slot, or the allowance is already used (a late finisher),
          "fenced"       another runner holds an unexpired work lease for this search (only with `run`),
          "unavailable"  the store could not be written.
        Counting and freeing the slot are ONE atomic update: when the token owns the slot the same write clears it;
        when the slot is already empty nothing needs clearing. There is no state in which a crash between two writes
        leaves a counted search holding the slot, or a freed slot with an uncounted search.
        With `run` (the work-lease id of the caller) the same update also requires that no OTHER runner holds an
        unexpired lease: a runner whose lease was taken over cannot count - the check and the count are one write."""
        now = self.now()
        base = {"_id": owner_key, "tokens": token, "completed_tokens": {"$ne": token}, "completed": {"$lt": self.limit}}
        if run:
            base["$and"] = [{"$or": [{"work_lease": None}, {"work_lease.run": run}, {"work_lease.until": {"$lte": now}}]}]
        fields = {"last_chat_id": chat_id, "last_topic_key": topic_key, "last_completed_at": now, "updated_at": now}
        push = {"completed_tokens": {"$each": [token], "$slice": -TOKEN_HISTORY}}
        try:
            result = self.coll.update_one({**base, "inflight.token": token},
                                          {"$inc": {"completed": 1}, "$push": push, "$set": {**fields, "inflight": None}})
            if getattr(result, "matched_count", 0) != 1:
                result = self.coll.update_one({**base, "inflight": None},
                                              {"$inc": {"completed": 1}, "$push": push, "$set": fields})
            if getattr(result, "matched_count", 0) == 1:
                return "completed"
            doc = self.coll.find_one({"_id": owner_key}) or {}
            if token in (doc.get("completed_tokens") or []) or token not in (doc.get("tokens") or []):
                return "duplicate"
            lease = doc.get("work_lease") or {}
            until = _aware(lease.get("until"))
            if run and lease.get("run") not in (None, run) and until is not None and until > now:
                return "fenced"
            return "superseded"
        except Exception as exc:
            log.warning(f"guest gate complete failed: {type(exc).__name__}")
            return "unavailable"

    def revert(self, owner_key, token) -> bool:
        """Undoes the count of `token` when its answer could not be stored (nothing was delivered). Only that token's
        own count is removed; the slot stays free for a retry."""
        try:
            result = self.coll.update_one({"_id": owner_key, "completed_tokens": token, "completed": {"$gt": 0}},
                                          {"$inc": {"completed": -1}, "$pull": {"completed_tokens": token},
                                           "$set": {"updated_at": self.now()}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate revert failed: {type(exc).__name__}")
            return False

    def is_counted(self, owner_key, token) -> bool:
        doc = self.coll.find_one({"_id": owner_key}) or {}
        return token in (doc.get("completed_tokens") or [])

    def holds(self, owner_key, token, run=None) -> bool:
        """True while `token` still owns the in-flight slot and (with `run`) no other runner holds an unexpired lease.
        A read: the compare-and-set answer save stays the atomic authority for what gets stored."""
        doc = self.coll.find_one({"_id": owner_key}) or {}
        if not (isinstance(doc.get("inflight"), dict) and doc["inflight"].get("token") == token):
            return False
        lease = doc.get("work_lease") or {}
        until = _aware(lease.get("until"))
        return not (run and lease.get("run") not in (None, run) and until is not None and until > self.now())

    def acquire_work(self, owner_key, token):
        """The right to GENERATE the answer of one search. One runner at a time per search: a second tab, a retry or a
        background worker that finds the lease held gets None and must wait for / replay the first runner's stored
        answer instead of making a second model call. Applies to the search that owns the slot, and to an already
        counted search whose answer still has to be stored. Returns the runner's id, or None. The lease expires by
        itself, so a crashed runner never blocks a search for longer than work_lease_seconds."""
        now = self.now()
        run = uuid.uuid4().hex
        flt = {"_id": owner_key,
               "$and": [{"$or": [{"inflight.token": token}, {"completed_tokens": token}]},
                        {"$or": [{"work_lease": None}, {"work_lease.token": {"$ne": token}},
                                 {"work_lease.until": {"$lte": now}}]}]}
        result = self.coll.update_one(flt, {"$set": {"work_lease": {"token": token, "run": run,
                                                                  "until": now + timedelta(seconds=self.work_lease_seconds)}}})
        return run if getattr(result, "matched_count", 0) == 1 else None

    def renew_work(self, owner_key, token, run) -> bool:
        """Extends the work lease of a long-running runner. Only the runner that holds the lease (same run id) can: a
        runner that was taken over finds another run id and gets False. A lease that merely expired, with nobody having
        taken it over, can still be renewed. While the search owns the slot the same write also refreshes the
        reservation's started_at, so a live search is not taken over as stale."""
        now = self.now()
        until = now + timedelta(seconds=self.work_lease_seconds)
        held = {"_id": owner_key, "work_lease.token": token, "work_lease.run": run}
        try:
            result = self.coll.update_one({**held, "inflight.token": token},
                                          {"$set": {"work_lease.until": until, "inflight.started_at": now, "updated_at": now}})
            if getattr(result, "matched_count", 0) != 1:
                result = self.coll.update_one({**held, "completed_tokens": token},
                                              {"$set": {"work_lease.until": until, "updated_at": now}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate renew-work failed: {type(exc).__name__}")
            raise

    def end_work(self, owner_key, token, run) -> bool:
        """Gives the lease back - only the runner that holds it (same id) can."""
        try:
            result = self.coll.update_one({"_id": owner_key, "work_lease.token": token, "work_lease.run": run},
                                          {"$unset": {"work_lease": ""}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate end-work failed: {type(exc).__name__}")
            return False

    def live_search(self, owner_key):
        """The live reservation {token, topic_key, chat_id} or None (read-only)."""
        inflight = self._live_inflight(self.coll.find_one({"_id": owner_key}) or {})
        return dict(inflight) if inflight else None

    def release(self, owner_key, token) -> bool:
        """Frees the reservation owned by `token` (nothing is counted). A different token never releases it."""
        try:
            result = self.coll.update_one({"_id": owner_key, "inflight.token": token},
                                          {"$set": {"inflight": None, "updated_at": self.now()}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate release failed: {type(exc).__name__}")
            return False


# ── process-wide access + the hooks used inside the search pipeline ─────────────────────
def default_gate():
    """The configured gate, or None when GUEST_SEARCH_LIMIT is 0 (gate off)."""
    if configured_limit() <= 0:
        return None
    import database
    return GuestGate(database.guest_gate_collection)


def answer_counts(answer) -> bool:
    """True when `answer` (the saved answer, JSON text or dict) is a finished answer. A genuine "nothing found"
    answer counts. An answer the pipeline itself marks "incomplete" (no usable text) or "partial" (cut off) does not."""
    if answer is None:
        return True                                  # the caller did not pass the answer: judged elsewhere
    data = answer
    if isinstance(answer, (str, bytes)):
        text = answer.decode("utf-8", "replace") if isinstance(answer, bytes) else answer
        if not text.strip():
            return False
        try:
            data = json.loads(text)
        except ValueError:
            return True                              # plain prose is a finished answer
    if isinstance(data, dict):
        return data.get("answer_status") not in ("incomplete", "partial", "limited")
    return bool(data)


def mark_limited(owner_key, token, answer):
    """A guest's timeout / scan-deadline answer is built from the evidence found so far. Stamp it
    answer_status "limited" so the page can say it is a limited result that did not use up the free search. Only for
    gated guest searches (everyone else's answers are returned unchanged); an answer that already carries an
    incomplete / partial status keeps it. Never raises."""
    try:
        if not token or not is_guest_owner(owner_key) or default_gate() is None:
            return answer
        data = json.loads(answer) if isinstance(answer, str) else None
        if isinstance(data, dict) and data.get("answer_status") not in ("incomplete", "partial"):
            data["answer_status"] = "limited"
            return json.dumps(data, ensure_ascii=False)
    except Exception:
        pass
    return answer


def on_answer_complete(owner_key, chat_id, topic_key, token, answer=None):
    """Hook (legacy form, kept for direct use): count a finished search. Never raises."""
    if not token or not is_guest_owner(owner_key):
        return None
    try:
        gate = default_gate()
        if gate is None:
            return None
        if not answer_counts(answer):
            return gate.release(owner_key, token)
        return gate.complete(owner_key, token, chat_id, topic_key)
    except Exception as exc:
        log.warning(f"guest gate completion hook failed: {type(exc).__name__}")
        return None


PROCEED, ABANDON, DEFER, WAIT = "proceed", "abandon", "defer", "wait"
DELIVER, DISCARD = "deliver", "discard"


def gate_owner(owner_key, msg=None):
    """The identity the gate knows this search by: the guest id stamped on the message when the guest signed in
    while it was running, otherwise the chat owner."""
    stamped = msg.get("guest_gate_owner") if isinstance(msg, dict) else None
    return stamped if isinstance(stamped, str) and stamped else owner_key


def superseded_answer() -> str:
    """Saved INSTEAD of an answer for a search that no longer owns its reservation. Same explicit-incomplete
    envelope the page already renders ("answer unavailable"); it carries no research content."""
    return json.dumps({
        "format": "no_results",
        "answer_status": "incomplete",
        "searched": {"query": None, "platforms": [], "time_window": None},
        "message": "This search was replaced by a newer one, so no answer was produced for it.",
        "likely_reason": "Only one search can run at a time for a visitor who is not signed in.",
        "suggested_actions": [],
    }, ensure_ascii=False)


def on_work_start(owner_key, token, topic_key=None, chat_id=None, lease=None):
    """Hook: called BEFORE any model call for a search's answer (first run, background retry, stream, fallback).
      PROCEED  this token owns (or has just re-taken) the reservation, or it was already counted (re-delivery), AND
               this runner now holds the work lease (its id is stored in `lease["run"]`; give it back with
               on_work_end in a finally);
      WAIT     another runner is already generating this very search: make no model call, wait for / replay its answer;
      ABANDON  another search is admitted, or the allowance is used: produce nothing for this one;
      DEFER    the gate store is unavailable: do nothing now, leave the turn pending for a later retry.
    Registered owners, gate off or no token: PROCEED. Never raises."""
    if not token or not is_guest_owner(owner_key):
        return PROCEED
    try:
        gate = default_gate()
        if gate is None:
            return PROCEED
        if not gate.is_counted(owner_key, token):          # counted but never stored: finish delivering it
            decision = gate.reserve(owner_key, token, topic_key, chat_id)
            if decision.kind != ALLOW:
                return DEFER if decision.kind == UNAVAILABLE else ABANDON
        run = gate.acquire_work(owner_key, token)
    except Exception as exc:
        log.warning(f"guest gate work-start hook failed: {type(exc).__name__}")
        return DEFER
    if run is None:
        return WAIT
    if isinstance(lease, dict):
        lease["run"] = run
        lease["renewed_at"] = time.monotonic()
    return PROCEED


def on_work_renew(owner_key, token, lease, force=False):
    """Hook: a runner that got PROCEED calls this while it works (and right before each model call, with force=True).
    Returns False only when the gate says the lease is no longer this runner's (another runner took it over): the
    caller must stop - it must not start the model call and must not save or show anything. Calls inside the same
    third of the lease are skipped (cheap); a store error returns True (the commit stays the authority). Registered
    owners, gate off, or no lease: True. Never raises."""
    if not token or not is_guest_owner(owner_key) or not isinstance(lease, dict) or not lease.get("run"):
        return True
    try:
        gate = default_gate()
        if gate is None:
            return True
        now = time.monotonic()
        if not force and now - lease.get("renewed_at", 0) < gate.work_lease_seconds / 3:
            return True
        ok = gate.renew_work(owner_key, token, lease["run"])
        if ok:
            lease["renewed_at"] = now
        return ok
    except Exception as exc:
        log.warning(f"guest gate work-renew hook failed: {type(exc).__name__}")
        return True


def on_work_end(owner_key, token, lease):
    """Hook: the runner that got PROCEED is done (success, failure or disconnect). Never raises."""
    if not token or not is_guest_owner(owner_key) or not isinstance(lease, dict) or not lease.get("run"):
        return None
    try:
        gate = default_gate()
        return gate.end_work(owner_key, token, lease["run"]) if gate is not None else None
    except Exception as exc:
        log.warning(f"guest gate work-end hook failed: {type(exc).__name__}")
        return None


def on_answer_ready(owner_key, chat_id, topic_key, token, answer, counted=True, lease=None):
    """Hook: an answer exists and is about to be SAVED / SHOWN. Counting is the commit, so only the reservation's
    owner is told to deliver:
      DELIVER  counted (or already counted, or an uncounted answer by the current owner) - go ahead;
      DISCARD  a newer reservation owns the slot / the allowance is used - save the superseded notice instead;
      WAIT     another runner holds the work lease of this very search (this runner was taken over): save and show
               nothing of its own - the caller waits for / replays the stored winner;
      DEFER    the gate store could not be written - deliver nothing now, leave the turn pending.
    `lease` is the dict handed to on_work_start: its run id makes the count conditional on lease ownership, in the same
    atomic write. Delivery additionally requires winning the compare-and-set answer save (index.save_answer_for_gate).
    counted=False (timeout / scan-deadline answers) checks ownership but never counts. An answer the pipeline marks
    incomplete / partial is delivered as the explicit notice it is and releases the reservation. Never raises."""
    if not token or not is_guest_owner(owner_key):
        return DELIVER
    try:
        gate = default_gate()
        if gate is None:
            return DELIVER
        run = lease.get("run") if isinstance(lease, dict) else None
        if not counted:
            if gate.holds(owner_key, token, run):
                return DELIVER
            return WAIT if gate.holds(owner_key, token) else DISCARD
        if not answer_counts(answer):
            gate.release(owner_key, token)
            return DELIVER
        outcome = gate.complete(owner_key, token, chat_id, topic_key, run)
    except Exception as exc:
        log.warning(f"guest gate answer-ready hook failed: {type(exc).__name__}")
        return DEFER
    if outcome in ("completed", "duplicate"):
        return DELIVER
    if outcome == "fenced":
        return WAIT
    return DISCARD if outcome == "superseded" else DEFER


def on_delivery_failed(owner_key, token):
    """Hook: a counted answer could not be stored and was not shown - take the count back. Never raises."""
    if not token or not is_guest_owner(owner_key):
        return None
    try:
        gate = default_gate()
        return gate.revert(owner_key, token) if gate is not None else None
    except Exception as exc:
        log.warning(f"guest gate revert hook failed: {type(exc).__name__}")
        return None


def on_search_incomplete(owner_key, token):
    """Hook: the search ended without a usable answer (error / timeout). Never raises."""
    if not token or not is_guest_owner(owner_key):
        return None
    try:
        gate = default_gate()
        return gate.release(owner_key, token) if gate is not None else None
    except Exception as exc:
        log.warning(f"guest gate release hook failed: {type(exc).__name__}")
        return None


def on_guest_authenticated(guest_id):
    """Hook: a visitor just signed in / signed up. Returns the live search {token, topic_key, chat_id} that must now
    be stamped with this guest id (so it keeps reporting to its reservation), or None. Read-only: nothing is counted
    or released here. Never raises."""
    if not is_guest_owner(guest_id):
        return None
    try:
        gate = default_gate()
        return gate.live_search(guest_id) if gate is not None else None
    except Exception as exc:
        log.warning(f"guest gate sign-in hook failed: {type(exc).__name__}")
        return None


def auth_required_for(owner_key, chat_id=None):
    """"auth_required" when this guest has used the allowance (and, if given, `chat_id` is the conversation
    of the completed search); otherwise None. Read-only; any error means None (never a false popup)."""
    if not is_guest_owner(owner_key):
        return None
    try:
        gate = default_gate()
        if gate is None:
            return None
        state = gate.state(owner_key)
    except Exception as exc:
        log.warning(f"guest gate state read failed: {type(exc).__name__}")
        return None
    if not state["used"]:
        return None
    if chat_id is not None and state["last_chat_id"] != chat_id:
        return None
    return "auth_required"


def resume_chat_id(owner_key):
    """The conversation to reopen after this guest signs in: the one of their completed search (allowance used) or of
    the search that is still running. Server-held — never taken from the client. None if unavailable."""
    if not is_guest_owner(owner_key):
        return None
    try:
        gate = default_gate()
        if gate is None:
            return None
        state = gate.state(owner_key)
    except Exception as exc:
        log.warning(f"guest gate resume read failed: {type(exc).__name__}")
        return None
    if state["used"]:
        return state["last_chat_id"]
    live = gate.live_search(owner_key)           # signing in while the one search is still running: that conversation
    return live.get("chat_id") if live else None
