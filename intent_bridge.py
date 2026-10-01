"""
intent_bridge.py — Intent Prototype ↔ Production Pipeline Bridge
=================================================================
This module is the ONLY place that calls into intent_prototype from the
production pipeline.  It is activated only when INTENT_BRIDGE_ENABLED=true
(off by default).

RESPONSIBILITIES
----------------
  rerank_with_intent(user_query, candidates, evidence_required, ...)
      Takes the candidates already returned by get_matched_signals() and
      re-ranks them by buyer-intent classification.  If any step fails or
      the total bridge time exceeds INTENT_BRIDGE_TIMEOUT_SECONDS, the
      original unmodified candidates are returned — never fewer posts.

  get_query_intent_summary(user_query)
      Thin wrapper around query_interpreter.interpret().  Returns the raw
      QueryIntent dict or None on error.  Used for diagnostics.

DESIGN CONSTRAINTS
------------------
  - FAIL-SAFE: every code path is wrapped in try/except.  Any failure
    at any stage returns the original candidates unchanged.
  - RETURN FORMAT INVARIANT: always returns
    [{"title": …, "post_text": …, "post_url": …, "platform": …}]
    Nothing is added.  Nothing is removed unless intent filtering kept it.
    Fill-to-N guarantees the returned list is never shorter than what
    production would have returned.
  - READ-ONLY: this module contains no MongoDB write calls.  The only
    permitted write is in classification_cache.py (save_many to the
    intent_classification_cache collection).
  - TIMEOUT: a concurrent.futures.ThreadPoolExecutor + Future.result(timeout)
    pattern wraps the entire bridge computation so it cannot block the
    production response path indefinitely.
  - NO PRODUCTION MODIFICATIONS: import nothing from logics.py, routes.py,
    flintel.py, or index.py.  Those files import from here, not the reverse.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import List, Optional, Dict, Any

log = logging.getLogger("flintel.intent_bridge")

# ── Sentinel returned by any failing internal step ───────────────────────────
_FAIL = object()


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════

def rerank_with_intent(
    user_query: str,
    candidates: List[dict],
    evidence_required: int,
    *,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> List[dict]:
    """Re-rank `candidates` by intent classification for `user_query`.

    Parameters
    ----------
    user_query        : The user's natural-language query string.
    candidates        : Production's similarity-ranked candidates — list of
                        {"title", "post_text", "post_url", "platform"} dicts.
    evidence_required : Minimum number of posts the caller expects back.
                        The fill-to-N step ensures we never return fewer.
    config_overrides  : Optional dict of INTENT_* config values to override
                        the defaults from config.py (useful in tests).

    Returns
    -------
    List of {"title", "post_text", "post_url", "platform"} dicts —
    intent-ranked when the bridge succeeds, original order otherwise.
    The list is never shorter than min(len(candidates), evidence_required).
    """
    if not candidates:
        return candidates

    cfg = _load_config(config_overrides)
    if not cfg.get("INTENT_BRIDGE_ENABLED"):
        return candidates

    if not user_query or not user_query.strip():
        log.debug("intent_bridge: empty user_query — skipping bridge")
        return candidates

    t_start = time.perf_counter()
    timeout  = cfg.get("INTENT_BRIDGE_TIMEOUT_SECONDS", 25)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            _run_bridge, user_query, candidates, evidence_required, cfg
        )
        try:
            result = future.result(timeout=timeout)
        except FuturesTimeout:
            log.warning(
                f"intent_bridge: timed out after {timeout}s "
                f"— returning original {len(candidates)} candidates"
            )
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return candidates
    except Exception as exc:
        log.warning(f"intent_bridge: executor error — {exc} — returning originals")
        executor.shutdown(wait=False, cancel_futures=True)
        return candidates
    finally:
        # Shutdown without blocking on the worker thread — it may still be
        # sleeping after a timeout and we must not wait for it here.
        executor.shutdown(wait=False, cancel_futures=True)

    elapsed = (time.perf_counter() - t_start) * 1000
    if result is _FAIL or result is None:
        log.info(f"intent_bridge: bridge failed ({elapsed:.0f}ms) — returning originals")
        return candidates

    log.info(
        f"intent_bridge: reranked {len(candidates)} → {len(result)} posts "
        f"in {elapsed:.0f}ms"
    )
    return result


def get_query_intent_summary(user_query: str) -> Optional[dict]:
    """Return the QueryIntent dict for user_query, or None on any error.

    This is a diagnostic helper; it has no effect on the production pipeline.
    One Claude call (the interpreter) is made if ANTHROPIC_API_KEY is set.
    """
    try:
        from intent_prototype import query_interpreter  # noqa: PLC0415
        return query_interpreter.interpret(user_query)
    except Exception as exc:
        log.warning(f"get_query_intent_summary failed: {exc}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# INTERNAL IMPLEMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def _run_bridge(
    user_query: str,
    candidates: List[dict],
    evidence_required: int,
    cfg: dict,
) -> List[dict]:
    """The full bridge computation, intended to run inside a ThreadPoolExecutor.

    Steps
    -----
    1. Interpret query → QueryIntent (or reuse cached summary).
    2. Check classification cache for as many posts as possible.
    3. Classify uncached posts in parallel batches (short-circuit eligible).
    4. Save newly classified posts to cache.
    5. Intent-filter + rank by opportunity/ranking_score.
    6. Fill-to-N: if ranked < evidence_required, append remaining
       similarity-ordered candidates (no double-counting).
    7. Return final list (same dict shape as input).
    """
    try:
        # ── 1. Interpret query ─────────────────────────────────────────────
        qi = _interpret(user_query)
        if qi is _FAIL:
            return _FAIL  # type: ignore[return-value]

        # ── 2. Check classification cache ──────────────────────────────────
        urls = [c.get("post_url", "") for c in candidates]
        cache_hits = _cache_get(urls, cfg)  # dict: post_url → cache doc

        # ── 3. Classify uncached posts in parallel batches ─────────────────
        uncached_posts  = [c for c in candidates if c.get("post_url") not in cache_hits]
        new_results     = _classify_parallel(uncached_posts, qi, cfg)
        if new_results is _FAIL:
            return _FAIL  # type: ignore[return-value]

        # ── 4. Save newly classified posts to cache ────────────────────────
        _cache_save(new_results, cfg)

        # ── 5. Merge all classifications, intent-filter, rank ──────────────
        # Build a unified classification map: post_url → classification dict
        cls_map: Dict[str, dict] = {}
        for hit_url, hit_doc in cache_hits.items():
            cls_map[hit_url] = {
                "intents":    hit_doc.get("intents", []),
                "confidence": hit_doc.get("confidence", 0.0),
                "from_cache": True,
            }
        for item in new_results:
            url = item.get("post_url", "")
            if url:
                cls_map[url] = {
                    "intents":    item.get("intents", []),
                    "confidence": item.get("confidence", 0.0),
                    "from_cache": False,
                }

        intent_include = qi.get("intent_include") or []
        intent_logic   = qi.get("intent_logic", "OR")

        passing    = []  # (candidate_dict, confidence) — intent-matched
        non_passing = [] # candidate_dicts that did not match intent filter

        for cand in candidates:
            url = cand.get("post_url", "")
            cls = cls_map.get(url)
            if cls is None:
                # No classification available — treat as non-passing
                non_passing.append(cand)
                continue

            matched = _intent_matches(cls.get("intents", []), intent_include, intent_logic)
            if matched:
                passing.append((cand, float(cls.get("confidence", 0.0))))
            else:
                non_passing.append(cand)

        # Sort passing by confidence descending (higher confidence first)
        passing.sort(key=lambda x: x[1], reverse=True)
        ranked = [cand for cand, _ in passing]

        # ── 6. Fill-to-N ───────────────────────────────────────────────────
        ranked = _fill_to_n(ranked, candidates, non_passing, evidence_required)

        return ranked

    except Exception as exc:
        log.warning(f"intent_bridge._run_bridge: unhandled error — {exc}")
        return _FAIL  # type: ignore[return-value]


def _interpret(user_query: str):
    """Call query_interpreter.interpret(); return QueryIntent dict or _FAIL."""
    try:
        from intent_prototype import query_interpreter  # noqa: PLC0415
        return query_interpreter.interpret(user_query)
    except Exception as exc:
        log.warning(f"intent_bridge._interpret failed: {exc}")
        return _FAIL


def _cache_get(post_urls: List[str], cfg: dict) -> dict:
    """Return cache hits dict from classification_cache.get_many().

    Always returns a dict (possibly empty) — never raises.
    """
    if not cfg.get("INTENT_CACHE_ENABLED", True):
        return {}
    try:
        from intent_prototype.classification_cache import get_many  # noqa: PLC0415
        return get_many(post_urls)
    except Exception as exc:
        log.debug(f"intent_bridge._cache_get failed (non-fatal): {exc}")
        return {}


def _cache_save(new_results: list, cfg: dict) -> None:
    """Persist newly classified items via classification_cache.save_many().

    Only saves items that were successfully classified (no _error field).
    Never raises.
    """
    if not cfg.get("INTENT_CACHE_ENABLED", True) or not new_results:
        return
    # Filter out error/unclassified placeholders — they must not pollute
    # the cache with low-confidence ambiguous data that would block future
    # real classifications from being fetched and stored.
    # _unclassified() always produces intent_confidence=0.0; real LLM
    # classifications always return a positive (possibly small) confidence,
    # so this is a safe proxy for "was actually classified".
    good_results = [r for r in new_results if r.get("confidence", 0.0) > 0.0]
    if not good_results:
        return
    try:
        from intent_prototype.classification_cache import save_many  # noqa: PLC0415
        save_many(good_results)
    except Exception as exc:
        log.debug(f"intent_bridge._cache_save failed (non-fatal): {exc}")


def _classify_parallel(
    posts: List[dict],
    qi: dict,
    cfg: dict,
) -> list:
    """Classify `posts` using doc_classifier, potentially in parallel batches.

    Returns a list of classification result dicts, each containing at minimum:
        { "post_url": str, "intents": [str], "confidence": float }

    Returns _FAIL only when the classifier import itself fails.
    Any partial failure is absorbed: failed posts simply won't appear in the
    result — they'll fall back to non-passing in the merge step.

    SHORT-CIRCUIT: if the short-circuit conditions are met (see schemas.py),
    classify only the top SHORTCIRCUIT_HEAD posts first; if enough pass,
    skip the tail.
    """
    if not posts:
        return []

    try:
        from intent_prototype import doc_classifier, schemas  # noqa: PLC0415
    except Exception as exc:
        log.warning(f"intent_bridge._classify_parallel: import error — {exc}")
        return _FAIL  # type: ignore[return-value]

    head_n = cfg.get("INTENT_SHORTCIRCUIT_HEAD", 50)
    min_passing = cfg.get("INTENT_SHORTCIRCUIT_MIN_PASSING", 15)
    min_conf    = cfg.get("INTENT_SHORTCIRCUIT_MIN_CONFIDENCE", 0.70)
    n_batches   = cfg.get("INTENT_CLASSIFY_PARALLEL_BATCHES", 3)
    intent_include = qi.get("intent_include") or []
    intent_logic   = qi.get("intent_logic", "OR")

    try:
        # ── Short-circuit check ────────────────────────────────────────────
        if len(posts) > head_n:
            head_docs = [_post_to_doc(p) for p in posts[:head_n]]
            head_cls  = _classify_batch(head_docs, doc_classifier)

            strong = sum(
                1 for cls in head_cls
                if _intent_matches(
                    [cls.get("intent")] if cls.get("intent") else [],
                    intent_include, intent_logic
                ) and (cls.get("intent_confidence") or 0.0) >= min_conf
            )

            if strong >= min_passing:
                log.debug(
                    f"intent_bridge: short-circuit triggered "
                    f"({strong}/{head_n} strong) — skipping tail"
                )
                return _merge_cls_with_posts(posts[:head_n], head_cls)

            # Classify tail in parallel batches
            tail_posts = posts[head_n:]
        else:
            head_docs = None
            head_cls  = None
            tail_posts = posts

        # ── Classify in N parallel batches ────────────────────────────────
        all_results = []

        if head_docs is not None and head_cls is not None:
            # Already have head results
            all_results.extend(_merge_cls_with_posts(posts[:head_n], head_cls))

        if tail_posts:
            batch_size = max(1, (len(tail_posts) + n_batches - 1) // n_batches)
            batches    = [
                tail_posts[i : i + batch_size]
                for i in range(0, len(tail_posts), batch_size)
            ]

            if len(batches) == 1:
                docs = [_post_to_doc(p) for p in batches[0]]
                cls  = _classify_batch(docs, doc_classifier)
                all_results.extend(_merge_cls_with_posts(batches[0], cls))
            else:
                with ThreadPoolExecutor(max_workers=n_batches) as pool:
                    futures_map = {}
                    for batch in batches:
                        docs_batch = [_post_to_doc(p) for p in batch]
                        f = pool.submit(_classify_batch, docs_batch, doc_classifier)
                        futures_map[f] = batch

                    for f, batch in futures_map.items():
                        try:
                            cls = f.result()
                            all_results.extend(_merge_cls_with_posts(batch, cls))
                        except Exception as exc:
                            log.debug(
                                f"intent_bridge: batch classify failed: {exc} "
                                f"— {len(batch)} posts treated as non-passing"
                            )

        return all_results

    except Exception as exc:
        log.warning(f"intent_bridge._classify_parallel error: {exc}")
        return []


def _classify_batch(docs: list, doc_classifier) -> list:
    """Call doc_classifier.classify(docs) and return the classification list."""
    return doc_classifier.classify(docs)


def _post_to_doc(post: dict) -> dict:
    """Convert a production candidate dict to the shape doc_classifier expects."""
    return {
        "post_title": post.get("title", ""),
        "post_text":  post.get("post_text", ""),
        "post_url":   post.get("post_url", ""),
        "platform":   post.get("platform", ""),
        # doc_classifier also reads these aliases if post_title is absent
        "title":      post.get("title", ""),
        "text":       post.get("post_text", ""),
        "url":        post.get("post_url", ""),
    }


def _merge_cls_with_posts(posts: List[dict], classifications: list) -> list:
    """Zip post dicts with their classification results into a flat result list.

    Returns list of dicts:
        { "post_url": str, "intents": [str], "confidence": float, "intent": str }
    """
    results = []
    for post, cls in zip(posts, classifications):
        if cls is None:
            continue
        intent     = cls.get("intent") or ""
        intents    = [intent] if intent else []
        if cls.get("secondary_intent"):
            intents.append(cls["secondary_intent"])
        confidence = float(cls.get("intent_confidence") or 0.0)
        results.append({
            "post_url":   post.get("post_url", ""),
            "intents":    intents,
            "confidence": confidence,
            # preserve extra fields for ranking
            "intent":           intent,
            "intent_confidence": confidence,
            "secondary_intent": cls.get("secondary_intent"),
            "commercial_signal": float(cls.get("commercial_signal") or 0.0),
            "urgency":           float(cls.get("urgency") or 0.0),
            "pain_intensity":    float(cls.get("pain_intensity") or 0.0),
        })
    return results


def _intent_matches(intents: List[str], intent_include: List[str], logic: str) -> bool:
    """Return True if `intents` satisfies the intent filter.

    When intent_include is empty, every post passes (no filter).
    Logic "OR" → at least one intent from intents is in intent_include.
    Logic "AND" → ALL items in intent_include appear in intents.
    """
    if not intent_include:
        return True
    if not intents:
        return False
    if logic == "AND":
        return all(inc in intents for inc in intent_include)
    # OR (default)
    return any(inc in intents for inc in intent_include)


def _fill_to_n(
    ranked:      List[dict],
    original:    List[dict],
    non_passing: List[dict],
    n:           int,
) -> List[dict]:
    """Guarantee the returned list has at least min(len(original), n) posts.

    If intent-ranked results < n, append non-passing candidates in their
    original similarity order — no duplicates (checked by post_url).
    """
    if len(ranked) >= n:
        return ranked

    seen_urls = {c.get("post_url", "") for c in ranked}

    # Fill from non-passing first (already in original similarity order)
    for cand in non_passing:
        if len(ranked) >= n:
            break
        url = cand.get("post_url", "")
        if url not in seen_urls:
            ranked.append(cand)
            seen_urls.add(url)

    # If still short, fill from remaining originals (belt-and-suspenders)
    if len(ranked) < n:
        for cand in original:
            if len(ranked) >= n:
                break
            url = cand.get("post_url", "")
            if url not in seen_urls:
                ranked.append(cand)
                seen_urls.add(url)

    return ranked


def _load_config(overrides: Optional[dict]) -> dict:
    """Load all INTENT_BRIDGE_* settings from config.py, apply overrides.

    Returns a plain dict so the rest of the bridge never imports config
    directly — only this function does, and any import failure defaults
    gracefully to INTENT_BRIDGE_ENABLED=False.
    """
    defaults = {
        "INTENT_BRIDGE_ENABLED":             False,
        "INTENT_CANDIDATE_MULTIPLIER":       4,
        "INTENT_CANDIDATE_MIN":              100,
        "INTENT_CANDIDATE_MAX":              200,
        "INTENT_SHORTCIRCUIT_HEAD":          50,
        "INTENT_SHORTCIRCUIT_MIN_PASSING":   15,
        "INTENT_SHORTCIRCUIT_MIN_CONFIDENCE": 0.70,
        "INTENT_CLASSIFY_PARALLEL_BATCHES":  3,
        "INTENT_BRIDGE_TIMEOUT_SECONDS":     25,
        "INTENT_CACHE_ENABLED":              True,
        "INTENT_CACHE_TTL_DAYS":             30,
        "INTENT_CACHE_COLLECTION":           "intent_classification_cache",
    }
    try:
        import config  # noqa: PLC0415
        for key in defaults:
            if hasattr(config, key):
                defaults[key] = getattr(config, key)
    except Exception as exc:
        log.debug(f"intent_bridge._load_config: config import failed: {exc}")

    if overrides:
        defaults.update(overrides)

    return defaults
