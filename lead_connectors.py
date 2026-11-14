"""
lead_connectors.py — the thin adapters between lead_engine and the retrieval code that already exists. Each connector has
`key`, `label`, `retrieval_path` and `fetch(concepts, since_days, limit) -> {"status", "records", "detail"}` and NEVER invents records.

Statuses use the existing vocabulary (ok | empty | partial | failed | rate_limited | unavailable | skipped); lead_engine.coverage_of turns them
into the six user-facing states. A connector that cannot tell whether it finished must not say "ok" for a partial run.

  hackernews  LIVE Algolia search of Hacker News STORIES (comments are not searched), via hackernews_live.fetch (honest per-request status).
  reddit      STORED Reddit conversations from the application's own database via the existing matcher (`search` callable, injected by the
              route). It is a historical store, not a live search; it needs the production database and embedding service, so it is
              exercised in tests only through an injected fake.
"""
from datetime import datetime, timezone

import hackernews_live
import platforms


def _queries(concepts, n=6):
    out, seen = [], set()
    for c in concepts or []:
        q = c.get("query")
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
        if len(out) >= n:
            break
    return out


class HackerNewsConnector:
    key, label, retrieval_path = "hackernews", "Hacker News", "live"

    def __init__(self, http_get=None, now=None):
        self.http_get, self.now = http_get, now

    def fetch(self, concepts, since_days, limit):
        now = self.now or datetime.now(timezone.utc)
        start, end = hackernews_live.window_bounds(since_days, None, now)
        records, status = hackernews_live.fetch(_queries(concepts), pool_n=int(limit), start=start, end=end, by_date=True,
                                                http_get=self.http_get, now=now)
        state = status.get("state") or "failed"
        detail = {"ok": "Live search of Hacker News stories (comments are not searched).",
                  "empty": "Live search of Hacker News stories found nothing in the period.",
                  "partial": "Some Hacker News requests failed or timed out.", "failed": "The Hacker News search failed.",
                  "rate_limited": "Hacker News rate-limited the search.", "skipped": "No usable search terms."}.get(state, "")
        return {"status": state, "records": records, "detail": detail}


class StoredRedditConnector:
    key, label, retrieval_path = "reddit", "Reddit", "historical"

    def __init__(self, search):
        self.search = search                       # search(queries, since_days, limit) -> list of stored post dicts; may raise

    def fetch(self, concepts, since_days, limit):
        queries = _queries(concepts)
        if not queries:
            return {"status": "skipped", "records": [], "detail": "No usable search terms."}
        try:
            records = list(self.search(queries, since_days, limit) or [])
        except Exception:
            return {"status": "failed", "records": [], "detail": "The stored Reddit conversations could not be read."}
        return {"status": "ok" if records else "empty", "records": records,
                "detail": "Stored Reddit conversations (a historical archive, not a live search)."}


def available_sources() -> list:
    """What the registry says is enabled right now: [{"key","label","kind"}]. Only these may be offered to the user."""
    kind = {"reddit": "Stored conversations (historical)", "hackernews": "Live stories (comments not searched)"}
    return [{"key": k, "label": platforms.label(k, k), "kind": kind[k]} for k in platforms.enabled_keys() if k in kind]
