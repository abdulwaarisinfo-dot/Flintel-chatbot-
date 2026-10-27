"""
C — natural-language date/time ranges for post searches.

    parse_time_range(text, reference_utc, tz_name) -> TimeRange | Clarification | None 

* Pure and deterministic: the caller captures ONE reference instant (aware UTC)
  per request and passes it in; nothing here reads the clock.
* Every range is HALF-OPEN: start inclusive, end exclusive, both aware UTC.
  Local calendar boundaries (midnight, "today", "October 9") are built in the
  user's timezone with zoneinfo and only then converted to UTC, so DST days
  are 23/25 hours long and midnight is the real local midnight.
* An end that lies after the reference instant is capped at the reference
  instant (posts cannot be published in the future); `capped` records it.
* Meanings kept distinct:
    "last 3 hours" / "past 3 hours" / "since 3 hours ago"
                        -> rolling: [ref - 3h, ref)
    "3 hours ago"       -> a point; read the way Reddit labels a post
                           "3 hr. ago": age of 3 to 4 hours -> [ref - 4h, ref - 3h)
    "2 days ago"        -> the local calendar day two days back, whole day
    "last 2 days"       -> rolling: [ref - 48h, ref)
    "October 9 to October 12"
                        -> Oct 9 00:00 local .. Oct 13 00:00 local (final day included)
* Anything materially ambiguous (a date later this year with no year, 10/9,
  "from 3 to 5" with no am/pm, an end before the start, a range wholly in the
  future) returns a Clarification instead of a guess.
* Returns None when the text carries no time expression this module handles
  (the caller then keeps its existing behaviour).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo

import platforms   # the platform registry (earliest valid date per platform); has no imports of its own

try:                                    # stdlib; needs the system tz database (or the tzdata package)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:                       # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

DEFAULT_TZ = "Asia/Karachi"
_PKT = timezone(timedelta(hours=5), "PKT")   # Pakistan has no DST; used if the tz database is missing


# ── timezone handling ────────────────────────────────────────────────────────
def resolve_tz(tz_name) -> tuple[tzinfo, str]:
    """(tzinfo, canonical name). Unknown / invalid names fall back to DEFAULT_TZ."""
    for name in ((tz_name if isinstance(tz_name, str) else None), DEFAULT_TZ):
        if not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", name):
            continue
        if name.upper() in ("UTC", "ETC/UTC", "GMT"):
            return timezone.utc, "UTC"
        if ZoneInfo is not None:
            try:
                return ZoneInfo(name), name
            except (ZoneInfoNotFoundError, ValueError, OSError):
                pass
        if name == "Asia/Karachi":
            return _PKT, name
    return _PKT, DEFAULT_TZ


def is_valid_tz(tz_name) -> bool:
    if not isinstance(tz_name, str) or not tz_name:
        return False
    return resolve_tz(tz_name)[1] == tz_name or tz_name.upper() in ("UTC", "ETC/UTC", "GMT")


def _local_midnight(d: date, tz: tzinfo) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _at(d: date, t: time, tz: tzinfo) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=tz)


# ── results ──────────────────────────────────────────────────────────────────
@dataclass
class TimeRange:
    start: datetime                 # aware UTC, inclusive
    end: datetime                   # aware UTC, exclusive
    tz_name: str
    kind: str                       # rolling | point | day | day_part | date_range | time_range | week | month
    phrase: str                     # the words that were interpreted
    capped: bool = False            # end was moved back to the reference instant
    note: str = ""                  # extra explanation shown with the range

    def contains(self, instant) -> bool:
        t = to_utc(instant)
        return t is not None and self.start <= t < self.end

    def describe(self) -> str:
        tz, _ = resolve_tz(self.tz_name)
        a, b = self.start.astimezone(tz), self.end.astimezone(tz)
        same_day = a.date() == b.date()
        fa = _fmt(a, with_date=True)
        fb = _fmt(b, with_date=not same_day)
        zone = a.tzname() if a.tzname() == b.tzname() else f"{a.tzname()}/{b.tzname()}"
        text = f"{fa} → {fb} ({self.tz_name}, {zone}; start included, end excluded)"
        if self.capped:
            text += " — ends at the time of this search"
        return text

    def to_doc(self, reference_utc: datetime) -> dict:
        return {"start": self.start, "end": self.end, "tz": self.tz_name, "kind": self.kind,
                "phrase": self.phrase, "capped": self.capped, "note": self.note,
                "reference": to_utc(reference_utc), "display": self.describe()}


@dataclass
class Clarification:
    question: str
    phrase: str = ""
    reasons: list = field(default_factory=list)


def _fmt(dt: datetime, with_date: bool) -> str:
    hour = dt.strftime("%I").lstrip("0") or "12"
    clock = f"{hour}:{dt.strftime('%M')} {dt.strftime('%p')}"
    if not with_date:
        return clock
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {clock}"


def to_utc(value):
    """Aware UTC datetime from a datetime / ISO string / epoch number, else None."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


# A stored publication time is trusted only between these bounds. Before the floor (epoch 0, 1970, 1900 placeholders) a
# post cannot exist on Reddit; after "now + tolerance" the value is a future date. Either is treated exactly like a missing
# or unreadable date: unknown, never fresh and never a date shown to the user. Ingestion/scan time is never substituted.
PUBLICATION_FLOOR = platforms.DEFAULT_EARLIEST_DATE             # shared default floor (platforms.py: a platform may have its own)
FUTURE_TOLERANCE = timedelta(minutes=10)                            # clock skew between collector and app


def publication_time(value, now=None, platform=None):
    """Aware UTC datetime of a stored PUBLICATION time, or None when it is missing, unreadable, before the platform's
    earliest valid date (default PUBLICATION_FLOOR) or NOT BEFORE now + FUTURE_TOLERANCE. (to_utc stays a plain parser: it is
    also used for range endpoints.)

    The upper bound is EXCLUSIVE — the same half-open convention as every retrieval window [start, now + tolerance) — so a
    time exactly at the limit is unknown, never valid. (Phase 2.2: it was inclusive, which disagreed with the window by one
    instant.) Pass `now` to validate against one shared clock reading; without it the current time is read once per call."""
    t = to_utc(value)
    if t is None:
        return None
    ref = to_utc(now) or datetime.now(timezone.utc)
    return t if platforms.earliest_date(platform) <= t < ref + FUTURE_TOLERANCE else None


# ── time intent: "latest / recent / current" with no stated period ────────────────────────────────────────────────────
# Deterministic, conservative reading of the user's words. Explicit periods ("last 7 days") and calendar ranges are parsed
# elsewhere (router window / parse_time_range) and always win; this only decides whether a bare recency word should
# activate the documented default lookback (config.RECENT_DEFAULT_WINDOW_DAYS). Bounded on purpose: "current" counts only in
# a demand/discussion sense ("current demand for X"), never as "their current CRM"; "latest iPhone" names a product.
_RECENT_PLAIN = re.compile(r"\b(?:latest|newest|most\s+recent|recent(?:ly)?|lately|freshest|right\s+now|these\s+days|nowadays|"
                           r"as\s+of\s+now)\b", re.I)
_NOT_A_TIME_FILTER_AFTER = re.compile(r"^\s+(?:version|release|model|edition|generation|iteration|update|upgrade|iphone|android|ios|"
                                      r"graduates?|grads?|hires?|immigrants?|college|retirees?)\b", re.I)
_CURRENT_AS_TIME = re.compile(
    r"\bcurrent\s+(?:demand|trends?|state|situation|interest|buyers?|leads?|opportunit(?:y|ies)|discussions?|conversations?|"
    r"complaints?|needs?|pain\s+points?|sentiment|activity|mentions?|posts?|threads?|requests?|chatter|market\s+(?:demand|"
    r"sentiment|trends?))\b", re.I)
_CURRENTLY_AS_TIME = re.compile(r"\bcurrently\s+(?:looking|searching|seeking|asking|evaluating|comparing|switching|shopping|"
                                r"hiring|trying|needing|wanting|discussing|complaining)\b", re.I)


def recency_intent(text) -> bool:
    """True when the wording asks for the newest posts ("latest buyer-intent discussions", "recent complaints about X",
    "current demand for Y", "people currently looking for a CRM"). "their current CRM" and "latest iPhone" are not."""
    if not isinstance(text, str) or not text.strip():
        return False
    for m in _RECENT_PLAIN.finditer(text):
        if m.group(0).lower() in ("latest", "newest") or m.group(0).lower().startswith("recent"):
            if _NOT_A_TIME_FILTER_AFTER.match(text[m.end():]):
                continue
        return True
    return bool(_CURRENT_AS_TIME.search(text) or _CURRENTLY_AS_TIME.search(text))


def effective_lookback(query, resolved_query=None, routed_days=None, time_range=None, resolved_range=None,
                       default_days=30, max_days=3650):
    """(days, time_range, source) — the lookback this message is searched over. Precedence:
      explicit calendar range/period in the message > the router's period (from the user's own words) >
      a constraint the router carried into its standalone restatement of a follow-up (`resolved_range`, parsed from
      resolved_query: "now only agencies" after "... last 14 days") > the documented default for a bare recency word
      (in the message or in that restatement) > none (an ordinary, unbounded search)."""
    if time_range is not None:
        return None, time_range, "explicit_range"
    if isinstance(routed_days, int) and not isinstance(routed_days, bool) and routed_days > 0:
        return min(routed_days, max_days), None, "explicit_period"
    if resolved_range is not None:
        return None, resolved_range, "restated_range"
    if isinstance(default_days, int) and default_days > 0 and (recency_intent(query) or recency_intent(resolved_query)):
        return min(default_days, max_days), None, "default_recent"
    return None, None, "none"


def published_iso(doc):
    """ISO-8601 UTC string of a stored signal's ORIGINAL publication time
    (its created_utc), or None — used to carry the date onto result posts."""
    t = publication_time((doc or {}).get("created_utc")) if isinstance(doc, dict) else None
    return t.isoformat() if t is not None else None


def with_published_at(post: dict, doc) -> dict:
    """`post` plus "published_at" when the source doc has a usable created_utc."""
    iso = published_iso(doc)
    if iso:
        post["published_at"] = iso
    return post


def range_from_doc(doc):
    """TimeRange from a stored message `time_range` dict (or None)."""
    if not isinstance(doc, dict):
        return None
    start, end = to_utc(doc.get("start")), to_utc(doc.get("end"))
    if start is None or end is None or not start < end:
        return None
    return TimeRange(start=start, end=end, tz_name=str(doc.get("tz") or DEFAULT_TZ), kind=str(doc.get("kind") or ""),
                     phrase=str(doc.get("phrase") or ""), capped=bool(doc.get("capped")), note=str(doc.get("note") or ""))


def post_published_at(post, now=None):
    """The ORIGINAL publication instant of a post, or None. Never ingestion time. `now`: one shared clock reading."""
    if not isinstance(post, dict):
        return None
    platform = platforms.of(post)
    for key in ("published_at", "created_utc"):
        t = publication_time(post.get(key), now=now, platform=platform)
        if t is not None:
            return t
    return None


def filter_posts(posts, time_range, now=None):
    """(kept, stats) — posts published inside the half-open range. Posts with no
    usable publication time are excluded and counted, never assumed to match. One clock reading validates every post."""
    stats = {"kept": 0, "outside": 0, "no_timestamp": 0}
    if time_range is None:
        return list(posts or []), stats
    now = to_utc(now) or datetime.now(timezone.utc)
    kept = []
    for p in posts or []:
        t = post_published_at(p, now=now)
        if t is None:
            stats["no_timestamp"] += 1
        elif time_range.start <= t < time_range.end:
            kept.append(p)
        else:
            stats["outside"] += 1
    stats["kept"] = len(kept)
    return kept, stats


# ── grammar ──────────────────────────────────────────────────────────────────
_MONTHS = {m: i for i, names in enumerate([
    (), ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
    ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
    ("oct", "october"), ("nov", "november"), ("dec", "december")]) for m in names}
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_UNIT_RE = r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?)"
_UNIT_SECONDS = {"sec": 1, "second": 1, "min": 60, "minute": 60, "hr": 3600, "hour": 3600,
                 "day": 86400, "week": 604800, "wk": 604800}
_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
             "thirty": 30, "forty-eight": 48, "fortyeight": 48}
_NUM_RE = r"(?P<num>\d{1,4}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)"

_TIME_RE = (r"(?:(?P<{p}h>\d{{1,2}})(?::(?P<{p}m>\d{{2}}))?\s*(?P<{p}ap>a\.?m\.?|p\.?m\.?)"
            r"|(?P<{p}hh>\d{{1,2}}):(?P<{p}mm>\d{{2}})(?!\s*(?:a\.?m|p\.?m))"
            r"|(?P<{p}word>noon|midday|midnight))")
_BARE_HOUR_RE = r"(?P<{p}bare>\d{{1,2}})(?!\s*(?:[:\d]|a\.?m|p\.?m|st|nd|rd|th|/))"

_DATE_RE = (r"(?:(?P<{p}mon>" + _MONTH_RE + r")\s+(?P<{p}day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<{p}year>\d{{4}}))?"
            r"|(?P<{p}day2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<{p}mon2>" + _MONTH_RE + r")(?:,?\s+(?P<{p}year2>\d{{4}}))?"
            r"|(?P<{p}iso>\d{{4}}-\d{{2}}-\d{{2}})"
            r"|(?P<{p}num>\d{{1,2}})/(?P<{p}num2>\d{{1,2}})(?:/(?P<{p}numy>\d{{2,4}}))?)")

_DAYWORD_RE = (r"(?P<dw>today|tonight|yesterday|aaj|(?P<dwn>\d{1,3}|two|three|four|five|six|seven|eight|nine|ten)\s+days?\s+ago"
               r"|(?:the\s+)?day\s+before\s+yesterday)")

_RANGE_SEP = r"\s*(?:to|until|till|through|thru|and|-|–|—)\s*"


def _num(tok) -> int | None:
    if tok is None:
        return None
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    return _WORD_NUM.get(tok)


def _unit_seconds(unit: str) -> int:
    u = (unit or "").lower()
    for prefix, secs in (("sec", 1), ("min", 60), ("h", 3600), ("d", 86400), ("w", 604800)):
        if u.startswith(prefix):
            return secs
    return 0


def _unit_name(seconds: int, n: int) -> str:
    name = {1: "second", 60: "minute", 3600: "hour", 86400: "day", 604800: "week"}[seconds]
    return f"{n} {name}{'' if n == 1 else 's'}"


class _Ambiguous(Exception):
    def __init__(self, question):
        super().__init__(question)
        self.question = question


def _time_from(m, p) -> time | None:
    """time from a _TIME_RE match with prefix p, or None if that group did not match."""
    g = m.groupdict()
    if g.get(p + "word"):
        w = g[p + "word"]
        return time(0, 0) if w == "midnight" else time(12, 0)
    if g.get(p + "h"):
        h, mi = int(g[p + "h"]), int(g.get(p + "m") or 0)
        ap = g[p + "ap"].replace(".", "")
        if not 1 <= h <= 12 or mi > 59:
            raise _Ambiguous("That time doesn't look valid — could you write it like 10 AM or 3:30 PM?")
        h = (0 if h == 12 else h) + (12 if ap.startswith("p") else 0)
        return time(h, mi)
    if g.get(p + "hh"):
        h, mi = int(g[p + "hh"]), int(g[p + "mm"])
        if h > 23 or mi > 59:
            raise _Ambiguous("That time doesn't look valid — could you write it like 10 AM or 15:30?")
        if 1 <= h <= 12 and not g[p + "hh"].startswith("0"):
            raise _Ambiguous(f"Do you mean {h}:{mi:02d} AM or PM?")
        return time(h, mi)
    return None


def _resolve_year(month: int, day: int, year: int | None, today: date, what: str) -> date:
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError:
            raise _Ambiguous(f"{what} isn't a real calendar date — which date did you mean?")
    try:
        d = date(today.year, month, day)
    except ValueError:
        raise _Ambiguous(f"{what} isn't a real calendar date this year — which date did you mean?")
    if d > today:
        prev = f"{d.strftime('%B')} {day}, {today.year - 1}"
        raise _Ambiguous(f"{d.strftime('%B')} {day} hasn't happened yet this year. Did you mean {prev}? "
                         f"Please include the year.")
    return d


def _date_parts(m, p):
    """(month, day, year_or_None, label) from a _DATE_RE match with prefix p, or None."""
    g = m.groupdict()
    if g.get(p + "iso"):
        y, mo, d = (int(x) for x in g[p + "iso"].split("-"))
        return mo, d, y, g[p + "iso"]
    if g.get(p + "num"):
        a, b = int(g[p + "num"]), int(g[p + "num2"])
        y = g.get(p + "numy")
        year = (int(y) + 2000 if len(y) == 2 else int(y)) if y else None
        if a <= 12 and b <= 12 and a != b:
            raise _Ambiguous(f"Does {a}/{b} mean {date(2000, a, 1).strftime('%B')} {b} or "
                             f"{date(2000, b, 1).strftime('%B')} {a}? Please write the month name.")
        if a > 12 and b > 12:
            raise _Ambiguous(f"{a}/{b} isn't a real calendar date — which date did you mean?")
        month, day = (a, b) if a <= 12 else (b, a)
        return month, day, year, f"{a}/{b}"
    mon = g.get(p + "mon") or g.get(p + "mon2")
    if mon:
        month = _MONTHS[mon.rstrip(".")]
        day = int(g.get(p + "day") or g.get(p + "day2"))
        y = g.get(p + "year") or g.get(p + "year2")
        return month, day, (int(y) if y else None), f"{date(2000, month, 1).strftime('%B')} {day}"
    return None


def _make_date(month, day, year, label) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise _Ambiguous(f"{label} isn't a real calendar date — which date did you mean?")


def _date_from(m, p, today: date) -> date | None:
    parts = _date_parts(m, p)
    if parts is None:
        return None
    month, day, year, label = parts
    return _resolve_year(month, day, year, today, label)


def _dayword_date(m, today: date) -> date | None:
    dw = (m.group("dw") or "").lower()
    if not dw:
        return None
    if dw in ("today", "tonight", "aaj"):
        return today
    if dw == "yesterday":
        return today - timedelta(days=1)
    if "before yesterday" in dw:
        return today - timedelta(days=2)
    n = _num(m.group("dwn"))
    return today - timedelta(days=n) if n is not None else None


def _finish(start_local, end_local, ref, tz_name, kind, phrase, note="") -> TimeRange | Clarification:
    start, end = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    if not start < end:
        return Clarification("The end of that time range is not after its start — which range did you mean?",
                             phrase, ["end_not_after_start"])
    if start >= ref:
        return Clarification("That time range hasn't started yet, so no posts can exist in it yet — "
                             "did you mean an earlier date?", phrase, ["future"])
    capped = end > ref
    return TimeRange(start=start, end=min(end, ref), tz_name=tz_name, kind=kind, phrase=phrase,
                     capped=capped, note=note)


def _clean(text: str) -> str:
    t = (text or "").lower()
    t = t.replace("’", "'").replace("–", "-").replace("—", "-")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    t = re.sub(r"\s+", " ", t)
    return t


def parse_time_range(text: str, reference_utc: datetime, tz_name: str = None):
    """TimeRange, Clarification, or None. `reference_utc` must be aware (it is
    treated as UTC if naive) and is the only notion of "now" used."""
    ref = to_utc(reference_utc)
    if ref is None:
        raise ValueError("reference_utc must be a datetime")
    tz, tz_name = resolve_tz(tz_name)
    t = _clean(text)
    if not t:
        return None
    today = ref.astimezone(tz).date()
    try:
        return (_explicit_date_range(t, ref, tz, tz_name, today)
                or _day_with_time_range(t, ref, tz, tz_name, today)
                or _day_part(t, ref, tz, tz_name, today)
                or _rolling(t, ref, tz_name)
                or _point_ago(t, ref, tz_name)
                or _week_month(t, ref, tz, tz_name, today)
                or _single_day(t, ref, tz, tz_name, today))
    except _Ambiguous as exc:
        return Clarification(exc.question, t)


# 1) "October 9 to October 12", "Oct 9 at 10 AM to Oct 12 at 3 PM", "between 2026-10-09 and 2026-10-12", "Oct 9 - 12"
def _explicit_date_range(t, ref, tz, tz_name, today):
    rx = re.compile(
        r"(?:from|between)?\s*" + _DATE_RE.format(p="a") + r"(?:,?\s*(?:at\s+)?" + _TIME_RE.format(p="ta") + r")?"
        + _RANGE_SEP +
        r"(?:" + _DATE_RE.format(p="b") + r"|(?P<bdayonly>\d{1,2})(?!\s*(?::|a\.?m|p\.?m)))"
        + r"(?:,?\s*(?:at\s+)?" + _TIME_RE.format(p="tb") + r")?")
    for m in rx.finditer(t):
        g = m.groupdict()
        if not any(g.get(k) for k in ("amon", "amon2", "aiso", "anum")):
            continue
        a_month, a_day, a_year, a_label = _date_parts(m, "a")
        if g.get("bdayonly"):                                   # "Oct 9 - 12": same month (and year) as the start
            b_parts = (a_month, int(g["bdayonly"]), a_year, f"{a_label.split(' ')[0]} {g['bdayonly']}")
        else:
            b_parts = _date_parts(m, "b")
        if b_parts is None:
            continue
        b_month, b_day, b_year, b_label = b_parts
        if b_year is None and a_year is not None:
            b_year = a_year if (b_month, b_day) >= (a_month, a_day) else a_year + 1
        d2 = _resolve_year(b_month, b_day, b_year, today, b_label)
        if a_year is None:
            # the start belongs to the end's year — or, only when the months wrap
            # ("Dec 28 to Jan 3"), the year before. "Oct 12 to Oct 9" is not guessed.
            if (a_month, a_day) <= (b_month, b_day):
                a_year = d2.year
            elif a_month > b_month:
                a_year = d2.year - 1
            else:
                raise _Ambiguous(f"{a_label} comes after {b_label} — which range did you mean?")
        d1 = _make_date(a_month, a_day, a_year, a_label)
        t1, t2 = _time_from(m, "ta"), _time_from(m, "tb")
        start = _at(d1, t1, tz) if t1 else _local_midnight(d1, tz)
        end = _at(d2, t2, tz) if t2 else _local_midnight(d2 + timedelta(days=1), tz)
        return _finish(start, end, ref, tz_name, "date_range", m.group(0).strip())
    return None


# 2) "today between 12 AM and 3 PM", "between 9 AM and noon today", "yesterday from 9pm to midnight", "October 9 from 10 AM to 2 PM"
def _day_with_time_range(t, ref, tz, tz_name, today):
    times = (r"(?:from|between)\s+" + _TIME_RE.format(p="x") + _RANGE_SEP + _TIME_RE.format(p="y"))
    day = r"(?:" + _DAYWORD_RE + r"|(?:on\s+)?" + _DATE_RE.format(p="d") + r")"
    for rx in (re.compile(day + r",?\s*(?:,\s*)?" + times), re.compile(times + r"\s*(?:on\s+)?" + day + r"?")):
        m = rx.search(t)
        if not m:
            continue
        g = m.groupdict()
        d = _dayword_date(m, today) if g.get("dw") else (_date_from(m, "d", today)
                                                          if any(g.get(k) for k in ("dmon", "dmon2", "diso", "dnum")) else today)
        t1, t2 = _time_from(m, "x"), _time_from(m, "y")
        if t1 is None or t2 is None:
            continue
        start = _at(d, t1, tz)
        end_day = d + timedelta(days=1) if (t2 <= t1 and t2 == time(0, 0)) else d
        end = _at(end_day, t2, tz)
        note = "" if (g.get("dw") or any(g.get(k) for k in ("dmon", "dmon2", "diso", "dnum"))) else "No day was given, so today was used."
        return _finish(start, end, ref, tz_name, "time_range", m.group(0).strip(), note)
    # a clock range with times that carry no AM/PM: ask
    m = re.search(r"(?:from|between)\s+(\d{1,2})\s*(?:to|and|-)\s*(\d{1,2})\b(?!\s*(?:a\.?m|p\.?m|/|days?|hours?|minutes?))", t)
    if m and re.search(r"\b(today|yesterday|morning|afternoon|evening|night|o'?clock)\b", t):
        raise _Ambiguous(f"Do you mean {m.group(1)} to {m.group(2)} AM or PM?")
    return None


_PARTS = {"morning": (time(5, 0), time(12, 0)), "afternoon": (time(12, 0), time(17, 0)),
          "evening": (time(17, 0), time(21, 0))}


# 3) "this morning", "yesterday afternoon", "last night", "tonight"
def _day_part(t, ref, tz, tz_name, today):
    m = re.search(r"\b(this|yesterday|today)\s+(morning|afternoon|evening)\b", t)
    if m:
        d = today if m.group(1) in ("this", "today") else today - timedelta(days=1)
        a, b = _PARTS[m.group(2)]
        return _finish(_at(d, a, tz), _at(d, b, tz), ref, tz_name, "day_part", m.group(0),
                       f"'{m.group(2)}' is read as {_fmt(datetime(2000, 1, 1, a.hour), False)}–"
                       f"{_fmt(datetime(2000, 1, 1, b.hour), False)} local time.")
    m = re.search(r"\blast night\b", t)
    if m:
        y = today - timedelta(days=1)
        return _finish(_at(y, time(18, 0), tz), _at(today, time(6, 0), tz), ref, tz_name, "day_part", m.group(0),
                       "'last night' is read as 6:00 PM yesterday to 6:00 AM today, local time.")
    m = re.search(r"\btonight\b", t)
    if m:
        return _finish(_at(today, time(18, 0), tz), _local_midnight(today + timedelta(days=1), tz), ref, tz_name,
                       "day_part", m.group(0), "'tonight' is read as 6:00 PM to midnight, local time.")
    return None


# 4) "last 2 hours", "past 24 hours", "in the last minute", "since 3 hours ago", "within the past week"
def _rolling(t, ref, tz_name):
    m = re.search(r"\b(?:in |within |over |during )?(?:the )?(?:last|past|previous|recent)\s+(?:" + _NUM_RE + r"\s+)?"
                  + _UNIT_RE + r"\b", t)
    if not m:
        m = re.search(r"\bsince\s+" + _NUM_RE + r"\s+" + _UNIT_RE + r"\s+ago\b", t)
    if not m:
        return None
    n = _num(m.group("num")) if m.group("num") else 1
    secs = _unit_seconds(m.group("unit"))
    if not n or not secs:
        return None
    start = ref - timedelta(seconds=n * secs)
    return TimeRange(start=start, end=ref, tz_name=tz_name, kind="rolling", phrase=m.group(0).strip(),
                     note=f"Rolling window: the {_unit_name(secs, n)} before this search.")


# 5) "3 hours ago", "1 minute ago", "2 weeks ago" (days are calendar days — see _single_day)
def _point_ago(t, ref, tz_name):
    m = re.search(r"\b" + _NUM_RE + r"\s+" + _UNIT_RE + r"\s+ago\b", t)
    if not m:
        return None
    secs = _unit_seconds(m.group("unit"))
    if secs == 86400:
        return None                       # "N days ago" = a calendar day
    n = _num(m.group("num"))
    if not n or not secs:
        return None
    start, end = ref - timedelta(seconds=(n + 1) * secs), ref - timedelta(seconds=n * secs)
    label, older = _unit_name(secs, n), _unit_name(secs, n + 1)
    return TimeRange(start=start, end=end, tz_name=tz_name, kind="point", phrase=m.group(0).strip(),
                     note=(f"'{label} ago' is a point in time. It is read the way Reddit labels a post "
                           f"'{label} ago': published between {older} and {label} before this search. "
                           f"For everything since then, ask for 'the last {label}'."))


# 6) "this week", "last week", "this month", "last month"
def _week_month(t, ref, tz, tz_name, today):
    # "last week" / "past week" are rolling 7-day windows (handled by _rolling first)
    m = re.search(r"\b(this|last|previous)\s+(week|month)\b", t)
    if not m:
        return None
    which, unit = m.group(1), m.group(2)
    if unit == "week":
        monday = today - timedelta(days=today.weekday())
        if which == "this":
            return _finish(_local_midnight(monday, tz), ref.astimezone(tz), ref, tz_name, "week", m.group(0),
                           "Weeks start on Monday, local time.")
        return _finish(_local_midnight(monday - timedelta(days=7), tz), _local_midnight(monday, tz), ref, tz_name,
                       "week", m.group(0), "The previous Monday-to-Sunday week, local time.")
    first = today.replace(day=1)
    if which == "this":
        return _finish(_local_midnight(first, tz), ref.astimezone(tz), ref, tz_name, "month", m.group(0))
    prev_first = (first - timedelta(days=1)).replace(day=1)
    return _finish(_local_midnight(prev_first, tz), _local_midnight(first, tz), ref, tz_name, "month", m.group(0),
                   "The previous calendar month, local time.")


# 7) "today", "yesterday", "2 days ago", "on October 9"
def _single_day(t, ref, tz, tz_name, today):
    m = re.search(r"\b" + _DAYWORD_RE + r"\b", t)
    if m:
        d = _dayword_date(m, today)
        if d is not None:
            note = "A calendar day in your timezone (not a rolling 24-hour window)." if "ago" in m.group(0) else ""
            return _finish(_local_midnight(d, tz), _local_midnight(d + timedelta(days=1), tz), ref, tz_name, "day",
                           m.group(0), note)
    m = re.search(r"\b(?:on|from|for|of|posted)\s+" + _DATE_RE.format(p="s") + r"(?!\s*(?:to|-|until|through))", t)
    if not m:
        m = re.search(r"(?<![\w/])" + _DATE_RE.format(p="s") + r"(?![\w/:])", t)
        if m and m.group("snum"):
            m = None                      # a bare 10/9 elsewhere in a sentence is too weak a signal
    if m:
        d = _date_from(m, "s", today)
        if d is not None:
            return _finish(_local_midnight(d, tz), _local_midnight(d + timedelta(days=1), tz), ref, tz_name, "day",
                           m.group(0).strip())
    return None
