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

CREDIT MODE (GUEST_CREDIT_ALLOWANCE, default 5): the allowance is measured in CREDITS, not searches. Each completed genuine
answer adds the cost of that search (credit_cost's calculation, read from the credit reservation: bound with bind_cost) to
`credits_used` in the same atomic write that counts it. A guest may start a search while credits_used < allowance; the
search in progress is always allowed to finish, so the total may overshoot the allowance - after that the next search needs
an account. gate_mode() documents how GUEST_SEARCH_LIMIT still works (the legacy "N searches" rule when it is set explicitly
and no credit allowance is).
HANDOVER: when a guest creates a NEW account, claim_handover() (one compare-and-set on this document) records what moves to
the account - the credits already used plus, for a search still running, a HOLD of its cost - and the credit store charges
it once to the account's first free window (search_credits.CreditStore.seed_guest_handover). The hold is committed when the
search is counted and released when it ends uncounted; which of the two happens is decided on THIS document (a token is either
in completed_tokens or handover.released_token), so a committed hold can never be released and a released one never counted.

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


def gate_mode():
    """("credits", allowance) | ("searches", n). Precedence: GUEST_CREDIT_ALLOWANCE (when set; 0 = off) wins; otherwise an
    explicitly set GUEST_SEARCH_LIMIT keeps its legacy meaning (N completed searches; 0 = off); otherwise 5 credits."""
    cfg = _cfg()
    allowance = getattr(cfg, "GUEST_CREDIT_ALLOWANCE", None)
    if isinstance(allowance, int) and not isinstance(allowance, bool) and allowance >= 0:
        return "credits", allowance
    if getattr(cfg, "GUEST_SEARCH_LIMIT_EXPLICIT", False):
        try:
            return "searches", max(0, int(getattr(cfg, "GUEST_SEARCH_LIMIT", 1)))
        except (TypeError, ValueError):
            return "searches", 1                  # an unusable value never switches the gate off
    return "credits", int(getattr(cfg, "GUEST_CREDIT_ALLOWANCE_DEFAULT", 5))


def configured_limit() -> int:
    """The active threshold: credits in credit mode, completed searches in the legacy mode. 0 = the gate is off."""
    return gate_mode()[1]


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
    def __init__(self, coll, clock=None, limit=None, stale_seconds=None, work_lease_seconds=None, credit_allowance=None):
        self.coll = coll
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self._limit, self._stale, self._lease = limit, stale_seconds, work_lease_seconds
        self._credit_allowance = credit_allowance     # an explicit credit allowance (tests); `limit` = the legacy N searches

    @property
    def credits_mode(self) -> bool:
        if self._credit_allowance is not None:
            return True
        if self._limit is not None:
            return False
        return gate_mode()[0] == "credits"

    @property
    def limit(self) -> int:
        """The threshold of the active mode: credits (credit mode) or completed searches (legacy mode); 0 = off."""
        if self._credit_allowance is not None:
            return self._credit_allowance
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

    def _count_field(self) -> str:
        return "credits_used" if self.credits_mode else "completed"

    @staticmethod
    def _bound_cost(doc, token):
        """(cost, reason) bound to `token` (the last bind wins), or None when nothing usable is bound."""
        for entry in reversed((doc or {}).get("token_costs") or []):
            if isinstance(entry, dict) and entry.get("t") == token:
                c = entry.get("c")
                if isinstance(c, int) and not isinstance(c, bool) and c >= 1:
                    return c, entry.get("r")
        return None

    def _cost_for(self, doc, token) -> int:
        """The cost bound to `token`, else the existing deterministic fallback charge. A search accepted by this code always
        has its routed cost bound when it is reserved, so the fallback is only for a token reserved by older code (or whose
        history has aged out) - it is logged, and complete() records the token in `cost_fallback_tokens`."""
        bound = self._bound_cost(doc, token)
        if bound is not None:
            return bound[0]
        import credit_cost
        log.warning("guest gate: a reserved search has no bound cost; the deterministic fallback charge is used (audited)")
        return credit_cost.fallback_cost()

    def _credits_of(self, doc) -> int:
        """credits_used of `doc`; a record written before credit mode is read (never written) through recover_legacy()."""
        if "credits_used" in (doc or {}):
            try:
                return max(0, int(doc["credits_used"]))
            except (TypeError, ValueError):
                return 0
        return self.recover_legacy(doc)[0] if (doc or {}).get("completed") else 0

    def recover_legacy(self, doc):
        """-> (credits, unmatched) for a gate record written when every completed search counted 1 (no cost stored). A
        completed token's cost is recovered ONLY from a reliable chain: the chat message carrying that token -> its
        credit_request_id -> the guest's committed credit entry with a stored integer cost. Anything that cannot be matched
        counts 0 credits and is tallied as unmatched; no cost is ever invented. Read-only."""
        completed = int((doc or {}).get("completed") or 0)
        tokens = list((doc or {}).get("completed_tokens") or [])
        credits, matched = 0, 0
        try:
            import search_credits as sc
            store = sc.default_store()
            if store is not None:
                owner = (doc or {}).get("_id")
                usage = store.coll.find_one({"owner_key": owner}) or {}
                for tok in tokens:
                    chat = store.chats.find_one({"messages.request_id": tok}, {"messages": 1})
                    msg = next((m for m in (chat or {}).get("messages") or [] if m.get("request_id") == tok), None)
                    entry = ((usage.get("res") or {}).get((msg or {}).get("credit_request_id")))
                    if (isinstance(entry, dict) and entry.get("state") == sc.COMMITTED and entry.get("reason") in sc.DELIVERED_REASONS
                            and isinstance(entry.get("cost"), int) and not isinstance(entry.get("cost"), bool) and entry["cost"] >= 1):
                        credits += entry["cost"]
                        matched += 1
        except Exception as exc:
            log.warning(f"guest gate legacy cost recovery failed: {type(exc).__name__}")
        return credits, max(0, completed - matched)

    def _persist_legacy(self, doc):
        """Write the recovered credits_used once (compare-and-set on its absence). Only this guest's own record."""
        try:
            credits, unmatched = self.recover_legacy(doc)
            self.coll.update_one({"_id": doc["_id"], "credits_used": {"$exists": False}},
                                 {"$set": {"credits_used": credits, "legacy_unmatched": unmatched,
                                           "credits_recovered_at": self.now()}})
            if unmatched:
                log.info(f"guest gate: {unmatched} earlier completed search(es) had no recoverable cost and count 0 credits")
        except Exception as exc:
            log.warning(f"guest gate legacy upgrade failed: {type(exc).__name__}")

    def state(self, owner_key) -> dict:
        """{"limit", "completed", "credits_used", "used", "inflight", "last_chat_id", "mode"}. Raises on a store error."""
        doc = self.coll.find_one({"_id": owner_key}) or {}
        completed = int(doc.get("completed") or 0)
        credits = self._credits_of(doc)
        limit = self.limit
        counted = credits if self.credits_mode else completed
        handed_over = isinstance(doc.get("handover"), dict)          # the allowance moved to an account: this record is closed
        return {"limit": limit, "completed": completed, "credits_used": credits,
                "used": limit > 0 and (counted >= limit or handed_over), "handed_over": handed_over,
                "inflight": self._live_inflight(doc) is not None, "last_chat_id": doc.get("last_chat_id"),
                "mode": "credits" if self.credits_mode else "searches"}

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
    @staticmethod
    def _closure_clause(token) -> dict:
        """A record that has been handed over to an account accepts only its hold token (and not once that hold was
        released). Everything else - a new search, a stale or released token - can neither start nor count."""
        return {"$or": [{"handover": {"$exists": False}},
                        {"handover.hold.token": token, "handover.released_token": {"$ne": token}}]}

    def reserve(self, owner_key, token, topic_key=None, chat_id=None, cost=None, cost_reason=None) -> Decision:
        limit = self.limit
        if limit <= 0:
            return Decision(ALLOW)
        field = self._count_field()
        now = self.now()
        cutoff = now - timedelta(seconds=self.stale_seconds)
        inflight = {"token": token, "topic_key": topic_key, "chat_id": chat_id, "started_at": now}
        push = {"tokens": {"$each": [token], "$slice": -TOKEN_HISTORY}}
        costed = isinstance(cost, int) and not isinstance(cost, bool) and cost >= 1
        cost_entry = {"t": token, "c": cost}
        if costed and isinstance(cost_reason, str) and cost_reason.strip():
            cost_entry["r"] = cost_reason.strip()[:120]          # why this cost (a fallback code when the router gave none)
        if costed:
            inflight["cost"] = cost
            push["token_costs"] = {"$each": [cost_entry], "$slice": -TOKEN_HISTORY}
        try:
            try:
                self.coll.insert_one({"_id": owner_key, "completed": 0, "credits_used": 0, "completed_tokens": [], "tokens": [token],
                                      "token_costs": [cost_entry] if costed else [],
                                      "inflight": inflight, "created_at": now, "updated_at": now})
                return Decision(ALLOW, reserved=True)
            except Exception as exc:
                if not _is_duplicate_key(exc):
                    raise
            existing = self.coll.find_one({"_id": owner_key})
            if existing is not None and "credits_used" not in existing and self.credits_mode:
                self._persist_legacy(existing)                     # one upgrade of this guest's record, never invented
            before = self.coll.find_one_and_update(
                {"_id": owner_key, field: {"$lt": limit}, "completed_tokens": {"$ne": token},
                 "$and": [{"$or": [{"inflight": None}, {"inflight.token": token}, {"inflight.started_at": {"$lte": cutoff}}]},
                          self._closure_clause(token)]},
                {"$set": {"inflight": inflight, "updated_at": now}, "$push": push},
                return_document=ReturnDocument.BEFORE)
            if before is not None:
                prior = before.get("inflight")
                resumed = isinstance(prior, dict) and prior.get("token") == token
                return Decision(ALLOW, reserved=True, resumed=resumed)
            doc = self.coll.find_one({"_id": owner_key}) or {}
            if token in (doc.get("completed_tokens") or []):
                return Decision(ALLOW, doc.get("last_chat_id"), resumed=True)    # already counted: never takes the slot again
            counted = self._credits_of(doc) if self.credits_mode else int(doc.get("completed") or 0)
            handover = doc.get("handover")
            if isinstance(handover, dict):
                hold = handover.get("hold") or {}
                if not (hold.get("token") == token and handover.get("released_token") != token):
                    return Decision(AUTH_REQUIRED, doc.get("last_chat_id"))       # closed: the allowance moved to the account
            if counted >= limit:
                return Decision(AUTH_REQUIRED, doc.get("last_chat_id"))
            return Decision(BUSY, (doc.get("inflight") or {}).get("chat_id"))
        except Exception as exc:
            log.warning(f"guest gate reserve failed: {type(exc).__name__}")
            return Decision(UNAVAILABLE)

    def bind_cost(self, owner_key, token, cost, reason=None) -> bool:
        """Records the cost of the reservation `token` (the credit reservation's stored charge, which wins over any earlier
        value). The cost is what complete() adds to credits_used and what a handover holds for a search still running.
        Binding the value that is already bound changes nothing (and keeps its reason)."""
        if not (isinstance(cost, int) and not isinstance(cost, bool) and cost >= 1):
            return False
        try:
            doc = self.coll.find_one({"_id": owner_key, "tokens": token}) or {}
            if not doc:
                return False
            current = self._bound_cost(doc, token)
            if current is not None and current[0] == cost:
                self.coll.update_one({"_id": owner_key, "inflight.token": token}, {"$set": {"inflight.cost": cost}})
                return True
            entry = {"t": token, "c": cost}
            if isinstance(reason, str) and reason.strip():
                entry["r"] = reason.strip()[:120]
            result = self.coll.update_one(
                {"_id": owner_key, "tokens": token},
                {"$push": {"token_costs": {"$each": [entry], "$slice": -TOKEN_HISTORY}}})
            self.coll.update_one({"_id": owner_key, "inflight.token": token}, {"$set": {"inflight.cost": cost}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate bind-cost failed: {type(exc).__name__}")
            return False

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
        try:
            doc0 = self.coll.find_one({"_id": owner_key}) or {}
            if doc0 and "credits_used" not in doc0:
                self._persist_legacy(doc0)
                doc0 = self.coll.find_one({"_id": owner_key}) or {}
            cost = self._cost_for(doc0, token)
            unbound = self._bound_cost(doc0, token) is None
        except Exception as exc:
            log.warning(f"guest gate complete failed: {type(exc).__name__}")
            return "unavailable"
        base = {"_id": owner_key, "tokens": token, "completed_tokens": {"$ne": token}, self._count_field(): {"$lt": self.limit},
                "handover.released_token": {"$ne": token}}
        base["$and"] = [self._closure_clause(token)]          # after a handover only the hold token may count
        if run:
            base["$and"].append({"$or": [{"work_lease": None}, {"work_lease.run": run}, {"work_lease.until": {"$lte": now}}]})
        fields = {"last_chat_id": chat_id, "last_topic_key": topic_key, "last_completed_at": now, "updated_at": now}
        push = {"completed_tokens": {"$each": [token], "$slice": -TOKEN_HISTORY}}
        if unbound:
            push["cost_fallback_tokens"] = {"$each": [token], "$slice": -TOKEN_HISTORY}
        try:
            result = self.coll.update_one({**base, "inflight.token": token},
                                          {"$inc": {"completed": 1, "credits_used": cost}, "$push": push,
                                           "$set": {**fields, "inflight": None}})
            if getattr(result, "matched_count", 0) != 1:
                result = self.coll.update_one({**base, "inflight": None},
                                              {"$inc": {"completed": 1, "credits_used": cost}, "$push": push, "$set": fields})
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
        own count is removed; the slot stays free for a retry.
        Once the record has been HANDED OVER to an account the count is KEPT (returns False): the account was charged for
        it (snapshot or hold), so the credit is not given back; the answer is still owed and the durable turn retry
        re-delivers it without counting again (is_counted() bypasses reserve). The failure is logged and recorded in
        `handover.delivery_failures` so an answer that is never delivered can be found."""
        try:
            doc = self.coll.find_one({"_id": owner_key}) or {}
            inc = {"completed": -1}
            if "credits_used" in doc:
                inc["credits_used"] = -min(self._cost_for(doc, token), max(0, int(doc.get("credits_used") or 0)))
            result = self.coll.update_one({"_id": owner_key, "completed_tokens": token, "completed": {"$gt": 0},
                                           "handover": {"$exists": False}},
                                          {"$inc": inc, "$pull": {"completed_tokens": token},
                                           "$set": {"updated_at": self.now()}})
            if getattr(result, "matched_count", 0) == 1:
                return True
            fresh = self.coll.find_one({"_id": owner_key}) or {}      # the claim may have landed after the first read
            if isinstance(fresh.get("handover"), dict) and token in (fresh.get("completed_tokens") or []):
                self.coll.update_one({"_id": owner_key, "handover": {"$exists": True}},
                                     {"$push": {"handover.delivery_failures": {"$each": [{"t": token, "at": self.now()}],
                                                                                 "$slice": -TOKEN_HISTORY}}})
                log.warning("guest gate: an answer was counted but could not be delivered after the guest's credits were "
                            "handed over to an account; the count is KEPT (the account was charged) and the answer is "
                            "re-delivered by the durable turn retry without counting again")
                self._requeue_undelivered(fresh, token)
            return False
        except Exception as exc:
            log.warning(f"guest gate revert failed: {type(exc).__name__}")
            return False

    @staticmethod
    def _requeue_undelivered(doc, token):
        """Best effort, idempotent: put the turn whose counted answer was not delivered into the durable turn queue under
        the ACCOUNT that now owns the chat, so its retry does not depend on the periodic backfill window. Only when this
        token is the latest completion (the record keeps one chat / topic): otherwise the periodic backfill and the page
        view still find it. Re-delivery never recounts (is_counted bypasses reserve). Never raises."""
        try:
            handover = doc.get("handover") or {}
            tokens = doc.get("completed_tokens") or []
            if not tokens or tokens[-1] != token or not handover.get("to"):
                return
            import turn_finisher
            chat_id, topic_key = doc.get("last_chat_id"), doc.get("last_topic_key")
            if chat_id and topic_key:
                turn_finisher.enqueue(chat_id, handover["to"], topic_key, due_in_seconds=turn_finisher.BACKOFF_BASE_SECONDS)
        except Exception as exc:
            log.warning(f"guest gate: re-queueing the undelivered turn failed: {type(exc).__name__}")

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


    # ── handover to a NEW account ────────────────────────────────────────────────────────────────────────────
    def claim_handover(self, guest_id, email, now=None):
        """Records, in ONE compare-and-set of this document, what moves to the new account `email`: the credits already
        used, a HOLD (token + cost) for a search that is still running, and the free month that receives them
        (search_credits.handover_window_period of `now`). The compare-and-set also requires credits_used, updated_at and the in-flight token to be
        what was read, so a completion that lands in between makes it retry: a completion is either inside the snapshot
        or happens after the claim as the hold's commit - never both, never neither. Idempotent for the same email; another
        email gets None (guest usage moves to one account only). The record is claimed (= closed) even when there is
        nothing to move - a guest identity that created an account must not start a fresh allowance after a logout.
        A reservation that is stale at this moment gets no hold: once the record is claimed it is closed (see
        _closure_clause), so that search can neither start nor count afterwards and nothing escapes the accounting."""
        import search_credits as sc
        now = _aware(now or self.now())
        for _ in range(4):
            doc = self.coll.find_one({"_id": guest_id})
            if doc is None:                       # never searched: still closed, so the identity can not start a fresh allowance
                closed = {"to": email, "at": now, "period": sc.handover_window_period(now), "snapshot": 0, "hold": None}
                try:
                    self.coll.insert_one({"_id": guest_id, "completed": 0, "credits_used": 0, "completed_tokens": [],
                                          "tokens": [], "token_costs": [], "inflight": None, "created_at": now,
                                          "updated_at": now, "handover": closed})
                    return closed
                except Exception as exc:
                    if not _is_duplicate_key(exc):
                        raise
                    continue                      # a first search created the record meanwhile: claim it normally
            existing = doc.get("handover")
            if isinstance(existing, dict):
                return existing if existing.get("to") == email else None
            if "credits_used" not in doc:
                self._persist_legacy(doc)
                continue
            live = self._live_inflight(doc)
            credits = max(0, int(doc.get("credits_used") or 0))
            hold = None
            if live is not None and live.get("token") not in (doc.get("completed_tokens") or []):
                hold = {"token": live["token"], "cost": self._cost_for(doc, live["token"])}
            handover = {"to": email, "at": now, "period": sc.handover_window_period(now), "snapshot": credits, "hold": hold}
            seen_token = (doc.get("inflight") or {}).get("token")
            same_inflight = {"inflight.token": seen_token} if seen_token is not None else {"inflight": None}
            result = self.coll.update_one({"_id": guest_id, "handover": {"$exists": False}, "credits_used": doc["credits_used"],
                                           "updated_at": doc.get("updated_at"), **same_inflight},
                                          {"$set": {"handover": handover}})
            if getattr(result, "matched_count", 0) == 1:
                return handover
        return None

    def fence_release(self, guest_id, token) -> bool:
        """The hold of `token` ends UNCOUNTED. One conditional write on this document: it wins only if the token was not
        counted (completed_tokens) and no outcome was recorded, and from then on complete() refuses the token. So a hold is
        either committed (counted) or released, decided here, never both."""
        try:
            result = self.coll.update_one(
                {"_id": guest_id, "handover.hold.token": token, "completed_tokens": {"$ne": token},
                 "handover.released_token": {"$exists": False}},
                {"$set": {"handover.released_token": token}})
            return getattr(result, "matched_count", 0) == 1
        except Exception as exc:
            log.warning(f"guest gate hold fence failed: {type(exc).__name__}")
            return False


# ── process-wide access + the hooks used inside the search pipeline ─────────────────────
def default_gate():
    """The configured gate, or None when GUEST_SEARCH_LIMIT is 0 (gate off)."""
    if configured_limit() <= 0:
        return None
    import database
    return GuestGate(database.guest_gate_collection)


def _account_store():
    try:
        import search_credits
        return search_credits.default_store()
    except Exception as exc:
        log.warning(f"guest handover: credit store unavailable: {type(exc).__name__}")
        return None


def handover_to_account(guest_id, email, owner_type="email", now=None):
    """Hook: a visitor just CREATED a new account (email sign-up or Google). Moves their completed guest usage (and the cost
    of a search still running, as a hold) onto the account's first free window, exactly once (the claim and the credit
    store's marker are both idempotent). Best effort - never blocks the sign-up; if it fails the account keeps the full grant
    until reconcile_handover() applies it. Existing accounts must never be passed here. Never raises."""
    if not is_guest_owner(guest_id):
        return None
    try:
        import search_credits as sc
        gate = default_gate()
        if gate is None:
            return None
        store = _account_store()
        if store is None and not sc.enforcement_enabled():
            return None               # credit enforcement is off (kill switch): there is no account ledger to hand over to
        handover = gate.claim_handover(guest_id, email, now)         # also CLOSES the guest identity (see claim_handover)
        if handover is None:
            return None
        hold = handover.get("hold") or {}
        if store is None:
            return None               # enforcement is on but the store was unavailable: reconcile_handover() applies it later
        if handover.get("snapshot", 0) == 0 and not hold.get("token"):
            return None               # nothing to move: the identity is closed, the account keeps its full grant
        return store.seed_guest_handover(email, owner_type, guest_id, handover["period"], handover["snapshot"],
                                         hold.get("token"), hold.get("cost", 0), now)
    except Exception as exc:
        log.warning(f"guest handover failed (the account is unaffected): {type(exc).__name__}")
        return None


def _settle_hold(owner_key, token, released=False):
    """Apply the outcome of a search's hold to the account: counted -> commit, ended uncounted -> release. The outcome is
    decided on the gate document (fence_release / completed_tokens); the account-side effect is idempotent. Never raises."""
    try:
        gate = default_gate()
        if gate is None:
            return None
        if released:
            gate.fence_release(owner_key, token)
        doc = gate.coll.find_one({"_id": owner_key}) or {}
        handover = doc.get("handover")
        hold = (handover or {}).get("hold") if isinstance(handover, dict) else None
        if not isinstance(hold, dict) or hold.get("token") != token:
            return None
        store = _account_store()
        if store is None:
            return None
        if token in (doc.get("completed_tokens") or []):
            return "committed" if store.commit_guest_hold(handover["to"], token) else "noop"
        if handover.get("released_token") == token:
            return store.release_guest_hold(handover["to"], token)
        return "pending"
    except Exception as exc:
        log.warning(f"guest handover hold settlement failed: {type(exc).__name__}")
        return None


def _reconcile_handover(guest_id, email, now=None, store=None):
    """The reconcile logic; raises on a store error (reconcile_handover() swallows, handover_ready_for_activation() must not).
    -> None (nothing to do) | "seeded" | "pending_expired" | the hold settlement ("committed" | "released" | ... | "pending")."""
    gate = default_gate()
    store = store if store is not None else (_account_store() if gate is not None else None)
    if gate is None or store is None:
        return None
    doc = gate.coll.find_one({"_id": guest_id}) or {}
    handover = doc.get("handover")
    if not isinstance(handover, dict) or handover.get("to") != email:
        return None
    hold = handover.get("hold") or {}
    seeded = store.seed_guest_handover(email, "email", guest_id, handover["period"], handover["snapshot"], hold.get("token"),
                                       hold.get("cost", 0), now)
    if seeded.get("status") == "pending_expired":
        log.warning(f"guest handover for {email}: its target month {handover['period']} is over; the deduction is PENDING "
                    f"an explicit resolution (guest_gate.resolve_handover) and is not applied to any other month")
        return "pending_expired"
    token = hold.get("token")
    if token:
        live = gate._live_inflight(doc)
        if token not in (doc.get("completed_tokens") or []) and (live is None or live.get("token") != token):
            gate.fence_release(guest_id, token)                 # the search is gone without being counted
        return _settle_hold(guest_id, token)
    return "seeded"


def reconcile_handover(guest_id, email, now=None, store=None):
    """Hook (idempotent, never raises): finish a handover that was interrupted - apply a claimed-but-unseeded handover, and
    settle the hold of a search that is no longer running (released if it ended uncounted, committed if it was counted).
    Only acts for the account that claimed it. A handover whose target month is already over is NOT applied: it stays
    "pending_expired" until resolve_handover() records an explicit decision."""
    if not is_guest_owner(guest_id):
        return None
    try:
        return _reconcile_handover(guest_id, email, now, store)
    except Exception as exc:
        log.warning(f"guest handover reconcile failed: {type(exc).__name__}")
        return None


def handover_ready_for_activation(email, store, now=None) -> bool:
    """Called before a FIRST free -> paid activation (billing.py). Any guest handover claimed for `email` is first applied
    (an interrupted one is finished); then False while a handover is still unresolved (target month over, no explicit
    resolution yet), so the activation cannot silently carry over credits the guest already used. Raises on a store error:
    the caller must treat that as 'not ready' (retry later), never as 'nothing to do'."""
    gate = default_gate()
    if gate is not None:
        doc = gate.coll.find_one({"handover.to": email}, {"_id": 1})      # (not indexed: first activations are rare)
        if doc is not None:
            _reconcile_handover(doc["_id"], email, now, store)
    return not store.has_pending_guest_handover(email)


def resolve_handover(guest_id, email, outcome, reason, now=None, store=None):
    """OPERATOR function (no route calls it): explicitly resolve a handover left "pending_expired".
      outcome "apply_late"  deduct from the ORIGINAL target month only (clamped to what that month's grant has room for)
      outcome "waive"       record that nothing is deducted
    `reason` is required and stored with the resolution. Neither outcome is ever chosen automatically. Exactly once: the
    account marker is written by one conditional update; a repeat returns "duplicate". The hold (a search running at
    sign-up) must already be decided (counted or released); reconcile it first otherwise ("hold_undecided").
    -> "applied_late" | "waived" | "duplicate" | "no_pending" | "hold_undecided" | "period_frozen" | "invalid" | "unavailable"."""
    if outcome not in ("apply_late", "waive") or not isinstance(reason, str) or not reason.strip() or not is_guest_owner(guest_id):
        return "invalid"
    try:
        gate = default_gate()
        store = store if store is not None else (_account_store() if gate is not None else None)
        if gate is None or store is None:
            return "unavailable"
        doc = gate.coll.find_one({"_id": guest_id}) or {}
        handover = doc.get("handover")
        if not isinstance(handover, dict) or handover.get("to") != email:
            return "no_pending"
        hold = handover.get("hold") or {}
        token, hold_amount = hold.get("token"), 0
        if token:
            live = gate._live_inflight(doc)
            if token not in (doc.get("completed_tokens") or []) and handover.get("released_token") != token \
                    and (live is None or live.get("token") != token):
                gate.fence_release(guest_id, token)
                doc = gate.coll.find_one({"_id": guest_id}) or {}
                handover = doc.get("handover") or handover
            if token in (doc.get("completed_tokens") or []):
                hold_amount = int(hold.get("cost") or 0)
            elif handover.get("released_token") != token:
                return "hold_undecided"
        result = store.resolve_pending_guest_handover(email, guest_id, outcome, reason.strip(), handover.get("snapshot", 0),
                                                      hold_amount, now)
        log.warning(f"guest handover resolution for {email}: {outcome} -> {result.get('status')}")
        if result.get("status") in ("applied_late", "waived"):     # a first paid activation may have been waiting for this
            try:
                import billing
                billing.resume_activation_default(email, store, now)
            except Exception as exc:
                log.warning(f"guest handover: resuming the activation failed ({type(exc).__name__})")
        return {"applied_late": "applied_late", "waived": "waived"}.get(result.get("status"), result.get("status"))
    except Exception as exc:
        log.warning(f"guest handover resolution failed: {type(exc).__name__}")
        return "unavailable"


def routed_cost(routed):
    """The credit-cost decision (cost, effort, reason) of the routed search under the EXISTING credit-cost rules
    (credit_cost.select_cost; credit_cost.py is not changed). Used for a guest search that has no credit-store ticket (credit
    enforcement switched off) so the gate counts the real routed cost, not a fixed one. Raises when no usable cost can be
    decided: the caller fails closed (nothing is reserved or enqueued)."""
    import credit_cost
    decision = credit_cost.select_cost(routed)
    cost = getattr(decision, "cost", None)
    if not isinstance(cost, int) or isinstance(cost, bool) or cost < 1:
        raise ValueError("no usable routed cost")
    return decision


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
            released = gate.release(owner_key, token)
            _settle_hold(owner_key, token, released=True)
            return released
        outcome = gate.complete(owner_key, token, chat_id, topic_key)
        if outcome in ("completed", "duplicate"):
            _settle_hold(owner_key, token)
        return outcome
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
            _settle_hold(owner_key, token, released=True)
            return DELIVER
        outcome = gate.complete(owner_key, token, chat_id, topic_key, run)
    except Exception as exc:
        log.warning(f"guest gate answer-ready hook failed: {type(exc).__name__}")
        return DEFER
    if outcome in ("completed", "duplicate"):
        _settle_hold(owner_key, token)
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
        if gate is None:
            return None
        released = gate.release(owner_key, token)
        _settle_hold(owner_key, token, released=True)      # no-op unless this token is a handed-over hold that was not counted
        return released
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
