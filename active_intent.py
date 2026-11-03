"""Active Intent: a separate Live Reddit search, combined into ONE answer with the unchanged Historical Search.

For a signed-in owner's plain lead or research search that a small GPT classifier (intent_classifier.py) judges - from its MEANING,
not from particular words - to be either ACTIVE DEMAND (people looking for or needing a solution) or ACTIVE DISCUSSION (people
currently discussing a topic), this module lets two independent branches contribute to the same answer:

  Branch A - the EXISTING saved-post (historical) search and its normal evidence analysis. It is NOT changed in any way: it keeps
             the message, the time range and the behaviour the user asked for. The Live window is never given to it.
  Branch B - the separate Live Reddit web search of reddit.com (reddit_live.py: same claim, budgets, cooldown, in-flight guard,
             URL validation and idempotency), started at the same moment, driven by the keywords the router already stored on
             the message, with instructions for the decided intent (demand or discussion). Its window is the Live window,
             ACTIVE_INTENT_WINDOW_MINUTES (default 30; any integer from 1 to MAX_WINDOW_MINUTES = 10080; an unusable value turns
             the workflow off instead of being changed): the half-open UTC interval [end - minutes, end) whose end is the search's
             own `requested_at`. The model is told the exact boundaries, and then the BACKEND checks every returned post's
             publication time against Reddit's own metadata and keeps only posts inside the interval.

What the combined answer is - and is not
    * Historical rows keep their normal analysis, ranking and integrity checks. Nothing here touches them.
    * A live result is a URL, maybe a title, and the publication time Reddit reported (checked to lie inside the Live window).
      Flintel has NOT read the post: it is ranked by how well its TITLE matches the request, never given a buying-intent score,
      never called a buyer or lead, and only its date is verified. Web search is discovery, not an index of Reddit.
    * Live candidates are merged AFTER the answer is finalised, as one clearly labelled block inside the same answer
      (`live_candidates`, with `intent` "demand" or "discussion"). They are deliberately NOT put into the pool the answer-integrity
      layer counts.
    * Each branch fails on its own: a live failure or zero result leaves the historical answer untouched.

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
import intent_classifier
import reddit_live
import timerange

log = logging.getLogger("flintel.active_intent")

SOURCE_HISTORICAL = "historical"
SOURCE_LIVE = "live_web_search"
DEFAULT_WINDOW_MINUTES = 30           # used by config.py when ACTIVE_INTENT_WINDOW_MINUTES is unset or blank
MAX_WINDOW_MINUTES = 10080            # 7 days: the deliberate, documented upper bound (nothing silently shortens a value above it)
DEFAULT_LIVE_WAIT_SECONDS = 30
MAX_LIVE_WAIT_SECONDS = 60
MAX_LIVE_CANDIDATES = 8
WINDOW_KIND = "active_intent_window"   # the TimeRange.kind of this workflow's window: the explicit marker the Historical scan reads (mysql_signals.ACTIVE_INTENT_WINDOW_KIND)
REQUEST_KIND_NAMES = ("mention_search", "research", "person_mentions", "provider_search")   # the router's fixed kinds: safe to log

# Overridable in tests: how the Live branch obtains its database/HTTP dependencies (default: the real ones).
DEPS_FACTORY = None


# ── configuration ────────────────────────────────────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    return getattr(config, "ACTIVE_INTENT_ENABLED", False) is True


def window_setting():
    """(minutes, reason). The configured window EXACTLY as configured, or (None, why) when it cannot be used:
    'invalid' (not a positive integer: config.py stores 0 for those, and a boolean is never accepted) or 'over_maximum'
    (above MAX_WINDOW_MINUTES). The workflow then stays off - a value is never silently clamped or replaced."""
    v = getattr(config, "ACTIVE_INTENT_WINDOW_MINUTES", DEFAULT_WINDOW_MINUTES)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return None, "invalid"
    if v > MAX_WINDOW_MINUTES:
        return None, "over_maximum"
    return v, "ok"


def window_minutes():
    """The configured window in minutes, or None when it is unusable (see window_setting)."""
    return window_setting()[0]


def live_wait_seconds() -> int:
    v = getattr(config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", DEFAULT_LIVE_WAIT_SECONDS)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        v = DEFAULT_LIVE_WAIT_SECONDS
    return min(v, MAX_LIVE_WAIT_SECONDS)


def window_end(msg, now=None):
    """The one instant that closes the shared window: the search's own `requested_at` (never later than now), else now, in whole
    seconds. Deriving it from the search - not from whichever code path runs - is what makes every path agree."""
    now = timerange.to_utc(now) or datetime.now(timezone.utc)
    t = timerange.to_utc((msg or {}).get("requested_at")) if isinstance(msg, dict) else None
    return (min(t, now) if t is not None else now).replace(microsecond=0)


def recent_window(end, minutes) -> "timerange.TimeRange":
    """The half-open [end - minutes, end) range, in the same type a user-typed 'last N minutes' produces."""
    end = (timerange.to_utc(end) or datetime.now(timezone.utc)).replace(microsecond=0)
    return timerange.TimeRange(start=end - timedelta(minutes=minutes), end=end, tz_name="UTC", kind=WINDOW_KIND,
                               phrase=f"last {minutes} minutes", capped=True,
                               note="Applied automatically because this search asks for people who are actively looking; the "
                                    "live web search uses the same window and its posts are date-checked against it.")


def same_window(doc, tr) -> bool:
    r = timerange.range_from_doc(doc)
    return r is not None and r.start == tr.start and r.end == tr.end


# (Eligibility by meaning - not by words - is decided by intent_classifier.decide(), called from apply_window below. There is no
# regex or phrase list in this module and no fallback to one.)


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
    extra: dict = field(default_factory=dict)   # live only: subreddit, body (the post's real text), analysis (grounded summary / sentiment)


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
            when = timerange.to_utc(l.get("published_at")) if isinstance(l.get("published_at"), str) else None
            safe = (reddit_live._safe_links([l]) or [{}])[0]           # re-validated: only grounded content fields survive
            out.append(Evidence(source_type=SOURCE_LIVE, url=url, title=reddit_live.clean_title(l.get("title")),
                                has_content=False, published_at=when.isoformat() if when else None,
                                verification="date_verified_only" if when else "unverified",
                                extra={k: safe[k] for k in ("subreddit", "body", "analysis") if k in safe}))
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

def _fmt(t):
    return timerange.to_utc(t).strftime("%Y-%m-%d %H:%M UTC")


def build_report(candidates, report, found):
    """The concise research summary of the Live posts, or None. Everything in it is tied to the posts in `candidates` (accepted,
    date-verified, with a grounded analysis): theme counts are counted here, a theme or implication with no supporting accepted
    post is dropped, and the limitations are written here (never by the model). `found` = how many posts the model's search
    returned before verification."""
    if not isinstance(report, dict):
        return None
    analysed = {reddit_live.url_key(c.url) for c in candidates if (c.extra or {}).get("analysis")}
    if not analysed:
        return None
    themes = []
    for t in report.get("themes") or []:
        urls = [u for u in t.get("post_urls") or [] if reddit_live.url_key(u) in analysed]
        if t.get("theme") and urls:
            themes.append({"theme": t["theme"], "why_it_matters": t.get("why_it_matters"), "verified_post_count": len(urls),
                           "post_urls": urls})
    without = sum(1 for c in candidates if not (c.extra or {}).get("analysis"))
    limits = ["Summaries and sentiment are GPT's reading of each post; Flintel confirmed the post, its date and that the quoted "
              "excerpt is in the post, not the reading itself.",
              "Web search finds some posts, not all of them."]
    if without:
        limits.insert(0, f"{without} of {len(candidates)} posts are listed without a summary because their text could not be "
                         "matched to an analysis.")
    if found > len(candidates):
        limits.append(f"{found - len(candidates)} found post{'s were' if found - len(candidates) != 1 else ' was'} left out because "
                      "Reddit could not confirm its date, text or window.")
    return {"main_finding": report.get("main_finding"), "themes": themes[:3], "implications": (report.get("implications") or [])[:3],
            "limitations": limits[:3]}


def build_block(candidates, *, historical_count, window_range, duplicates_removed, historical_state, intent="demand",
                report=None, found=0, search_keywords=None):
    """The `live_candidates` object. Every sentence in it is written here, never by a model, and says only what is true: each
    item's publication time was confirmed from Reddit's own metadata to lie inside the Live window; nothing else about the post
    (its content, its author's intent) has been read or assessed by Flintel; and web search cannot list every post in a window.
    `intent` ("demand" or "discussion") only changes the label and the sentence about what the posts are: a discussion post is
    never described as a buyer or a lead."""
    n = len(candidates)
    discussion = intent == "discussion"
    minutes = int((window_range.end - window_range.start).total_seconds() // 60)
    what = (f"found by a separate live web search as relevant current conversations about your topic" if discussion
            else "found by a separate live web search for people who appear to be looking for a solution")
    read = ("Flintel has not read these posts; they are not leads or confirmed buyers, and they are ranked only by how well their "
            "titles match your request." if discussion else
            "Flintel has not read these posts or assessed whether the authors are buying, so they are ranked only by how well "
            "their titles match your request.")
    parts = [f"{'Live Reddit discussions' if discussion else 'Live Reddit results'}: {n} Reddit post{'s' if n != 1 else ''} {what}. "
             f"Each post's publication time was checked against Reddit's own data and falls between {_fmt(window_range.start)} and "
             f"{_fmt(window_range.end)} (the {minutes}-minute live search window). {read} "
             "Web search finds some posts, not all of them: other posts in this window may exist that were not found."]
    if historical_state == "timed_out":
        parts.append("The search of saved posts did not finish in time, so this answer may be missing saved-post evidence.")
    elif historical_count:
        parts.append("Saved-post results are analysed in the answer above and are separate from these live results.")
    else:
        parts.append("No analysed saved-post evidence was available for this answer, so these live results are the only "
                     "posts found.")
    if duplicates_removed:
        parts.append(f"{duplicates_removed} duplicate link{'s' if duplicates_removed != 1 else ''} already covered by the "
                     "saved posts ha" + ("ve" if duplicates_removed != 1 else "s") + " been left out.")
    return {
        "source": SOURCE_LIVE, "intent": "discussion" if discussion else "demand",
        "label": "Live Reddit discussions" if discussion else "Live Reddit results",
        "verification": "date_verified_title_and_url_only", "live_window_minutes": minutes,
        "live_results_limited_to_window": True, "live_dates_verified_against": "reddit_metadata",
        "window": {"minutes": minutes, "start": window_range.start.isoformat(), "end": window_range.end.isoformat()},
        "statement": " ".join(parts),
        "items": [{"rank": i + 1, "title": c.title, "url": c.url, "source": SOURCE_LIVE,
                   "intent": "discussion" if discussion else "demand",
                   "verification_status": "date_verified_only", "evidence": "title_url_and_verified_date_only",
                   "published_at": c.published_at, "date_verified": True, "title_relevance": c.relevance,
                   "subreddit": (c.extra or {}).get("subreddit"), "body": (c.extra or {}).get("body") or None,
                   "analysis": (c.extra or {}).get("analysis"),
                   "content_status": "analysed" if (c.extra or {}).get("analysis") else "analysis_unavailable",
                   "discovered_by": list(search_keywords or [])[:20]}
                  for i, c in enumerate(candidates)],
        "report": build_report(candidates, report, found),
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


def _default_recency_lookback(msg) -> bool:
    """True only when the message RECORDS that its `time_window_days` was created automatically from a bare recency word
    ("currently", "right now", "recent"): `time_window_source == "default_recent"`, set by /search at the moment it made that
    lookback. Nothing is inferred from the number of days or the wording, so a user who asks for "the last 30 days" (recorded as
    "explicit_period") is never mistaken for it, and a message with no recorded origin (an older one) is treated as the user's own.
    Read-only; never raises."""
    try:
        days = msg.get("time_window_days")
        return (msg.get("time_window_source") == "default_recent" and isinstance(days, int) and not isinstance(days, bool)
                and days > 0 and not msg.get("time_range"))
    except Exception:
        return False


def historical_window_applies(msg, tr) -> bool:
    """Whether the Live window should ALSO be the Historical search's window for this request: only when the user gave the
    Historical search no range or period of their own. A typed `time_range` or a named period always wins and is never touched;
    only a lookback RECORDED as automatic (`time_window_source == "default_recent"`) does not count as the user's own. A message that already carries exactly this
    window (the second call of the same request) is already done."""
    existing = msg.get("time_range")
    if existing:
        return False
    days = msg.get("time_window_days")
    return days is None or _default_recency_lookback(msg)


def apply_window(msg, *, owner_key, chat_id, mode, owner_type=None, now=None):
    """(live_window, intent, reason). Decides whether this search qualifies for a Live Reddit search and of which kind
    ("demand" or "discussion", by the classifier) and returns the Live window [end - ACTIVE_INTENT_WINDOW_MINUTES, end).
    When the user gave the Historical search NO range or period of their own (see historical_window_applies), the same UTC interval is
    put on the in-memory `msg["time_range"]` so the EXISTING saved-post retrieval - MongoDB and MySQL alike - reads it exactly like
    a typed range, before it runs. A range or period the user chose is never overwritten and never blocks the Live search. The
    classifier is asked at most once per message (stored decision). Returns (None, kind, reason) when not eligible."""
    if not enabled():
        return None, "disabled", "disabled"
    if not isinstance(msg, dict):
        return None, "rejected", "no_message"
    # (HISTORICAL-ONLY SEARCH) Active Intent no longer depends on Live Search being available: the classifier and the shared
    # Historical window below run whether or not any Live code is switched on.
    owner_type = owner_type or ("email" if isinstance(owner_key, str) and "@" in owner_key else "anon")
    if owner_type != "email":
        _log("live_eligibility_rejected", reason="guest", mode=mode)
        return None, "rejected", "guest"
    minutes, wwhy = window_setting()
    if minutes is None:
        cfg = getattr(config, "ACTIVE_INTENT_WINDOW_MINUTES", None)
        log.warning("active_intent window unusable (%s): configured=%s maximum=%s - the workflow stays off for this search",
                    wwhy, cfg if isinstance(cfg, int) and not isinstance(cfg, bool) else "not-an-integer", MAX_WINDOW_MINUTES)
        _log("window_unusable", reason=wwhy, maximum=MAX_WINDOW_MINUTES, mode=mode)
        return None, "window_unusable", wwhy
    tr = recent_window(window_end(msg, now), minutes)
    ok, reason = reddit_live.eligible_pre_answer(msg)      # a plain lead or research search; no explicit time range of the user's
    if not ok:
        _log("live_eligibility_rejected", reason=reason, mode=mode,
             **({"request_kind": msg.get("request_kind")} if reason == "kind" and msg.get("request_kind") in REQUEST_KIND_NAMES else {}))
        return None, "rejected", reason
    # Every structural rule has passed, so only now is the (paid, once-per-message) semantic decision made.
    intent, why = intent_classifier.decide_full(owner_key=owner_key, chat_id=chat_id, request_id=msg.get("request_id"))
    _log("intent_classified", qualifies=bool(intent), intent=intent or None, reason=why, mode=mode)
    if not intent:
        return None, "not_detected", why
    if historical_window_applies(msg, tr):
        msg["time_range"] = tr.to_doc(tr.end)      # in memory only, the shared interval; the existing retrieval reads it like a typed range
        _log("historical_window_applied", mode=mode, window_minutes=int((tr.end - tr.start).total_seconds() // 60))
    elif same_window(msg.get("time_range"), tr):
        pass                                        # the second call of this request: already carries exactly this window
    else:
        _log("historical_range_kept", mode=mode)    # the user's own range / period stays Historical's; Live runs on its own window
    return tr, intent, why


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
        self.intent = kw.get("intent") or "demand"       # "demand" or "discussion": what the classifier decided
        self.terms = kw.get("terms") or []
        self.live_result = None
        self._thread = None
        self._done = threading.Event()
        self._t0 = None
        self._finished = False

    # -- creation -----------------------------------------------------------------------------------------------------
    @classmethod
    def prepare(cls, msg, *, owner_key, chat_id, mode="stream", owner_type=None, now=None):
        """Decide eligibility and, if eligible, remember the Live window and intent. The message is not changed. Never raises."""
        try:
            tr, intent, reason = apply_window(msg, owner_key=owner_key, chat_id=chat_id, mode=mode, owner_type=owner_type, now=now)
            if tr is None:
                return cls()
            text = msg.get("resolved_query") or msg.get("query")
            minutes = int((tr.end - tr.start).total_seconds() // 60)
            run = cls(True, msg=msg, owner_key=owner_key, chat_id=chat_id, mode=mode, window=minutes,
                      window_range=tr, intent=intent, terms=_terms(text, " ".join(msg.get("keywords") or [])))
            _log("intent_detected", mode=mode, intent=intent, window_minutes=minutes, window_start=tr.start.isoformat(),
                 window_end=tr.end.isoformat())
            _log("historical_branch_started", mode=mode)
            return run
        except Exception as exc:
            log.warning("active_intent prepare failed closed (%s)", type(exc).__name__)
            return cls()

    # -- the live branch ----------------------------------------------------------------------------------------------
    def start(self):
        """Start the Live branch NOW, independently of the historical branch. Idempotent; never raises."""
        if not self.active or self._thread is not None:
            return
        if reddit_live.LIVE_SEARCH_EXECUTION_ENABLED is not True:   # historical-only search: no thread, no Live call, nothing to wait for
            self.live_result = {"state": "disabled", "reason": "live_search_removed"}
            self._done.set()
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
                                                   pre_answer=True, window=(self.window_range.start, self.window_range.end),
                                                   intent=self.intent)
        except Exception as exc:
            self.live_result = {"state": "unavailable", "reason": "exception:" + type(exc).__name__}
        finally:
            self._done.set()

    # -- the historical side ------------------------------------------------------------------------------------------
    def historical(self, matched):
        """Background path: start the live branch (concurrent with the model call) and return the saved posts EXACTLY as they
        are. The Live window is never applied to them: Historical Search is not changed by this workflow."""
        if not self.active:
            return matched
        self.start()
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
            inside = [e for e in live if e.published_at and self.window_range.contains(e.published_at)]
            if len(inside) != len(live):                        # defence in depth: the backend already checked each date
                _log("live_candidates_dropped_outside_window", count=len(live) - len(inside))
            live = inside
            _log("live_branch_" + ("completed" if res.get("state") == "ok" else "failed"), state=live_state,
                 reason=res.get("reason"), http_status=res.get("http_status"),
                 web_search_calls=res.get("web_search_calls"), validated_urls=len(live),
                 verification=res.get("verification"),         # counts only: verified / outside_window / unverifiable / removed + lookup outcome
                 url_diagnostics=res.get("url_diagnostics"))   # counts / formats / reasons of the URLs the search offered (no content)
            # a link already in the saved posts OR in the finished answer is a duplicate of historical evidence
            known = list(hist) + [Evidence(SOURCE_HISTORICAL, u, None) for u in _answer_links(answer_text)]
            candidates, dups = combine(known, live, self.terms)
            combined = len(hist) + len(candidates)
            _log("evidence_combined", deduplicated=dups, historical=len(hist), live=len(candidates), final_combined=combined)
            if not candidates:
                _log("final_response", outcome="historical_only_or_empty", live_candidates=0)
                return answer_text
            block = build_block(candidates, historical_count=sum(1 for e in hist if e.has_content), window_range=self.window_range,
                                duplicates_removed=dups, historical_state=hist_state, intent=self.intent,
                                report=res.get("report"), found=int((res.get("verification") or {}).get("checked") or len(live)),
                                search_keywords=[k for k in (self.msg or {}).get("keywords") or [] if isinstance(k, str)])
            out, outcome = attach(answer_text, block)
            _log("final_response", outcome=outcome, live_candidates=len(candidates))
            return out
        except Exception as exc:
            log.warning("active_intent finish failed closed (%s)", type(exc).__name__)
            return answer_text
