"""Active lead intent: Historical Search + a separate Live Reddit search, combined into ONE answer.

For a signed-in owner's plain lead search that clearly asks for people who are ACTIVELY looking for something (a CRM, a
recommendation, an alternative, a solution), this module lets two independent branches contribute to the same answer:

  Branch A - the EXISTING saved-post (historical) search and its normal evidence analysis. Only one search parameter changes,
             and only for this request: its publication window becomes the last N minutes (default 30, never more than 40),
             supplied the same way a user-typed range is (the message's `time_range`), in memory, at stream time.
  Branch B - the separate Live Reddit web search of reddit_live.py (same claim, budgets, cooldown, in-flight guard, URL
             validation and idempotency), started at the same moment and bounded by its own wait.

What the combined answer is - and is not
    * Historical rows keep their normal analysis, ranking and integrity checks. Nothing here touches them.
    * A live result is a URL and (maybe) a title. Flintel has NOT read the post, so it is shown as an UNVERIFIED candidate:
      ranked by how well its TITLE matches the request, never given a buying-intent score, never described as verified,
      never given a date, and never claimed to fall inside the historical window.
    * Live candidates are merged AFTER the answer is finalised, as one clearly labelled block inside the same answer
      (`live_candidates`). They are deliberately NOT put into the pool the answer-integrity layer counts, so its accounting
      (retrieved / unique / unverified / verified) stays exactly what the historical branch produced.
    * Each branch fails on its own: a live failure or zero result leaves the historical answer untouched, and an empty or
      failed historical branch never stops the live branch.

Everything is OFF unless ACTIVE_INTENT_ENABLED is true AND the Live links feature is itself active, and every method of `Run`
is a no-op returning its input when the search is not eligible or anything goes wrong.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import config
import reddit_live
import timerange

log = logging.getLogger("flintel.active_intent")

SOURCE_HISTORICAL = "historical"
SOURCE_LIVE = "live_web_search"
DEFAULT_WINDOW_MINUTES = 30
MAX_WINDOW_MINUTES = 40
DEFAULT_LIVE_WAIT_SECONDS = 30
MAX_LIVE_WAIT_SECONDS = 60
MAX_LIVE_CANDIDATES = 8

# Overridable in tests: how the Live branch obtains its database/HTTP dependencies (default: the real ones).
DEPS_FACTORY = None


# ── configuration ────────────────────────────────────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    return getattr(config, "ACTIVE_INTENT_ENABLED", False) is True


def window_minutes() -> int:
    """The recent window in minutes: configured value, never more than MAX_WINDOW_MINUTES; anything invalid -> the default."""
    v = getattr(config, "ACTIVE_INTENT_WINDOW_MINUTES", DEFAULT_WINDOW_MINUTES)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        v = DEFAULT_WINDOW_MINUTES
    return min(v, MAX_WINDOW_MINUTES)


def live_wait_seconds() -> int:
    v = getattr(config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", DEFAULT_LIVE_WAIT_SECONDS)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        v = DEFAULT_LIVE_WAIT_SECONDS
    return min(v, MAX_LIVE_WAIT_SECONDS)


def recent_window(now: datetime) -> "timerange.TimeRange":
    """The half-open [now - N min, now) range, in the same type a user-typed 'last N minutes' produces."""
    now = timerange.to_utc(now) or datetime.now(timezone.utc)
    n = window_minutes()
    return timerange.TimeRange(start=now - timedelta(minutes=n), end=now, tz_name="UTC", kind="rolling",
                               phrase=f"last {n} minutes", capped=True,
                               note="Applied automatically to this search of saved posts because it asks for people who are "
                                    "actively looking; it does not apply to the separate live web search.")


# ── the narrow query-text predicate (NOT a model call, NOT the router) ──────────────────────────────────────────────────
# It asks two things of the request text: (1) it is a request to LOCATE other people/businesses, and (2) it describes them as
# actively looking / asking for a recommendation / needing a solution. Ordinary informational or educational questions
# ("what is a CRM", "how does a CRM work", "best CRM for ...") never satisfy (1) and so never trigger it.
_LEAD_VERB = re.compile(r"\b(?:find|show|list|get|search|look\s+for|discover|identify|surface|pull|give\s+me|who)\b")
_TARGET = re.compile(r"\b(?:who|people|persons?|users?|redditors?|businesses|business\s+owners?|companies|company|founders?|"
                     r"owners?|startups?|teams?|folks|someone|anyone|buyers?|customers?|prospects?|leads?|agencies|"
                     r"managers|sellers|shops?|stores?)\b")
_ACTIVE = re.compile(
    r"(?:\b(?:actively|currently|right\s+now|at\s+the\s+moment)\s+(?:looking|searching|seeking|shopping|asking|trying|"
    r"evaluating|comparing|hunting|needing|wanting)\b"
    r"|\b(?:looking|searching|shopping|hunting)\s+for\b"
    r"|\b(?:looking|trying|wanting|wants?|needs?|needing|planning|ready)\s+to\s+(?:buy|find|get|switch|replace|choose|pick|adopt|migrate|upgrade|compare)\b"
    r"|\b(?:in\s+the\s+market|in\s+need)\s+(?:for|of)\b"
    r"|\b(?:need|needs|needing|want|wants|require|requires)\s+(?:a|an|to\s+find|to\s+get|to\s+choose|help\s+(?:finding|choosing|picking))\b"
    r"|\b(?:ask(?:ing)?|request(?:ing)?)\s+(?:for\s+)?(?:[\w-]+\s+){0,3}?(?:recommendations?|suggestions?|advice)\b"
    r"|\brecommendations?\s+(?:for|on)\b"
    r"|\bwhich\s+[\w\s/&+.-]{1,40}?\s+(?:should|to)\s+(?:i|we|they|use|choose|pick|buy|get)\b"
    r"|\balternatives?\s+to\b"
    r"|\b(?:switch(?:ing)?|migrat(?:e|ing)|mov(?:e|ing))\s+(?:away\s+)?from\b"
    r"|\bseeking\s+(?:a|an|the)?\s*(?:solution|tool|software|app|platform)\b)")
_NOT_LEADS = re.compile(r"\b(?:tutorials?|courses?|guides?|explain|explanation|what\s+is|history\s+of|definition|salary|"
                        r"salaries|careers?|jobs?|hiring|interviews?|certifications?|training|degree)\b")


def is_active_lead_intent(text):
    """(detected, reason). Pure and deterministic. `text` is the user's request (the router's standalone restatement when it
    exists, else the original). Deliberately narrow: when in doubt the answer is False and nothing changes."""
    if not isinstance(text, str) or not text.strip():
        return False, "empty"
    t = re.sub(r"\s+", " ", text.lower()).strip()
    if _NOT_LEADS.search(t):
        return False, "informational"
    if not _LEAD_VERB.search(t) or not _TARGET.search(t):
        return False, "not_a_request_to_find_people"
    if not _ACTIVE.search(t):
        return False, "no_active_intent_phrase"
    return True, "active_lead_intent"


# ── common evidence structure: normalise, deduplicate, rank ─────────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """One piece of evidence from either branch, with its provenance."""
    source_type: str                     # "historical" | "live_web_search"
    url: str
    title: object = None                 # str | None
    has_content: bool = False            # True only when the post's text was actually retrieved
    published_at: object = None          # ISO string, only when a stored date exists (never for live)
    verification: str = "unverified"     # historical: the existing integrity status; live: always "unverified"
    relevance: object = None             # live only: title match to the request, 0..1 (NOT an intent score)
    also_found_by: list = field(default_factory=list)


def normalize_historical(rows):
    """Evidence items for the historical rows (read-only use of the existing integrity status function)."""
    import answer_integrity
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("post_url"):
            continue
        when = timerange.post_published_at(r)
        out.append(Evidence(source_type=SOURCE_HISTORICAL, url=r["post_url"], title=r.get("title"),
                            has_content=bool((r.get("post_text") or "").strip()),
                            published_at=when.isoformat() if when else None,
                            verification=answer_integrity.verification_status(r)))
    return out


def normalize_live(links):
    out = []
    for l in links or []:
        url = reddit_live.valid_reddit_url(l.get("url")) if isinstance(l, dict) else None
        if url:
            out.append(Evidence(source_type=SOURCE_LIVE, url=url, title=reddit_live.clean_title(l.get("title")),
                                has_content=False, published_at=None, verification="unverified"))
    return out


_STOP = frozenset("the a an and or for to of in on at with from who are is be that this you your their they them people "
                  "find show list get any all some looking asking actively currently business businesses company companies "
                  "reddit posts post threads thread need needs want wants which what should".split())


def _terms(*texts):
    seen, out = set(), []
    for text in texts:
        for w in re.findall(r"[a-z0-9]+", (text or "").lower() if isinstance(text, str) else ""):
            if len(w) >= 3 and w not in _STOP and w not in seen:
                seen.add(w)
                out.append(w)
    return out


def title_relevance(terms, title) -> float:
    """Share of the request's distinctive terms that appear in the TITLE (0..1). A missing title scores 0. This is lexical
    overlap on a title and nothing more: it says nothing about intent, content or recency."""
    if not terms or not isinstance(title, str) or not title.strip():
        return 0.0
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    return round(sum(1 for t in terms if t in words) / len(terms), 2)


def combine(historical, live, terms):
    """(live_candidates, duplicates_removed). Drops every live item whose post is already among the historical evidence (the
    historical item wins and is annotated), then ranks the rest by title relevance, best first, ties in the order returned."""
    by_key = {reddit_live.url_key(e.url): e for e in historical}
    kept, dups, seen = [], 0, set()
    for e in live:
        key = reddit_live.url_key(e.url)
        if key in by_key:
            dups += 1
            if SOURCE_LIVE not in by_key[key].also_found_by:
                by_key[key].also_found_by.append(SOURCE_LIVE)
            continue
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        e.relevance = title_relevance(terms, e.title)
        kept.append(e)
    order = sorted(range(len(kept)), key=lambda i: (-(kept[i].relevance or 0.0), i))
    return [kept[i] for i in order][:MAX_LIVE_CANDIDATES], dups


# ── the block that goes into the answer ───────────────────────────────────────────────────────────────────────────────────

def build_block(candidates, *, historical_count, window, duplicates_removed, historical_state):
    """The `live_candidates` object. Every sentence in it is written here, never by a model, and says only what is true."""
    n = len(candidates)
    parts = [f"Live web search: {n} Reddit link{'s' if n != 1 else ''} found by a separate live web search. "
             "Only the link and title were returned, so Flintel has not read these posts, verified their dates or assessed "
             f"whether the authors are buying. They were not limited to the last {window} minutes that was applied to "
             "Flintel's saved posts, and they are ranked by how well their titles match your request, nothing more."]
    if historical_state == "timed_out":
        parts.append("The search of saved posts did not finish in time, so this answer may be missing saved-post evidence.")
    elif historical_count:
        parts.append("Saved-post results are analysed in the answer above and are separate from these candidates.")
    else:
        parts.append("No analysed saved-post evidence was available for this answer, so these unverified candidates are "
                     "the only leads found.")
    if duplicates_removed:
        parts.append(f"{duplicates_removed} duplicate link{'s' if duplicates_removed != 1 else ''} already covered by the "
                     "saved posts ha" + ("ve" if duplicates_removed != 1 else "s") + " been left out.")
    return {
        "source": SOURCE_LIVE, "verification": "unverified_title_and_url_only", "historical_window_minutes": window,
        "live_results_limited_to_window": False, "statement": " ".join(parts),
        "items": [{"rank": i + 1, "title": c.title, "url": c.url, "source": SOURCE_LIVE,
                   "verification_status": "unverified", "evidence": "title_and_url_only", "title_relevance": c.relevance}
                  for i, c in enumerate(candidates)],
    }


def attach(answer_text, block):
    """The answer JSON with `live_candidates` added (post-integrity, like data_freshness), or the input unchanged when the
    answer is not a JSON object."""
    try:
        data = json.loads((answer_text or "").strip())
    except (ValueError, TypeError):
        return answer_text, "answer_not_json"
    if not isinstance(data, dict):
        return answer_text, "answer_not_json"
    data["live_candidates"] = block
    return json.dumps(data, ensure_ascii=False), "attached"


def _answer_links(answer_text):
    try:
        data = json.loads((answer_text or "").strip())
    except (ValueError, TypeError):
        return []
    out = []
    groups = [data.get("platforms")] if isinstance(data, dict) else []
    for s in (data.get("subjects") or []) if isinstance(data, dict) else []:
        groups.append(s.get("platforms") if isinstance(s, dict) else None)
    for platforms in groups:
        for entry in platforms if isinstance(platforms, list) else []:
            for p in (entry.get("posts") if isinstance(entry, dict) else None) or []:
                if isinstance(p, dict) and isinstance(p.get("link"), str):
                    out.append(p["link"])
    return out


def _log(event, **fields):
    """One structured line per step. Counts, flags and safe reason codes only: never keys, headers, queries or post text."""
    try:
        log.info("active_intent %s", json.dumps(dict(event=event, **fields), default=str, sort_keys=True))
    except Exception:
        pass


# ── the object the two answer paths call ─────────────────────────────────────────────────────────────────────────────────

class Run:
    """One search's active-intent run. Create it with `Run.prepare(...)`; an inert Run (the usual case) does nothing."""

    def __init__(self, active=False, **kw):
        self.active = active
        self.msg = kw.get("msg")
        self.owner_key = kw.get("owner_key")
        self.chat_id = kw.get("chat_id")
        self.mode = kw.get("mode")
        self.window = kw.get("window")
        self.window_range = kw.get("window_range")
        self.terms = kw.get("terms") or []
        self.live_result = None
        self._thread = None
        self._done = threading.Event()
        self._t0 = None
        self._finished = False

    # -- creation -----------------------------------------------------------------------------------------------------
    @classmethod
    def prepare(cls, msg, *, owner_key, chat_id, mode="stream", owner_type=None, now=None):
        """Decide eligibility and, if eligible, apply the recent window to THIS request's in-memory message. Never raises."""
        try:
            if not enabled():
                return cls()
            if not isinstance(msg, dict):
                return cls()
            text = msg.get("resolved_query") or msg.get("query")
            detected, why = is_active_lead_intent(text)
            if not detected:
                _log("intent_not_detected", reason=why, mode=mode)
                return cls()
            if not reddit_live.is_active():
                _log("intent_detected_live_inactive", mode=mode)
                return cls()
            owner_type = owner_type or ("email" if isinstance(owner_key, str) and "@" in owner_key else "anon")
            if owner_type != "email":
                _log("live_eligibility_rejected", reason="guest", mode=mode)
                return cls()
            ok, reason = reddit_live.eligible_pre_answer(msg)     # includes: no explicit time range, plain lead search
            if not ok:
                _log("live_eligibility_rejected", reason=reason, mode=mode)
                return cls()
            now = timerange.to_utc(now) or datetime.now(timezone.utc)
            tr = recent_window(now)
            msg["time_range"] = tr.to_doc(now)     # in memory only: the existing retrieval reads it exactly like a typed range
            run = cls(True, msg=msg, owner_key=owner_key, chat_id=chat_id, mode=mode, window=window_minutes(),
                      window_range=tr, terms=_terms(text, " ".join(msg.get("keywords") or [])))
            _log("intent_detected", mode=mode, window_minutes=run.window)
            _log("historical_branch_started", mode=mode, window_minutes=run.window)
            return run
        except Exception as exc:
            log.warning("active_intent prepare failed closed (%s)", type(exc).__name__)
            return cls()

    # -- the live branch ----------------------------------------------------------------------------------------------
    def start(self):
        """Start the Live branch NOW, independently of the historical branch. Idempotent; never raises."""
        if not self.active or self._thread is not None:
            return
        try:
            self._t0 = time.monotonic()
            _log("live_branch_started", mode=self.mode)
            self._thread = threading.Thread(target=self._live_worker, name="active-intent-live", daemon=True)
            self._thread.start()
        except Exception as exc:
            log.warning("active_intent live start failed (%s)", type(exc).__name__)
            self.live_result = {"state": "unavailable", "reason": "thread_start_failed"}
            self._done.set()

    def _live_worker(self):
        try:
            factory = DEPS_FACTORY or reddit_live.default_deps
            self.live_result = reddit_live.process(factory(), owner_key=self.owner_key, owner_type="email",
                                                   chat_id=self.chat_id, request_id=self.msg.get("request_id"),
                                                   pre_answer=True)
        except Exception as exc:
            self.live_result = {"state": "unavailable", "reason": "exception:" + type(exc).__name__}
        finally:
            self._done.set()

    # -- the historical side ------------------------------------------------------------------------------------------
    def historical(self, matched):
        """Background path only (its retrieval already ran before the hook, so the window cannot be passed to it): start the
        live branch and keep the saved posts published inside the window, using the existing pure range filter."""
        if not self.active:
            return matched
        self.start()
        try:
            kept, stats = timerange.filter_posts(matched, self.window_range)
            _log("historical_window_post_filter", **stats)
            return kept
        except Exception as exc:
            log.warning("active_intent historical filter failed (%s)", type(exc).__name__)
            return matched

    # -- the merge ----------------------------------------------------------------------------------------------------
    def finish(self, answer_text, historical=None, historical_timed_out=False):
        """Return the final answer with the live candidates added (or unchanged). `historical` is the saved-post pool the
        answer was built from. Never raises; every failure leaves the historical answer exactly as it was."""
        if not self.active or self._finished:
            return answer_text
        self._finished = True
        try:
            self.start()
            remaining = max(0.0, live_wait_seconds() - (time.monotonic() - (self._t0 or time.monotonic())))
            finished = self._done.wait(remaining)
            res = self.live_result if finished and isinstance(self.live_result, dict) else {}
            hist_state = "timed_out" if historical_timed_out else "completed"
            hist = normalize_historical(historical)
            _log("historical_branch_" + ("failed" if historical_timed_out else "completed"),
                 result_count=len(historical or []), timed_out=bool(historical_timed_out))
            live_state = "timed_out_waiting" if not finished else res.get("state")
            links = res.get("links") if res.get("state") == "ok" else []
            live = normalize_live(links)
            _log("live_branch_" + ("completed" if res.get("state") == "ok" else "failed"), state=live_state,
                 reason=res.get("reason"), http_status=res.get("http_status"),
                 web_search_calls=res.get("web_search_calls"), validated_urls=len(live))
            # a link already in the saved posts OR in the finished answer is a duplicate of historical evidence
            known = list(hist) + [Evidence(SOURCE_HISTORICAL, u, None) for u in _answer_links(answer_text)]
            candidates, dups = combine(known, live, self.terms)
            combined = len(hist) + len(candidates)
            _log("evidence_combined", deduplicated=dups, historical=len(hist), live=len(candidates), final_combined=combined)
            if not candidates:
                _log("final_response", outcome="historical_only_or_empty", live_candidates=0)
                return answer_text
            block = build_block(candidates, historical_count=sum(1 for e in hist if e.has_content), window=self.window,
                                duplicates_removed=dups,
                                historical_state=hist_state)
            out, outcome = attach(answer_text, block)
            _log("final_response", outcome=outcome, live_candidates=len(candidates))
            return out
        except Exception as exc:
            log.warning("active_intent finish failed closed (%s)", type(exc).__name__)
            return answer_text
