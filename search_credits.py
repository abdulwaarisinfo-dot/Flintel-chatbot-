"""
search_credits.py — monthly search credits (STAGE 1 backend).

An accepted search (a normal search or a website-URL search) costs a whole
number of credits chosen once, before the reservation (credit_cost.py), and
stored on its entry. Non-search turns, opening chats, reading history, stream
reconnects and page reloads never touch this module.

The monthly limit is the free allowance, or the plan's allowance for a
registered email owner with an ACTIVE record in `flintel_plans` (see
limit_for()). There is no payment code here; activate_plan() is the function a
future webhook would call and is not wired to any route.

Storage: one document per owner in `flintel_credit_usage` (unique owner_key):

    {owner_key, owner_type,
     used_by_period:      {"YYYY-MM": int},   # C[P]
     limit_max_by_period: {"YYYY-MM": int},   # highest limit a reservation of P used
     archived_by_period:  {"YYYY-MM": int},   # A[P]: committed entries pruned from P
     res: {<rid>: entry}}                     # one entry per request id

An entry carries `cost` (credits it holds; a legacy entry without one holds 1),
`effort` and a short `cost_reason` (never the query text).

Accounting invariant, for every month P:
    C[P] = L[P] + A[P]   and   0 <= C[P] <= limit_max_by_period[P]
where L[P] is the SUM OF THE COSTS of the entries of month P in a charge-holding
state (every state except `refunded`). Only a reservation (T1/T1r) raises a
counter (by the entry's cost), only a refund (T3/T3s) lowers one (by exactly
that cost), and both change the counter and the entry in the same
single-document update. A reservation succeeds only when
used[P] + cost <= limit, in that same update (no partial charge, no overdraft).
check_invariant() verifies it.

Safety rule: a refund is only possible from `reserved` or `started`. The job
is only sent after the claim `started -> sending` (T4) was acknowledged with
matched == 1, and nothing ever moves an entry from `sending` or later back to
a refundable state. So a job can never land for a refunded reservation.

Every state change goes through _cas(): a conditional update on the entry's
expected state, attempt and version (ev). An acknowledged update that matched
nothing is re-read once and classified with OUTCOME_ROWS below (data, shared
with the tests). Timeouts are never treated as proof of anything.

Job attribution: enqueue_search_job(credit_request_id=rid) pushes rid onto the
job's `credit_request_ids` (last JOB_ATTRIBUTION_CAP ids) in the same upsert.
That list is bounded evidence only, not an idempotency guarantee; a missing
id never refunds anything. COMPATIBILITY WITH THE EXTERNAL COLLECTOR (which
reads flintel_search_jobs and is not in this repository) IS UNVERIFIED.

Operator close (resolve "close") uses a marker on the chat document
(`credit_close.<rid>`): every message save's own filter refuses to write while
a marker exists, except the documented S-late save of the original request.
"""

import contextlib
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger("flintel.credits")

# ── states / reasons / outcomes ──────────────────────────────────────────────
RESERVED, STARTED, SENDING, ENQUEUED = "reserved", "started", "sending", "enqueued"
MESSAGE_FAILED, NEEDS_REC, COMMITTED, REFUNDED = "message_failed", "needs_reconciliation", "committed", "refunded"
CHARGED_STATES = frozenset({RESERVED, STARTED, SENDING, ENQUEUED, MESSAGE_FAILED, NEEDS_REC, COMMITTED})

R_AMBIGUOUS = "ambiguous_attribution"
R_MSG_MISSING = "job_enqueued_message_missing"
R_NORMAL, R_REPAIRED, R_ALREADY = "normal", "repaired", "already_present"
R_CHAT_MISSING, R_MANUAL = "chat_missing_at_repair", "manual_close"
DELIVERED_REASONS = frozenset({R_NORMAL, R_REPAIRED, R_ALREADY})   # a message for the rid exists

RETRY_FRESH, RETRY_PAYLOAD, DONE, PROCEED = "retry_fresh", "retry_payload", "done", "proceed"
COMPLETED, SUPERSEDED, TERMINAL, TERMINAL_NO_DELIVERY = "completed", "superseded", "terminal", "terminal_no_delivery"
NOOP, CONFLICT, CLAIM_UNCONFIRMED, SAME_KEY = "noop", "conflict", "claim_unconfirmed", "same_key"

# User-facing texts (Stage 1 renders them in the existing error card).
EXHAUSTED_MESSAGE = "You're out of free searches until {reset}. You can still open your chats and ask follow-up questions."
UNAVAILABLE_MESSAGE = "We couldn't verify your search credits right now. Please try again in a moment."
IN_FLIGHT_MESSAGE = "This search is still being processed. It will appear in your chat when it's ready."
RECONCILIATION_MESSAGE = ("We couldn't confirm whether this search started. It still counts toward your free "
                          "searches. If it ran, its results will appear in this chat. You don't need to send it again.")

# ── matched-zero classification table ───────────────────────────────────────
# Row: (observed state | "missing" | "*", reasons (set) | None for any,
#       attempt "same" | "other" | "*", outcome). First match wins; no match
# means DEFAULT_OUTCOME[transition]. Rule S (same state, reason and attempt —
# only ev differs) is applied before the rows, except for RULE_S_EXEMPT.
_ANY = None
OUTCOME_ROWS = {
    "T2": [("missing", _ANY, "*", SUPERSEDED), (REFUNDED, _ANY, "*", SUPERSEDED), ("*", _ANY, "other", SUPERSEDED)],
    "T3s": [(SENDING, _ANY, "same", CLAIM_UNCONFIRMED), (REFUNDED, _ANY, "*", SUPERSEDED),
            ("missing", _ANY, "*", SUPERSEDED), ("*", _ANY, "other", SUPERSEDED)],
    "T4": [("missing", _ANY, "*", SUPERSEDED), (REFUNDED, _ANY, "*", SUPERSEDED), ("*", _ANY, "other", SUPERSEDED)],
    "T6a": [(NEEDS_REC, {R_AMBIGUOUS}, "same", RETRY_FRESH), (ENQUEUED, _ANY, "same", DONE),
            (NEEDS_REC, {R_MSG_MISSING}, "same", PROCEED), (COMMITTED, set(DELIVERED_REASONS), "same", COMPLETED),
            (COMMITTED, {R_CHAT_MISSING}, "same", TERMINAL_NO_DELIVERY), (COMMITTED, {R_MANUAL}, "same", TERMINAL)],
    "T6b": [(ENQUEUED, _ANY, "same", RETRY_PAYLOAD), (NEEDS_REC, _ANY, "same", RETRY_PAYLOAD),
            (COMMITTED, _ANY, "same", TERMINAL_NO_DELIVERY)],
    "T6c": [("*", _ANY, "*", NOOP)],
    "T7": [("*", _ANY, "*", NOOP)],
    "T8": [(NEEDS_REC, {R_MSG_MISSING, R_AMBIGUOUS}, "same", RETRY_FRESH),
           (COMMITTED, set(DELIVERED_REASONS), "same", COMPLETED), (COMMITTED, {R_MANUAL}, "same", TERMINAL)],
    "T9": [(NEEDS_REC, {R_MSG_MISSING}, "same", RETRY_PAYLOAD), (COMMITTED, set(DELIVERED_REASONS), "same", COMPLETED),
           (COMMITTED, {R_MANUAL, R_CHAT_MISSING}, "same", TERMINAL_NO_DELIVERY)],
    "T10": [("*", _ANY, "*", NOOP)],
    "T11": [("*", _ANY, "*", NOOP)],
    "TSTALE": [("*", _ANY, "*", NOOP)],
    "T12": [("*", _ANY, "*", SAME_KEY)],
    "T13": [("*", _ANY, "*", SUPERSEDED)],
    "T15": [("*", _ANY, "*", NOOP)],
}
DEFAULT_OUTCOME = {t: CONFLICT for t in OUTCOME_ROWS}
RULE_S_EXEMPT = frozenset({"T12"})


def classify(transition: str, expected: dict, observed) -> str:
    """Outcome for an acknowledged update of `transition` that matched nothing.
    `expected` = {state, reason, attempt}; `observed` = the re-read entry or None."""
    if observed is not None and transition not in RULE_S_EXEMPT:
        if (observed.get("state") == expected.get("state") and observed.get("reason") == expected.get("reason")
                and observed.get("attempt") == expected.get("attempt")):
            return RETRY_FRESH
    o_state = "missing" if observed is None else observed.get("state")
    same = observed is not None and observed.get("attempt") == expected.get("attempt")
    for state, reasons, attempt, outcome in OUTCOME_ROWS.get(transition, []):
        if state != "*" and state != o_state:
            continue
        if state == "*" and o_state == "missing" and attempt != "*":
            continue
        if reasons is not None and (observed or {}).get("reason") not in reasons:
            continue
        if attempt == "same" and not same:
            continue
        if attempt == "other" and (observed is None or same):
            continue
        return outcome
    return DEFAULT_OUTCOME.get(transition, CONFLICT)


# ── small helpers ────────────────────────────────────────────────────────────
class CreditStoreError(Exception):
    """The credit store could not be read or written, or the outcome of a write is unknown."""


def _cfg():
    import config   # read at call time: tests reload/patch config
    return config


def configured_limit() -> int:
    try:
        value = int(getattr(_cfg(), "FREE_MONTHLY_CREDITS_EFFECTIVE", 40))
    except (TypeError, ValueError):
        value = 40
    return value if 1 <= value <= 10000 else 40


# ── plans (monthly allowance of an active paid record) ───────────────────────
PLAN_FREE, PLAN_STANDARD, PLAN_BUSINESS = "free", "standard", "business"
PAID_PLANS = (PLAN_STANDARD, PLAN_BUSINESS)


def plan_credits(plan: str):
    """Monthly credits of a paid plan, or None for an unknown plan name."""
    name = {PLAN_STANDARD: "PLAN_CREDITS_STANDARD", PLAN_BUSINESS: "PLAN_CREDITS_BUSINESS"}.get(plan)
    if name is None:
        return None
    default = {PLAN_STANDARD: 400, PLAN_BUSINESS: 1500}[plan]
    try:
        value = int(getattr(_cfg(), name, default))
    except (TypeError, ValueError):
        value = default
    return value if 1 <= value <= 1000000 else default


def registered_owner(owner_key, owner_type=None) -> bool:
    """Only a registered email owner can hold a plan; a guest (UUID key) never can."""
    return isinstance(owner_key, str) and "@" in owner_key and owner_type in (None, "email")


def active_plan(record, now):
    """The plan name when `record` is a well-formed plan record whose
    [active_from, active_until) window contains `now`; None otherwise
    (no record, unknown plan, missing or unusable dates)."""
    if not isinstance(record, dict) or record.get("plan") not in PAID_PLANS:
        return None
    start, end = record.get("active_from"), record.get("active_until")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return record["plan"] if _aware(start) <= _aware(now) < _aware(end) else None


def activate_plan(plans, owner_key, plan, active_until, source_ref, now=None, active_from=None) -> str:
    """Make `plan` active for a registered email owner until `active_until` (UTC).
    NOT wired to any route: a future payment webhook would call it with the
    payment provider's transaction id as `source_ref`.

    Idempotent per source_ref: the owner's record keeps every source_ref it has
    applied, and the one conditional upsert below only matches when this
    source_ref is NOT among them. A replay (same ref, any order, sequential or
    concurrent) matches nothing, tries to insert, and is stopped by the unique
    owner_key index (DuplicateKeyError) — so it never extends or re-activates.
    A new source_ref replaces plan / dates (an upgrade or a renewal).
    Returns "activated" or "duplicate". Raises ValueError for bad arguments and
    CreditStoreError when the store fails (the caller must then NOT acknowledge
    the payment event)."""
    if not registered_owner(owner_key):
        raise ValueError("a plan can only be activated for a registered email owner")
    if plan not in PAID_PLANS:
        raise ValueError(f"unknown plan {plan!r}")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("source_ref is required (it is the idempotency key)")
    if not isinstance(active_until, datetime):
        raise ValueError("active_until must be a datetime")
    now = _aware(now or datetime.now(timezone.utc))
    start = _aware(active_from) if active_from is not None else now
    if _aware(active_until) <= start:
        raise ValueError("active_until must be after active_from")
    try:
        result = plans.update_one(
            {"owner_key": owner_key, "applied_refs": {"$ne": source_ref}},
            {"$set": {"plan": plan, "active_from": start, "active_until": _aware(active_until),
                      "source_ref": source_ref, "updated_at": now},
             "$addToSet": {"applied_refs": source_ref}},
            upsert=True)
    except Exception as exc:
        if type(exc).__name__ == "DuplicateKeyError":
            return "duplicate"
        raise CreditStoreError(f"plan write failed: {type(exc).__name__}") from exc
    if getattr(result, "acknowledged", True) is not True:
        raise CreditStoreError("plan write not acknowledged")
    return "activated"


def enforcement_enabled() -> bool:
    return not bool(getattr(_cfg(), "CREDIT_ENFORCEMENT_DISABLED", False))


def job_attribution_enabled() -> bool:
    """JOB_ATTRIBUTION_MODE=field and this process has not seen a definitive
    validation rejection of the attribution field (logics.py)."""
    if str(getattr(_cfg(), "JOB_ATTRIBUTION_MODE", "off")).lower() != "field":
        return False
    import sys
    logics = sys.modules.get("logics")
    rejected = getattr(logics, "_JOB_ATTRIBUTION_REJECTED", {"value": False}) if logics else {"value": False}
    return not rejected.get("value")


def attribution_mode() -> str:
    if job_attribution_enabled():
        return "field"
    configured = str(getattr(_cfg(), "JOB_ATTRIBUTION_MODE", "off")).lower()
    return "field_rejected" if configured == "field" else "off"


def _setting(name: str, default: int) -> int:
    try:
        return int(getattr(_cfg(), name, default))
    except (TypeError, ValueError):
        return default


def period_key(now: datetime) -> str:
    now = _aware(now)
    return f"{now.year:04d}-{now.month:02d}"


def period_resets_at(now: datetime) -> datetime:
    now = _aware(now)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def previous_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return f"{year:04d}-{month:02d}"


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _entry_cost(entry) -> int:
    """Credits an entry holds. Legacy entries (written before usage-based costs) hold 1."""
    cost = (entry or {}).get("cost")
    return cost if isinstance(cost, int) and not isinstance(cost, bool) and cost >= 1 else 1


_SUBMIT_KEY_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")


def request_id(owner_key: str, submit_key) -> str:
    """Owner-scoped id for one intentional submit: the same submit_key from the
    same owner is the same request (retries); anything else gets a fresh id."""
    if isinstance(submit_key, str) and _SUBMIT_KEY_RE.match(submit_key):
        return hashlib.sha256(f"{owner_key}\x00{submit_key}".encode("utf-8")).hexdigest()[:32]
    return uuid.uuid4().hex


def message_save_filter(chat_id: str, owner_key: str, rid: str, late_delivery=None) -> dict:
    """S-normal (no close marker may exist) or S-late (only a completed close of
    this exact attempt and close token). Shared by index.add_search_to_chat()
    and the tests, so there is one definition of what a save may do."""
    filt = {"chat_id": chat_id, "owner_key": owner_key, "messages.credit_request_id": {"$ne": rid}}
    if late_delivery:
        attempt, close_token = late_delivery
        if not attempt or not close_token:
            raise ValueError("S-late needs the attempt and the close token")
        filt[f"credit_close.{rid}.phase"] = "closed"
        filt[f"credit_close.{rid}.attempt"] = attempt
        filt[f"credit_close.{rid}.close_token"] = close_token
    else:
        filt[f"credit_close.{rid}"] = {"$exists": False}
    return filt


def is_e1(result) -> bool:
    """E1: our own enqueue upsert was acknowledged and applied."""
    if result is None or getattr(result, "acknowledged", False) is not True:
        return False
    return getattr(result, "matched_count", 0) == 1 or getattr(result, "upserted_id", None) is not None


@contextlib.contextmanager
def _enqueue_timeout(seconds: int):
    """Bounds how long the request waits for the enqueue. Never proof of anything."""
    try:
        import pymongo
        cm = pymongo.timeout(seconds)
    except Exception:
        cm = contextlib.nullcontext()
    with cm:
        yield


def check_invariant(doc) -> list:
    """Violations of C[P] = L[P] + A[P] (L = sum of entry costs) and 0 <= C[P] <= limit_max[P] (empty = holds)."""
    if not doc:
        return []
    used = doc.get("used_by_period") or {}
    archived = doc.get("archived_by_period") or {}
    limits = doc.get("limit_max_by_period") or {}
    live = {}
    for entry in (doc.get("res") or {}).values():
        if entry.get("state") in CHARGED_STATES:
            live[entry.get("period")] = live.get(entry.get("period"), 0) + _entry_cost(entry)
    problems = []
    for period in set(used) | set(archived) | set(live):
        c, a, l_ = int(used.get(period, 0)), int(archived.get(period, 0)), live.get(period, 0)
        if c != l_ + a:
            problems.append(f"{period}: C={c} != L={l_} + A={a}")
        if c < 0:
            problems.append(f"{period}: C={c} < 0")
        if c > 0 and c > int(limits.get(period, 0)):
            problems.append(f"{period}: C={c} > limit_max={limits.get(period)}")
    return problems


# ── request-side value objects ───────────────────────────────────────────────
@dataclass
class Decision:
    """What the route should answer: done / redirect / busy / reconciliation /
    exhausted (429) / unavailable (503) / continue (early lookup only)."""
    kind: str
    chat_id: str = None
    resets_at: datetime = None
    message: str = None
    cleanup_ok: bool = False   # an empty chat created for this request may be deleted


@dataclass
class Ticket:
    owner_key: str
    rid: str
    attempt: str
    ev: int
    state: str
    chat_id: str
    topic_key: str
    expects_job: bool
    period: str
    reason: str = None
    e1: bool = False
    extra: dict = field(default_factory=dict)
    cost: int = 1   # credits this reservation holds (the entry's stored cost)

    def expected(self) -> dict:
        return {"state": self.state, "reason": self.reason, "attempt": self.attempt, "ev": self.ev}

    def follow(self, observed: dict):
        self.state, self.reason, self.ev = observed.get("state"), observed.get("reason"), observed.get("ev")


# ── the store ────────────────────────────────────────────────────────────────
class CreditStore:
    def __init__(self, coll, chats, jobs, save_message, clock=None, enabled=None, limit=None, checkpoint=None,
                 plans=None):
        self.coll, self.chats, self.jobs, self.save_message = coll, chats, jobs, save_message
        self.plans = plans   # flintel_plans handle; None = every owner is on the free allowance
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self.enabled = enabled or enforcement_enabled
        self.limit = limit or configured_limit
        self.checkpoint = checkpoint or (lambda name, **ctx: None)   # test hook, no-op in production

    # ── monthly limit (free allowance, or an active paid plan) ──────────────
    def plan_for(self, owner_key, now=None, owner_type=None) -> str:
        """"free", or the plan name of an ACTIVE record for a registered email
        owner. Fail closed: a plan-store read error raises CreditStoreError (the
        route answers 503); it never falls back to a limit."""
        if self.plans is None or not registered_owner(owner_key, owner_type):
            return PLAN_FREE
        try:
            record = self.plans.find_one({"owner_key": owner_key})
        except Exception as exc:
            raise CreditStoreError(f"plan read failed: {type(exc).__name__}") from exc
        return active_plan(record, now or self.now()) or PLAN_FREE

    def limit_for(self, owner_key, now=None, owner_type=None) -> int:
        """This owner's monthly limit right now. A plan REPLACES the free
        allowance; after it expires the free allowance applies again, so usage
        above it simply denies until the next period (counters are never rewritten)."""
        plan = self.plan_for(owner_key, now, owner_type)
        if plan == PLAN_FREE:
            return int(self.limit())
        return int(plan_credits(plan))

    # ── low-level reads/writes ───────────────────────────────────────────────
    def _read_doc(self, owner_key):
        try:
            return self.coll.find_one({"owner_key": owner_key})
        except Exception as exc:
            raise CreditStoreError(f"read failed: {type(exc).__name__}") from exc

    def _read_entry(self, owner_key, rid):
        try:
            doc = self.coll.find_one({"owner_key": owner_key}, {f"res.{rid}": 1})
        except Exception as exc:
            raise CreditStoreError(f"read failed: {type(exc).__name__}") from exc
        return ((doc or {}).get("res") or {}).get(rid)

    def _update(self, filt, update):
        try:
            result = self.coll.update_one(filt, update)
        except Exception as exc:
            raise CreditStoreError(f"write failed: {type(exc).__name__}") from exc
        if getattr(result, "acknowledged", True) is not True:
            raise CreditStoreError("write not acknowledged")
        return result.matched_count == 1

    def _cas(self, owner_key, rid, expected, set_=None, unset=(), counter=None, extra=None, push=None):
        """Conditional update on (state, reason, attempt, ev). Returns (ok, observed)."""
        filt = {"owner_key": owner_key, f"res.{rid}.state": expected["state"],
                f"res.{rid}.attempt": expected["attempt"], f"res.{rid}.ev": expected["ev"],
                f"res.{rid}.reason": expected.get("reason")}
        if extra:
            filt.update(extra)
        update = {"$inc": {f"res.{rid}.ev": 1}}
        if counter:
            update["$inc"].update(counter)
        if set_:
            update["$set"] = {f"res.{rid}.{k}": v for k, v in set_.items()}
        if unset:
            update["$unset"] = {f"res.{rid}.{k}": "" for k in unset}
        if push:
            update["$push"] = {f"res.{rid}.{k}": v for k, v in push.items()}
        if self._update(filt, update):
            return True, None
        return False, self._read_entry(owner_key, rid)

    def _stale(self, entry, *fields) -> bool:
        stamps = [_aware(entry.get(f)) for f in fields if entry.get(f) is not None] or [_aware(entry.get("at"))]
        newest = max(s for s in stamps if s is not None)
        return (self.now() - newest).total_seconds() >= _setting("RESERVATION_STALE_SECONDS", 900)

    def _has_e2(self, rid, entry) -> bool:
        if self.jobs is None or not entry.get("topic_key"):
            return False
        try:
            return self.jobs.find_one({"topic_key": entry["topic_key"], "credit_request_ids": rid}, {"_id": 1}) is not None
        except Exception as exc:
            raise CreditStoreError(f"job read failed: {type(exc).__name__}") from exc

    def _chat_view(self, owner_key, chat_id, rid):
        """None if the chat is not visible to this owner; else {has_message, marker}."""
        if not chat_id:
            return None
        try:
            doc = self.chats.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"_id": 1, f"credit_close.{rid}": 1})
            if doc is None:
                return None
            has_message = self.chats.find_one({"chat_id": chat_id, "owner_key": owner_key,
                                               "messages.credit_request_id": rid}, {"_id": 1}) is not None
        except Exception as exc:
            raise CreditStoreError(f"chat read failed: {type(exc).__name__}") from exc
        return {"has_message": has_message, "marker": ((doc.get("credit_close") or {}).get(rid))}

    def _new_entry(self, period, attempt, chat_id, topic_key, expects_job, ev=1, cost=1, effort=None,
                   cost_reason=None):
        now = self.now()
        return {"cost": cost, "effort": effort, "cost_reason": cost_reason, "state": RESERVED, "reason": None, "period": period, "attempt": attempt, "ev": ev, "at": now,
                "started_at": None, "sending_at": None, "enqueued_at": None, "failed_at": None,
                "claim_unconfirmed": False, "sent_once": False, "chat_id": chat_id, "topic_key": topic_key,
                "expects_job": bool(expects_job), "pending_message": None, "close_token": None, "audit": []}

    def _exhausted(self, cleanup_ok=True):
        return Decision("exhausted", resets_at=period_resets_at(self.now()), cleanup_ok=cleanup_ok)

    # ── early same-key lookup (before the router; not a credit check) ────────
    def lookup(self, owner_key, rid) -> Decision:
        entry = self._read_entry(owner_key, rid)
        if entry is None or entry.get("state") == REFUNDED:
            return Decision("continue")
        state = entry.get("state")
        if state in (RESERVED, STARTED):
            return Decision("continue") if self._resumable(entry) else Decision("busy", message=IN_FLIGHT_MESSAGE)
        if state == SENDING and not entry.get("failed_at") and not self._stale(entry, "sending_at"):
            return Decision("busy", message=IN_FLIGHT_MESSAGE)
        if state == ENQUEUED and not entry.get("pending_message") and not self._stale(entry, "enqueued_at"):
            return Decision("busy", message=IN_FLIGHT_MESSAGE)
        self._advance(owner_key, rid, entry, allow_retry_inputs=False)
        entry = self._read_entry(owner_key, rid)
        if entry and not entry.get("pending_message") and (
                entry.get("state") == ENQUEUED or
                (entry.get("state") == NEEDS_REC and entry.get("reason") == R_MSG_MISSING)):
            return Decision("continue")   # the retry's own inputs may repair it at the reservation point
        return self._decision_for(entry)

    def _resumable(self, entry) -> bool:
        return not entry.get("sent_once") and (bool(entry.get("failed_at")) or self._stale(entry, "started_at"))

    def precheck_exhausted(self, owner_key, rid, cost=1, owner_type=None):
        """OPTIMIZATION ONLY — never authoritative, never writes, never charges
        or refunds. One read: an "exhausted" Decision when this request id has
        no entry at all and this month's count plus `cost` would pass the limit;
        None otherwise (including any store error), so the caller continues and the
        atomic reservation decides. Same-key retries (an existing entry, in any
        state) are always left to the reservation's same-key handling."""
        try:
            doc = self.coll.find_one({"owner_key": owner_key})
        except Exception:
            return None
        if doc and ((doc.get("res") or {}).get(rid)) is not None:
            return None
        period = period_key(self.now())
        used = int(((doc or {}).get("used_by_period") or {}).get(period, 0))
        try:
            limit = self.limit_for(owner_key, self.now(), owner_type)
        except CreditStoreError:
            return None
        if used + _entry_cost({"cost": cost}) > limit:
            return self._over_limit(_entry_cost({"cost": cost}), limit, used)
        return None

    def _decision_for(self, entry) -> Decision:
        if entry is None:
            return Decision("redirect")
        state = entry.get("state")
        if state == COMMITTED:
            return Decision("redirect", chat_id=None if entry.get("reason") == R_CHAT_MISSING else entry.get("chat_id"))
        if state == NEEDS_REC:
            return Decision("reconciliation", chat_id=entry.get("chat_id"), message=RECONCILIATION_MESSAGE)
        return Decision("busy", chat_id=entry.get("chat_id"), message=IN_FLIGHT_MESSAGE)

    # ── reservation point (T1 / T1r / T12, or same-key handling) ────────────
    def _over_limit(self, cost, limit, used=None) -> Decision:
        """A search that costs more than the credits available (more than the whole
        limit, or more than what remains). Denied with the unchanged exhausted
        message, no write at all. The message says "out of free searches" even when
        some credits remain, which is a known wording limitation; the WARNING has
        the facts (no owner or query). A plain exhaustion (nothing left) is not a
        cost-based denial and is not logged here."""
        if used is None or used < limit:
            log.warning(f"credit denial (cost-based): a search costing {cost} credits exceeds what is available "
                        f"(limit {limit}, used {'unknown' if used is None else used})")
        return self._exhausted()

    def reserve(self, owner_key, owner_type, rid, topic_key, chat_id, expects_job, retry_message=None,
                cost=1, effort=None, cost_reason=None):
        """`cost` (whole credits, decided once by the caller) is held by this
        request's entry. A new reservation is ONE conditional update that succeeds
        only if used[P] + cost <= limit; otherwise nothing is written. A request
        id that already has an entry keeps the cost stored on it — a retry,
        resume or reconciliation never changes or repeats the charge."""
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
            raise ValueError(f"cost must be a whole number >= 1, got {cost!r}")
        for _ in range(4):
            now = self.now()
            period = period_key(now)
            doc = self._read_doc(owner_key)
            entry = ((doc or {}).get("res") or {}).get(rid) if doc else None
            if entry is None:
                limit = int(self.limit_for(owner_key, now, owner_type))   # fail closed: raises on a plan-store error
                if cost > limit:
                    return self._over_limit(cost, limit)
                attempt = uuid.uuid4().hex
                new = self._new_entry(period, attempt, chat_id, topic_key, expects_job, cost=cost, effort=effort,
                                      cost_reason=cost_reason)
                if doc is None:
                    try:
                        self.coll.insert_one({"owner_key": owner_key, "owner_type": owner_type,
                                              "used_by_period": {period: cost}, "limit_max_by_period": {period: limit},
                                              "archived_by_period": {}, "res": {rid: new}})
                        return self._ticket(owner_key, rid, new)
                    except Exception as exc:
                        if type(exc).__name__ != "DuplicateKeyError":
                            raise CreditStoreError(f"insert failed: {type(exc).__name__}") from exc
                        continue   # another request created the document first
                ok = self._update(
                    {"owner_key": owner_key, f"res.{rid}": {"$exists": False},
                     "$or": [{f"used_by_period.{period}": {"$exists": False}},
                             {f"used_by_period.{period}": {"$lte": limit - cost}}]},
                    {"$set": {f"res.{rid}": new}, "$inc": {f"used_by_period.{period}": cost},
                     "$max": {f"limit_max_by_period.{period}": limit}})
                if ok:
                    return self._ticket(owner_key, rid, new)
                doc = self._read_doc(owner_key)
                seen = int(((doc or {}).get("used_by_period") or {}).get(period, 0))
                if ((doc or {}).get("res") or {}).get(rid) is None and seen + cost > limit:
                    return self._over_limit(cost, limit, seen)
                continue
            state = entry.get("state")
            if state == REFUNDED:                                         # T1r
                stored = isinstance(entry.get("cost"), int) and not isinstance(entry.get("cost"), bool) \
                    and entry.get("cost") >= 1
                r_cost = entry["cost"] if stored else cost   # the cost decided for this request id is reused
                r_effort, r_reason = (entry.get("effort"), entry.get("cost_reason")) if stored else (effort, cost_reason)
                limit = int(self.limit_for(owner_key, now, owner_type))
                if r_cost > limit:
                    return self._over_limit(r_cost, limit)
                attempt = uuid.uuid4().hex
                new = self._new_entry(period, attempt, chat_id, topic_key, expects_job, ev=int(entry.get("ev", 0)) + 1,
                                      cost=r_cost, effort=r_effort, cost_reason=r_reason)
                ok = self._update(
                    {"owner_key": owner_key, f"res.{rid}.state": REFUNDED, f"res.{rid}.ev": entry.get("ev"),
                     "$or": [{f"used_by_period.{period}": {"$exists": False}},
                             {f"used_by_period.{period}": {"$lte": limit - r_cost}}]},
                    {"$set": {f"res.{rid}": new}, "$inc": {f"used_by_period.{period}": r_cost},
                     "$max": {f"limit_max_by_period.{period}": limit}})
                if ok:
                    return self._ticket(owner_key, rid, new)
                again = self._read_doc(owner_key)
                cur = ((again or {}).get("res") or {}).get(rid) or {}
                seen = int(((again or {}).get("used_by_period") or {}).get(period, 0))
                if cur.get("state") == REFUNDED and cur.get("ev") == entry.get("ev") and seen + r_cost > limit:
                    return self._over_limit(r_cost, limit, seen)
                continue
            if state in (RESERVED, STARTED) and self._resumable(entry):  # T12 resume
                attempt = uuid.uuid4().hex
                ok, observed = self._cas(owner_key, rid, {"state": state, "reason": entry.get("reason"),
                                                          "attempt": entry.get("attempt"), "ev": entry.get("ev")},
                                         set_={"state": STARTED, "attempt": attempt, "started_at": self.now(),
                                               "failed_at": None},
                                         extra={f"res.{rid}.sent_once": {"$ne": True}})
                if ok:
                    cur = self._read_entry(owner_key, rid)
                    return self._ticket(owner_key, rid, cur)
                continue
            if retry_message and state in (ENQUEUED, NEEDS_REC) and not entry.get("pending_message") and \
                    (state == NEEDS_REC and entry.get("reason") == R_MSG_MISSING or
                     state == ENQUEUED and self._stale(entry, "enqueued_at")):
                if entry.get("topic_key") and entry.get("topic_key") == topic_key:
                    self._repair(owner_key, rid, entry, message=retry_message)
                    return self._decision_for(self._read_entry(owner_key, rid))
            return self.lookup_decision(owner_key, rid)
        return Decision("unavailable")

    def lookup_decision(self, owner_key, rid) -> Decision:
        decision = self.lookup(owner_key, rid)
        return self._decision_for(self._read_entry(owner_key, rid)) if decision.kind == "continue" else decision

    def _ticket(self, owner_key, rid, entry) -> Ticket:
        return Ticket(owner_key=owner_key, rid=rid, attempt=entry["attempt"], ev=entry["ev"], state=entry["state"],
                      chat_id=entry.get("chat_id"), topic_key=entry.get("topic_key"),
                      expects_job=bool(entry.get("expects_job")), period=entry.get("period"), reason=entry.get("reason"),
                      cost=_entry_cost(entry))

    # ── request steps ────────────────────────────────────────────────────────
    def start(self, t: Ticket) -> str:
        """T2 reserved -> started."""
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, observed = self._cas(t.owner_key, t.rid, t.expected(), set_={"state": STARTED, "started_at": self.now()})
            if ok:
                t.state, t.ev = STARTED, t.ev + 1
                return DONE
            outcome = classify("T2", t.expected(), observed)
            if outcome != RETRY_FRESH:
                return outcome
            t.follow(observed)
        return CONFLICT

    def self_refund(self, t: Ticket) -> bool:
        """T3s: the request itself, before any claim. Only from reserved/started."""
        if t.state not in (RESERVED, STARTED):
            return False
        ok, observed = self._cas(t.owner_key, t.rid, t.expected(),
                                 set_={"state": REFUNDED, "refunded_at": self.now(), "pending_message": None},
                                 counter={f"used_by_period.{t.period}": -t.cost})
        if ok:
            t.state = REFUNDED
            return True
        if classify("T3s", t.expected(), observed) == CLAIM_UNCONFIRMED:
            self._flag_claim_unconfirmed(t, observed)
        return False

    def _flag_claim_unconfirmed(self, t: Ticket, observed):
        """T6c: the claim was applied although the client never saw it. No enqueue."""
        log.warning(f"claim unconfirmed, not sending rid={t.rid}")
        expected = {"state": SENDING, "reason": observed.get("reason"), "attempt": t.attempt, "ev": observed.get("ev")}
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, again = self._cas(t.owner_key, t.rid, expected, set_={"claim_unconfirmed": True})
            if ok or again is None or classify("T6c", expected, again) != RETRY_FRESH:
                return
            expected["ev"] = again.get("ev")

    def claim(self, t: Ticket) -> str:
        """T4 started -> sending. DONE only when the update matched (the only way to send)."""
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            try:
                ok, observed = self._cas(t.owner_key, t.rid, t.expected(),
                                         set_={"state": SENDING, "sent_once": True, "sending_at": self.now()},
                                         extra={f"res.{t.rid}.sent_once": {"$ne": True}})
            except CreditStoreError:
                # Outcome unknown: never enqueue. Self-refund only matches if the claim did not apply.
                try:
                    self.self_refund(t)
                except CreditStoreError:
                    log.warning(f"claim unconfirmed and self-refund failed rid={t.rid}")
                return CLAIM_UNCONFIRMED
            if ok:
                t.state, t.ev = SENDING, t.ev + 1
                return DONE
            outcome = classify("T4", t.expected(), observed)
            if outcome != RETRY_FRESH:
                return outcome
            t.follow(observed)
        return CONFLICT

    def record_enqueue_failure(self, t: Ticket, payload: dict):
        """T6b: enqueue raised or was not confirmed. Never a refund."""
        payload = _clean_payload(payload)
        expected = t.expected()
        set_ = {"failed_at": self.now(), "pending_message": payload}
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, observed = self._cas(t.owner_key, t.rid, expected, set_=set_)
            if ok:
                return
            outcome = classify("T6b", expected, observed)
            if outcome == RETRY_FRESH:
                expected = {"state": observed["state"], "reason": observed.get("reason"),
                            "attempt": t.attempt, "ev": observed["ev"]}
            elif outcome == RETRY_PAYLOAD:
                expected = {"state": observed["state"], "reason": observed.get("reason"),
                            "attempt": t.attempt, "ev": observed["ev"]}
                set_ = {"pending_message": payload}
            else:
                return

    def mark_enqueued(self, t: Ticket) -> str:
        """T6a with E1 (the original's own acknowledged enqueue)."""
        expected = t.expected()
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, observed = self._cas(t.owner_key, t.rid, expected,
                                     set_={"state": ENQUEUED, "reason": None, "enqueued_at": self.now()})
            if ok:
                t.state, t.reason, t.ev = ENQUEUED, None, expected["ev"] + 1
                return DONE
            outcome = classify("T6a", expected, observed)
            if outcome == RETRY_FRESH:
                expected = {"state": observed["state"], "reason": observed.get("reason"),
                            "attempt": t.attempt, "ev": observed["ev"]}
                continue
            if observed is not None:
                t.follow(observed)
            return outcome
        return CONFLICT

    def commit(self, t: Ticket, reason=R_NORMAL) -> str:
        """T8 with E3 (our own acknowledged message save)."""
        expected = t.expected()
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, observed = self._cas(t.owner_key, t.rid, expected,
                                     set_={"state": COMMITTED, "reason": reason, "pending_message": None,
                                           "committed_at": self.now()})
            if ok:
                t.state, t.reason, t.ev = COMMITTED, reason, expected["ev"] + 1
                return DONE
            outcome = classify("T8", expected, observed)
            if outcome == RETRY_FRESH:
                expected = {"state": observed["state"], "reason": observed.get("reason"),
                            "attempt": t.attempt, "ev": observed["ev"]}
                continue
            if observed is not None:
                t.follow(observed)
            return outcome
        return CONFLICT

    def record_message_failure(self, t: Ticket, payload: dict, state_preserving=False) -> str:
        """T9 (or a payload-only store when a close is in progress)."""
        payload = _clean_payload(payload)
        expected = t.expected()
        set_ = {"pending_message": payload} if state_preserving else {"state": MESSAGE_FAILED, "pending_message": payload}
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, observed = self._cas(t.owner_key, t.rid, expected, set_=set_)
            if ok:
                if not state_preserving:
                    t.state = MESSAGE_FAILED
                t.ev = expected["ev"] + 1
                return DONE
            outcome = classify("T9", expected, observed)
            if outcome in (RETRY_FRESH, RETRY_PAYLOAD):
                if outcome == RETRY_PAYLOAD:
                    set_ = {"pending_message": payload}
                expected = {"state": observed["state"], "reason": observed.get("reason"),
                            "attempt": t.attempt, "ev": observed["ev"]}
                continue
            return outcome
        return CONFLICT

    def run(self, t: Ticket, enqueue_fn, payload: dict) -> Decision:
        """The full accepted-search path after a successful reservation."""
        payload = _clean_payload(payload)
        if t.state == RESERVED:
            outcome = self.start(t)
            if outcome != DONE:
                return self._superseded_decision(t)
        self.checkpoint("before_claim", ticket=t)
        outcome = self.claim(t)
        if outcome == CLAIM_UNCONFIRMED:
            return Decision("unavailable", cleanup_ok=(t.state == REFUNDED))
        if outcome != DONE:
            return self._superseded_decision(t)
        self.checkpoint("after_claim", ticket=t)
        if t.expects_job:
            result, error = None, None
            try:
                with _enqueue_timeout(_setting("ENQUEUE_TIMEOUT_SECONDS", 30)):
                    result = enqueue_fn(t.rid)
            except Exception as exc:
                error = exc
            self.checkpoint("after_enqueue", ticket=t, result=result, error=error)
            if error is not None or not is_e1(result):
                log.warning(f"enqueue not confirmed rid={t.rid} error={type(error).__name__ if error else None}")
                try:
                    self.record_enqueue_failure(t, payload)
                except CreditStoreError:
                    log.warning(f"recording the enqueue failure failed rid={t.rid}")
                return Decision("unavailable", cleanup_ok=False)
            t.e1 = True
            try:
                outcome = self.mark_enqueued(t)
            except CreditStoreError:
                outcome = "unknown"
            if outcome == COMPLETED:
                return Decision("done", chat_id=t.chat_id)
            if outcome == TERMINAL_NO_DELIVERY:
                return Decision("redirect")
            if outcome == TERMINAL:
                return self.late_delivery(t, payload)
            if outcome == CONFLICT:
                log.error(f"credit conflict at T6a rid={t.rid}")
                return Decision("redirect", chat_id=t.chat_id)
        self.checkpoint("before_message", ticket=t)
        return self._save_and_commit(t, payload)

    def _superseded_decision(self, t: Ticket) -> Decision:
        """This attempt lost its reservation (refunded, resumed by a retry, or a
        conflict). It never enqueues; answer by what the entry shows now."""
        try:
            entry = self._read_entry(t.owner_key, t.rid)
        except CreditStoreError:
            return Decision("unavailable")
        if entry is None or entry.get("state") == REFUNDED:
            return Decision("unavailable")
        return self._decision_for(entry)

    def _save_and_commit(self, t: Ticket, payload: dict) -> Decision:
        try:
            saved = self.save_message(t.chat_id, t.owner_key, credit_request_id=t.rid, **payload)
        except Exception as exc:
            log.warning(f"message save raised rid={t.rid}: {type(exc).__name__}")
            saved = None
        self.checkpoint("after_message", ticket=t, saved=saved)
        if saved is True:
            try:
                outcome = self.commit(t)
            except CreditStoreError:
                return Decision("done", chat_id=t.chat_id)   # recovery finds the message (E3) later
            if outcome == TERMINAL:
                self._audit(t, {"late_landing_at": self.now(), "by": "original", "note": "saved before close"})
            return Decision("done", chat_id=t.chat_id)
        if saved is None:
            self._safe(lambda: self.record_message_failure(t, payload))
            return Decision("redirect")
        view = self._chat_view(t.owner_key, t.chat_id, t.rid)
        if view is None:                                    # the chat no longer exists for this owner
            self._safe(lambda: self.record_message_failure(t, payload))
            self._safe(lambda: self.recover_entry(t.owner_key, t.rid))
            return Decision("redirect")
        if view["has_message"]:
            self._safe(lambda: self.commit(t, reason=R_ALREADY))
            return Decision("done", chat_id=t.chat_id)
        if view["marker"]:                                  # CLOSE_IN_PROGRESS: never deliver around it
            self._safe(lambda: self.record_message_failure(t, payload, state_preserving=True))
            entry = self._read_entry(t.owner_key, t.rid)
            if t.e1 and entry and entry.get("state") == COMMITTED and entry.get("reason") == R_MANUAL:
                return self.late_delivery(t, payload)
            log.warning(f"close in progress, message not delivered rid={t.rid}")
            return Decision("reconciliation", chat_id=t.chat_id, message=RECONCILIATION_MESSAGE)
        log.error(f"credit conflict at message save rid={t.rid}")
        return Decision("redirect", chat_id=t.chat_id)

    def late_delivery(self, t: Ticket, payload: dict) -> Decision:
        """S-late: only the original with E1, only after its own read shows a
        completed manual_close of this attempt, only into the entry's chat."""
        if not t.e1:
            return Decision("redirect", chat_id=t.chat_id)
        entry = self._read_entry(t.owner_key, t.rid)
        if not (entry and entry.get("state") == COMMITTED and entry.get("reason") == R_MANUAL
                and entry.get("attempt") == t.attempt and entry.get("close_token")
                and entry.get("chat_id") and entry.get("chat_id") == t.chat_id):
            log.error(f"late delivery refused rid={t.rid}: entry does not show this attempt's completed close")
            return Decision("redirect", chat_id=t.chat_id)
        try:
            saved = self.save_message(entry["chat_id"], t.owner_key, credit_request_id=t.rid,
                                      late_delivery=(t.attempt, entry["close_token"]), **_clean_payload(payload))
        except Exception:
            saved = False
        if saved:
            t.follow(entry)
            self._audit(t, {"late_delivery_at": self.now(), "attempt": t.attempt, "chat_id": entry["chat_id"]})
        return Decision("redirect", chat_id=t.chat_id)

    def _audit(self, t: Ticket, note: dict):
        """T15: audit note only — state, reason and attempt are unchanged."""
        try:
            entry = self._read_entry(t.owner_key, t.rid)
            for _ in range(_setting("CAS_MAX_TRIES", 3)):
                if not entry:
                    return
                expected = {"state": entry["state"], "reason": entry.get("reason"), "attempt": t.attempt, "ev": entry["ev"]}
                ok, entry2 = self._cas(t.owner_key, t.rid, expected, push={"audit": note})
                if ok or entry2 is None or classify("T15", expected, entry2) != RETRY_FRESH:
                    return
                entry = entry2
        except CreditStoreError:
            log.warning(f"audit note failed rid={t.rid}")

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except CreditStoreError as exc:
            log.warning(f"credit write failed: {exc}")
            return None

    # ── recovery (lazy, per owner) ───────────────────────────────────────────
    def recover(self, owner_key) -> None:
        doc = self._read_doc(owner_key)
        if not doc:
            return
        for rid in list((doc.get("res") or {}).keys()):
            try:
                self.recover_entry(owner_key, rid)
            except CreditStoreError as exc:
                log.warning(f"recovery step failed rid={rid}: {exc}")
        self._prune(owner_key)

    def recover_entry(self, owner_key, rid) -> None:
        entry = self._read_entry(owner_key, rid)
        if entry is None:
            return
        state = entry.get("state")
        if state in (RESERVED, STARTED) and self._stale(entry, "at", "started_at"):
            self._cas(owner_key, rid, _exp(entry), set_={"state": REFUNDED, "refunded_at": self.now(),
                                                          "pending_message": None},
                      counter={f"used_by_period.{entry['period']}": -_entry_cost(entry)},
                      extra={f"res.{rid}.sent_once": {"$ne": True}})                          # T3
            return
        if state == COMMITTED and entry.get("reason") == R_MANUAL:
            self._recovery_c3(owner_key, rid, entry)
            return
        self._advance(owner_key, rid, entry, allow_retry_inputs=False)

    def _advance(self, owner_key, rid, entry, allow_retry_inputs=False):
        """Evidence-only steps shared by recovery and same-key retries (never an enqueue)."""
        for _ in range(4):
            if entry is None:
                return
            state, reason = entry.get("state"), entry.get("reason")
            if state == SENDING:
                if not (entry.get("failed_at") or self._stale(entry, "sending_at")):
                    return
                if not entry.get("expects_job"):
                    view = self._chat_view(owner_key, entry.get("chat_id"), rid)
                    if view and view["has_message"]:
                        self._cas(owner_key, rid, _exp(entry), set_={"state": COMMITTED, "reason": R_ALREADY,
                                                                      "pending_message": None})
                    elif self._stale(entry, "sending_at"):
                        self._cas(owner_key, rid, _exp(entry), set_={"state": NEEDS_REC, "reason": R_AMBIGUOUS})
                    return
                if self._has_e2(rid, entry):                                                # T7 -> enqueued
                    ok, _ = self._cas(owner_key, rid, _exp(entry), set_={"state": ENQUEUED, "reason": None,
                                                                          "enqueued_at": self.now()})
                else:                                                                       # T7 -> ambiguous
                    ok, _ = self._cas(owner_key, rid, _exp(entry), set_={"state": NEEDS_REC, "reason": R_AMBIGUOUS})
                if not ok:
                    return
            elif state == NEEDS_REC and reason == R_AMBIGUOUS:
                if not self._has_e2(rid, entry):
                    return
                ok, _ = self._cas(owner_key, rid, _exp(entry), set_={"state": ENQUEUED, "reason": None,
                                                                      "enqueued_at": self.now()})   # T11
                if not ok:
                    return
            elif state in (MESSAGE_FAILED, ENQUEUED) or (state == NEEDS_REC and reason == R_MSG_MISSING):
                if entry.get("pending_message"):
                    self._repair(owner_key, rid, entry)                                        # T10
                    return
                view = self._chat_view(owner_key, entry.get("chat_id"), rid)
                if view and view["has_message"]:
                    self._cas(owner_key, rid, _exp(entry), set_={"state": COMMITTED, "reason": R_ALREADY})
                    return
                if state == ENQUEUED and self._stale(entry, "enqueued_at"):                    # stale enqueued
                    self._cas(owner_key, rid, _exp(entry), set_={"state": NEEDS_REC, "reason": R_MSG_MISSING})
                return
            else:
                return
            entry = self._read_entry(owner_key, rid)

    def _repair(self, owner_key, rid, entry, message=None) -> bool:
        """T10: idempotent and owner-scoped; only ever writes to the entry's own chat_id; never creates a chat."""
        payload = _clean_payload(message if message is not None else entry.get("pending_message"))
        chat_id = entry.get("chat_id")
        if not payload:
            return False
        if not chat_id:
            log.error(f"credit conflict: entry has no chat_id rid={rid}")
            return False
        try:
            saved = self.save_message(chat_id, owner_key, credit_request_id=rid, **payload)
        except Exception as exc:
            log.warning(f"repair save raised rid={rid}: {type(exc).__name__}")
            return False
        if saved:
            ok, _ = self._cas(owner_key, rid, _exp(entry), set_={"state": COMMITTED, "reason": R_REPAIRED,
                                                                  "pending_message": None})
            return ok
        view = self._chat_view(owner_key, chat_id, rid)
        if view is None:
            ok, _ = self._cas(owner_key, rid, _exp(entry), set_={"state": COMMITTED, "reason": R_CHAT_MISSING,
                                                                  "pending_message": None, "topic_key": None})
            return ok
        if view["has_message"]:
            ok, _ = self._cas(owner_key, rid, _exp(entry), set_={"state": COMMITTED, "reason": R_ALREADY,
                                                                  "pending_message": None})
            return ok
        if view["marker"]:
            log.warning(f"close_marker_present: repair blocked rid={rid}")
        return False

    def _prune(self, owner_key):
        """T14: committed entries (archived) and refunded markers older than last month."""
        doc = self._read_doc(owner_key)
        if not doc:
            return
        cutoff = previous_period(period_key(self.now()))
        for rid, entry in (doc.get("res") or {}).items():
            period, state = entry.get("period") or "", entry.get("state")
            if period >= cutoff or state not in (COMMITTED, REFUNDED):
                continue
            filt = {"owner_key": owner_key, f"res.{rid}.state": state, f"res.{rid}.ev": entry.get("ev"),
                    f"res.{rid}.period": period}
            update = {"$unset": {f"res.{rid}": ""}}
            if state == COMMITTED:
                update["$inc"] = {f"archived_by_period.{period}": _entry_cost(entry)}
            try:
                self._update(filt, update)
            except CreditStoreError as exc:
                log.warning(f"prune failed rid={rid}: {exc}")

    # ── status for the API / home page ───────────────────────────────────────
    def status(self, owner_key, owner_type=None) -> dict:
        now = self.now()
        base = {"enforced": True, "limit": int(self.limit()), "period": period_key(now),
                "resets_at": period_resets_at(now).isoformat()}
        try:
            plan = self.plan_for(owner_key, now, owner_type)
            if plan != PLAN_FREE:
                base["limit"] = int(plan_credits(plan))
            base["plan"] = plan
            self.recover(owner_key)
            doc = self._read_doc(owner_key) or {}
        except CreditStoreError:
            return {**base, "status": "unavailable", "used": None, "remaining": None,
                    "needs_reconciliation": None, "close_in_progress": None}
        used = int((doc.get("used_by_period") or {}).get(base["period"], 0))
        entries = (doc.get("res") or {})
        needs = [rid for rid, e in entries.items() if e.get("state") == NEEDS_REC]
        closing = 0
        for rid in needs:
            try:
                view = self._chat_view(owner_key, entries[rid].get("chat_id"), rid)
            except CreditStoreError:
                view = None
            if view and view["marker"]:
                closing += 1
        return {**base, "status": "ok", "used": used, "remaining": max(0, base["limit"] - used),
                "needs_reconciliation": len(needs), "close_in_progress": closing,
                "attribution_mode": attribution_mode()}

    # ── operator tools (no endpoint) ─────────────────────────────────────────
    def list_needs_reconciliation(self, owner_key) -> list:
        doc = self._read_doc(owner_key) or {}
        out = []
        for rid, entry in (doc.get("res") or {}).items():
            if entry.get("state") != NEEDS_REC:
                continue
            view = self._chat_view(owner_key, entry.get("chat_id"), rid)
            out.append({"rid": rid, "reason": entry.get("reason"), "ev": entry.get("ev"), "period": entry.get("period"),
                        "attribution_mode": attribution_mode(),
                        "chat_id": entry.get("chat_id"), "claim_unconfirmed": bool(entry.get("claim_unconfirmed")),
                        "message_present": bool(view and view["has_message"]),
                        "close_marker_present": bool(view and view["marker"])})
        return out

    def resolve(self, owner_key, rid, action, expected_ev=None, force=False) -> dict:
        """Operator decisions. There is no refund: 'close' or 'abort_close' only."""
        if action == "close":
            return self._close(owner_key, rid, expected_ev, force)
        if action == "abort_close":
            return self._abort_close(owner_key, rid)
        raise ValueError(f"unsupported resolve action {action!r} (refunds are not available)")

    def _marker_consistent(self, entry, marker) -> bool:
        return bool(entry and marker and entry.get("state") == COMMITTED and entry.get("reason") == R_MANUAL
                    and entry.get("close_token") and marker.get("phase") == "closed"
                    and marker.get("attempt") == entry.get("attempt")
                    and marker.get("close_token") == entry.get("close_token"))

    def _marker_stale(self, marker) -> bool:
        at = _aware(marker.get("at"))
        return at is not None and (self.now() - at).total_seconds() >= _setting("RESERVATION_STALE_SECONDS", 900)

    def _take_over(self, owner_key, chat_id, rid, marker, new_token) -> bool:
        try:
            r = self.chats.update_one(
                {"chat_id": chat_id, "owner_key": owner_key, f"credit_close.{rid}.phase": "closing",
                 f"credit_close.{rid}.token": marker.get("token"), f"credit_close.{rid}.at": marker.get("at")},
                {"$set": {f"credit_close.{rid}.token": new_token, f"credit_close.{rid}.at": self.now(),
                          f"credit_close.{rid}.taken_over_from": marker.get("token")}})
        except Exception as exc:
            raise CreditStoreError(f"marker takeover failed: {type(exc).__name__}") from exc
        return r.matched_count == 1

    def _remove_marker(self, owner_key, chat_id, rid, token, phase="closing") -> bool:
        try:
            r = self.chats.update_one({"chat_id": chat_id, "owner_key": owner_key,
                                       f"credit_close.{rid}.token": token, f"credit_close.{rid}.phase": phase},
                                      {"$unset": {f"credit_close.{rid}": ""}})
        except Exception as exc:
            raise CreditStoreError(f"marker removal failed: {type(exc).__name__}") from exc
        return r.matched_count == 1

    def _bump(self, owner_key, rid, entry) -> bool:
        """State-preserving version bump (invalidates any in-flight C2)."""
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            ok, again = self._cas(owner_key, rid, _exp(entry), set_={"close_aborted_at": self.now()})
            if ok:
                return True
            if again is None or again.get("state") != entry.get("state") or again.get("reason") != entry.get("reason"):
                return False
            entry = again
        return False

    def _close(self, owner_key, rid, expected_ev, force) -> dict:
        entry = self._read_entry(owner_key, rid)
        if not entry or entry.get("state") != NEEDS_REC or (expected_ev is not None and entry.get("ev") != expected_ev):
            return {"result": "superseded", "current": entry}
        token, chat_id = uuid.uuid4().hex, entry.get("chat_id")
        holding = False
        if chat_id and self._chat_view(owner_key, chat_id, rid) is not None:
            try:                                                                         # C1
                r = self.chats.update_one(
                    {"chat_id": chat_id, "owner_key": owner_key, f"credit_close.{rid}": {"$exists": False},
                     "messages.credit_request_id": {"$ne": rid}},
                    {"$set": {f"credit_close.{rid}": {"phase": "closing", "token": token, "at": self.now()}}})
            except Exception as exc:
                raise CreditStoreError(f"marker write failed: {type(exc).__name__}") from exc
            if r.matched_count == 1:
                holding = True
            else:
                view = self._chat_view(owner_key, chat_id, rid)
                if view is None:
                    pass                                                     # chat deleted: close without a marker
                elif view["has_message"]:
                    fresh = self._read_entry(owner_key, rid)
                    if fresh and fresh.get("state") == NEEDS_REC:
                        self._cas(owner_key, rid, _exp(fresh), set_={"state": COMMITTED, "reason": R_ALREADY,
                                                                      "pending_message": None})
                    return {"result": "already_present", "current": self._read_entry(owner_key, rid)}
                elif view["marker"] and view["marker"].get("phase") == "closing":
                    if not (force or self._marker_stale(view["marker"])):
                        return {"result": "close_in_progress"}
                    if not self._take_over(owner_key, chat_id, rid, view["marker"], token):
                        return {"result": "close_in_progress"}
                    holding = True
                elif view["marker"] and view["marker"].get("phase") == "closed":
                    fresh = self._read_entry(owner_key, rid)
                    if self._marker_consistent(fresh, view["marker"]):
                        return {"result": "already_closed"}
                    log.error(f"close_marker_inconsistent rid={rid}")
                    return {"result": "close_marker_inconsistent"}
        # C2
        c2_attempt = entry.get("attempt")   # fixed after sent_once: every needs_reconciliation entry was sent
        for _ in range(_setting("CAS_MAX_TRIES", 3)):
            fresh = self._read_entry(owner_key, rid)
            if fresh and fresh.get("state") == NEEDS_REC:
                ok, observed = self._cas(owner_key, rid, _exp(fresh),
                                         set_={"state": COMMITTED, "reason": R_MANUAL, "close_token": token,
                                               "closed_at": self.now()})
                if ok:
                    break
                if observed is not None and classify("T13", _exp(fresh), observed) == RETRY_FRESH:
                    continue
                fresh = observed
            if fresh and fresh.get("state") == COMMITTED and fresh.get("reason") == R_MANUAL and fresh.get("close_token"):
                break
            if fresh and fresh.get("state") not in (COMMITTED, REFUNDED):
                self._bump(owner_key, rid, fresh)
            if holding:
                self._remove_marker(owner_key, chat_id, rid, token)
            return {"result": "superseded", "current": self._read_entry(owner_key, rid)}
        else:
            if holding:
                self._remove_marker(owner_key, chat_id, rid, token)
            return {"result": "superseded", "current": self._read_entry(owner_key, rid)}
        if holding:
            self.complete_c3(owner_key, rid, chat_id, token, c2_attempt)
        return {"result": "closed", "current": self._read_entry(owner_key, rid)}

    def complete_c3(self, owner_key, rid, chat_id, token_held, expected_attempt) -> bool:
        """C3 with the C3i safeguard: the credit entry is re-read immediately
        before the write and must show this exact completed close."""
        view = self._chat_view(owner_key, chat_id, rid)
        marker = (view or {}).get("marker")
        if not marker or marker.get("phase") != "closing" or marker.get("token") != token_held:
            log.error(f"close_precheck_failed rid={rid}: marker not held as closing")
            return False
        entry = self._read_entry(owner_key, rid)                          # immediately before the C3 write
        mismatched = []
        if not entry:
            mismatched.append("entry_missing")
        else:
            if entry.get("state") != COMMITTED:
                mismatched.append("state")
            if entry.get("reason") != R_MANUAL:
                mismatched.append("reason")
            if not expected_attempt or entry.get("attempt") != expected_attempt:
                mismatched.append("attempt")
            if entry.get("chat_id") != chat_id:
                mismatched.append("chat_id")
            if not entry.get("close_token"):
                mismatched.append("close_token")
        if mismatched:
            log.error(f"close_precheck_failed rid={rid} mismatched={','.join(mismatched)}")
            return False
        try:
            r = self.chats.update_one(
                {"chat_id": chat_id, "owner_key": owner_key, f"credit_close.{rid}.phase": "closing",
                 f"credit_close.{rid}.token": token_held},
                {"$set": {f"credit_close.{rid}.phase": "closed", f"credit_close.{rid}.attempt": entry["attempt"],
                          f"credit_close.{rid}.close_token": entry["close_token"],
                          f"credit_close.{rid}.closed_at": self.now()}})
        except Exception as exc:
            raise CreditStoreError(f"C3 write failed: {type(exc).__name__}") from exc
        return r.matched_count == 1

    def _recovery_c3(self, owner_key, rid, entry):
        """Recovery may only finish C3 for an entry that is already a completed
        manual_close, and only when the closing marker is at least 900 s old."""
        chat_id = entry.get("chat_id")
        view = self._chat_view(owner_key, chat_id, rid)
        marker = (view or {}).get("marker")
        if not marker or marker.get("phase") != "closing" or not self._marker_stale(marker):
            return
        if not (entry.get("reason") == R_MANUAL and entry.get("close_token")):
            return
        token = uuid.uuid4().hex
        if self._take_over(owner_key, chat_id, rid, marker, token):
            fresh = self._read_entry(owner_key, rid)
            if fresh and fresh.get("state") == COMMITTED and fresh.get("reason") == R_MANUAL:
                self.complete_c3(owner_key, rid, chat_id, token, fresh.get("attempt"))

    def _abort_close(self, owner_key, rid) -> dict:
        entry = self._read_entry(owner_key, rid)
        chat_id = (entry or {}).get("chat_id")
        view = self._chat_view(owner_key, chat_id, rid) if chat_id else None
        marker = (view or {}).get("marker")
        if not marker:
            return {"result": "no_marker"}
        if self._marker_consistent(entry, marker):
            return {"result": "refused_consistent_close"}
        if marker.get("phase") == "closing" and entry and entry.get("state") == COMMITTED \
                and entry.get("reason") == R_MANUAL and entry.get("close_token"):
            token = uuid.uuid4().hex
            if self._take_over(owner_key, chat_id, rid, marker, token):
                self.complete_c3(owner_key, rid, chat_id, token, entry.get("attempt"))
                return {"result": "closed"}
            return {"result": "close_in_progress"}
        if entry and entry.get("state") not in (COMMITTED, REFUNDED):
            self._bump(owner_key, rid, entry)
        removed = self._remove_marker(owner_key, chat_id, rid, marker.get("token"), marker.get("phase"))
        return {"result": "aborted" if removed else "close_in_progress"}


def _exp(entry) -> dict:
    return {"state": entry.get("state"), "reason": entry.get("reason"), "attempt": entry.get("attempt"),
            "ev": entry.get("ev")}


def _clean_payload(payload):
    """Stored message arguments never carry a chat id or credit routing fields."""
    if not payload:
        return {} if payload is not None else None
    return {k: v for k, v in dict(payload).items()
            if k not in ("chat_id", "owner_key", "credit_request_id", "late_delivery")}


def default_store():
    """The production store, or None when enforcement is disabled (kill switch)."""
    if not enforcement_enabled():
        return None
    import database
    import index
    return CreditStore(database.credit_usage_collection, index.chats_collection, database.jobs_collection,
                       index.add_search_to_chat, plans=getattr(database, "plans_collection", None))
