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
    topic_sims: Optional[List[float]] = None,
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
    topic_sims        : Optional list of embedding cosine-similarity scores,
                        parallel to `candidates`.  Passed to the ranker so
                        topic relevance contributes to the ranking score.
                        If None or length-mismatched, defaults to 0.0 per doc.
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

    # Validate topic_sims length; warn and default to zeros on mismatch.
    if topic_sims is not None and len(topic_sims) != len(candidates):
        log.warning(
            f"intent_bridge: topic_sims length {len(topic_sims)} != "
            f"candidates length {len(candidates)} — defaulting to 0.0"
        )
        topic_sims = None

    t_start = time.perf_counter()
    timeout  = cfg.get("INTENT_BRIDGE_TIMEOUT_SECONDS", 25)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            _run_bridge, user_query, candidates, evidence_required, cfg, topic_sims
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
    """Return the QueryIntent dict for user_query, or None on any error."""
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
    topic_sims: Optional[List[float]] = None,
) -> List[dict]:
    try:
        # ── 1. Interpret query ─────────────────────────────────────────────
        qi = _interpret(user_query)
        if qi is _FAIL:
            return _FAIL

        # ── 2. Check classification cache ──────────────────────────────────
        urls = [c.get("post_url", "") for c in candidates]
        cache_hits = _cache_get(urls, cfg)

        # ── 3. Classify uncached posts ─────────────────────────────────────
        uncached_posts = [c for c in candidates if c.get("post_url") not in cache_hits]
        new_results    = _classify_parallel(uncached_posts, qi, cfg)
        if new_results is _FAIL:
            return _FAIL

        # ── 4. Save newly classified posts to cache ────────────────────────
        _cache_save(new_results, cfg)

        # ── 5. Merge all classifications, intent-filter, rank ──────────────
        # Build index of topic_sims by post_url for O(1) lookup.
        sim_by_url: Dict[str, float] = {}
        if topic_sims is not None:
            for cand, sim in zip(candidates, topic_sims):
                url = cand.get("post_url", "")
                if url:
                    sim_by_url[url] = float(sim)

        # Build unified classification map.
        # Cache hits from get_many: {post_url, intents, confidence, schema_ver, cached_at}
        # New results from _merge_cls_with_posts: full field set including commercial_signal etc.
        cls_map: Dict[str, dict] = {}
        for hit_url, hit_doc in cache_hits.items():
            intents = hit_doc.get("intents") or []
            # Cache stores intents list; derive primary intent from first element.
            primary_intent = intents[0] if intents else ""
            cls_map[hit_url] = {
                "intents":           intents,
                "confidence":        float(hit_doc.get("confidence") or 0.0),
                "from_cache":        True,
                "intent":            primary_intent,
                "commercial_signal": 0.0,
                "urgency":           0.0,
                "pain_intensity":    0.0,
            }
        for item in new_results:
            url = item.get("post_url", "")
            if url:
                cls_map[url] = {
                    "intents":           item.get("intents", []),
                    "confidence":        item.get("confidence", 0.0),
                    "from_cache":        False,
                    "intent":            item.get("intent", ""),
                    "commercial_signal": float(item.get("commercial_signal") or 0.0),
                    "urgency":           float(item.get("urgency") or 0.0),
                    "pain_intensity":    float(item.get("pain_intensity") or 0.0),
                }

        intent_include = qi.get("intent_include") or []
        intent_logic   = qi.get("intent_logic", "OR")

        passing     = []  # (candidate_dict, topic_sim, cls_dict) — intent-matched
        non_passing = []  # candidate_dicts that did not match intent filter

        for cand in candidates:
            url = cand.get("post_url", "")
            cls = cls_map.get(url)
            if cls is None:
                non_passing.append(cand)
                continue
            matched = _intent_matches(cls.get("intents", []), intent_include, intent_logic)
            if matched:
                sim = sim_by_url.get(url, 0.0)
                passing.append((cand, sim, cls))
            else:
                non_passing.append(cand)

        ranked = _rank_passing(passing, qi)

        # ── 6. Fill-to-N ───────────────────────────────────────────────────
        ranked = _fill_to_n(ranked, candidates, non_passing, evidence_required)

        return ranked

    except Exception as exc:
        log.warning(f"intent_bridge._run_bridge: unhandled error — {exc}")
        return _FAIL

def _rank_passing(passing: list, qi: dict) -> List[dict]:
    """Sort intent-matching posts via ranker.rank() using opportunity.load_weights()."""
    if not passing:
        return []

    try:
        from intent_prototype import ranker, opportunity, schemas  # noqa: PLC0415
        weights = opportunity.load_weights()

        triples = []
        for cand, sim, cls in passing:
            url = cand.get("post_url", "")
            # ranker.flatten() de-duplicates by doc["id"]; set it to post_url.
            doc = {
                "id":        url,
                "title":     cand.get("title", ""),
                "post_text": cand.get("post_text", ""),
                "post_url":  url,
                "platform":  cand.get("platform", ""),
            }
            classification = schemas.normalize_classification({
                "i":                    1,
                "intent":               cls.get("intent", ""),
                "intent_confidence":    cls.get("confidence", 0.0),
                "secondary_intent":     None,
                "secondary_confidence": None,
                "actor_type":           None,
                "actor_role":           None,
                "commercial_signal":    cls.get("commercial_signal", 0.0),
                "pain_intensity":       cls.get("pain_intensity", 0.0),
                "urgency":              cls.get("urgency", 0.0),
                "specificity":          0.0,
                "geography":            None,
                "industry_hint":        None,
                "ambiguous":            False,
                "noise":                False,
            })
            triples.append((doc, sim, classification))

        result = ranker.rank(triples, qi, weights, top_n=len(passing))

        # sections[*]["results"] — each row has {"doc", "ranking_score", ...}
        ordered_docs = []
        for section in result.get("sections", []):
            for row in section.get("results", []):
                ordered_docs.append(row.get("doc") or row)

        url_to_cand = {cand.get("post_url", ""): cand for cand, _, _ in passing}
        ranked = []
        seen_urls = set()
        for doc in ordered_docs:
            url = doc.get("post_url", "") or doc.get("id", "")
            if url and url not in seen_urls and url in url_to_cand:
                ranked.append(url_to_cand[url])
                seen_urls.add(url)

        # Append any passing candidates the ranker dropped (shouldn't happen).
        for cand, _, _ in passing:
            url = cand.get("post_url", "")
            if url not in seen_urls:
                ranked.append(cand)
                seen_urls.add(url)

        return ranked

    except Exception as exc:
        log.warning(
            f"intent_bridge._rank_passing: ranker unavailable ({exc}) "
            f"— falling back to confidence sort"
        )
        passing_sorted = sorted(passing, key=lambda x: float(x[2].get("confidence", 0.0)), reverse=True)
        return [cand for cand, _, _ in passing_sorted]

def _interpret(user_query: str):
    try:
        from intent_prototype import query_interpreter  # noqa: PLC0415
        return query_interpreter.interpret(user_query)
    except Exception as exc:
        log.warning(f"intent_bridge._interpret failed: {exc}")
        return _FAIL

def _cache_get(post_urls: List[str], cfg: dict) -> dict:
    if not cfg.get("INTENT_CACHE_ENABLED", True):
        return {}
    try:
        from intent_prototype.classification_cache import get_many  # noqa: PLC0415
        return get_many(post_urls)
    except Exception as exc:
        log.warning(f"intent_bridge._cache_get failed (non-fatal): {exc}")
        return {}

def _cache_save(new_results: list, cfg: dict) -> None:
    if not cfg.get("INTENT_CACHE_ENABLED", True) or not new_results:
        return
    # Only cache items that were actually classified (confidence > 0).
    good_results = [r for r in new_results if r.get("confidence", 0.0) > 0.0]
    if not good_results:
        return
    try:
        from intent_prototype.classification_cache import save_many  # noqa: PLC0415
        # save_many(List[dict]) — each dict needs post_url, intents, confidence at minimum.
        save_many(good_results)
    except Exception as exc:
        log.warning(f"intent_bridge._cache_save failed (non-fatal): {exc}")

def _classify_parallel(posts: List[dict], qi: dict, cfg: dict) -> list:
    if not posts:
        return []

    try:
        from intent_prototype import doc_classifier, schemas  # noqa: PLC0415
    except Exception as exc:
        log.warning(f"intent_bridge._classify_parallel: import error — {exc}")
        return _FAIL

    head_n      = cfg.get("INTENT_SHORTCIRCUIT_HEAD", 50)
    min_passing = cfg.get("INTENT_SHORTCIRCUIT_MIN_PASSING", 15)
    min_conf    = cfg.get("INTENT_SHORTCIRCUIT_MIN_CONFIDENCE", 0.70)
    n_batches   = cfg.get("INTENT_CLASSIFY_PARALLEL_BATCHES", 3)
    intent_include = qi.get("intent_include") or []
    intent_logic   = qi.get("intent_logic", "OR")

    try:
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
                log.debug(f"intent_bridge: short-circuit triggered ({strong}/{head_n} strong) — skipping tail")
                return _merge_cls_with_posts(posts[:head_n], head_cls)

            tail_posts = posts[head_n:]
        else:
            head_docs  = None
            head_cls   = None
            tail_posts = posts

        all_results = []

        if head_docs is not None and head_cls is not None:
            all_results.extend(_merge_cls_with_posts(posts[:head_n], head_cls))

        if tail_posts:
            single_batch_limit = getattr(schemas, "CLASSIFIER_BATCH_SIZE", 17)
            if len(tail_posts) <= single_batch_limit:
                docs = [_post_to_doc(p) for p in tail_posts]
                cls  = _classify_batch(docs, doc_classifier)
                all_results.extend(_merge_cls_with_posts(tail_posts, cls))
            else:
                batch_size = max(1, (len(tail_posts) + n_batches - 1) // n_batches)
                batches    = [
                    tail_posts[i : i + batch_size]
                    for i in range(0, len(tail_posts), batch_size)
                ]
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
                            log.warning(
                                f"intent_bridge: batch classify failed: {exc} "
                                f"— {len(batch)} posts treated as non-passing"
                            )

        return all_results

    except Exception as exc:
        log.warning(f"intent_bridge._classify_parallel error: {exc}")
        return []

def _classify_batch(docs: list, doc_classifier) -> list:
    return doc_classifier.classify(docs)

def _post_to_doc(post: dict) -> dict:
    return {
        "post_title": post.get("title", ""),
        "post_text":  post.get("post_text", ""),
        "post_url":   post.get("post_url", ""),
        "platform":   post.get("platform", ""),
        "title":      post.get("title", ""),
        "text":       post.get("post_text", ""),
        "url":        post.get("post_url", ""),
    }

def _merge_cls_with_posts(posts: List[dict], classifications: list) -> list:
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
            "post_url":          post.get("post_url", ""),
            "intents":           intents,
            "confidence":        confidence,
            "intent":            intent,
            "intent_confidence": confidence,
            "secondary_intent":  cls.get("secondary_intent"),
            "commercial_signal": float(cls.get("commercial_signal") or 0.0),
            "urgency":           float(cls.get("urgency") or 0.0),
            "pain_intensity":    float(cls.get("pain_intensity") or 0.0),
        })
    return results

def _intent_matches(intents: List[str], intent_include: List[str], logic: str) -> bool:
    if not intent_include:
        return True
    if not intents:
        return False
    if logic == "AND":
        return all(inc in intents for inc in intent_include)
    return any(inc in intents for inc in intent_include)

def _fill_to_n(ranked: List[dict], original: List[dict], non_passing: List[dict], n: int) -> List[dict]:
    if len(ranked) >= n:
        return ranked

    seen_urls = {c.get("post_url", "") for c in ranked}

    for cand in non_passing:
        if len(ranked) >= n:
            break
        url = cand.get("post_url", "")
        if url not in seen_urls:
            ranked.append(cand)
            seen_urls.add(url)

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
    defaults = {
        "INTENT_BRIDGE_ENABLED":              False,
        "INTENT_CANDIDATE_MULTIPLIER":        4,
        "INTENT_CANDIDATE_MIN":               100,
        "INTENT_CANDIDATE_MAX":               200,
        "INTENT_SHORTCIRCUIT_HEAD":           50,
        "INTENT_SHORTCIRCUIT_MIN_PASSING":    15,
        "INTENT_SHORTCIRCUIT_MIN_CONFIDENCE": 0.70,
        "INTENT_CLASSIFY_PARALLEL_BATCHES":   3,
        "INTENT_BRIDGE_TIMEOUT_SECONDS":      25,
        "INTENT_CACHE_ENABLED":               True,
        "INTENT_CACHE_TTL_DAYS":              30,
        "INTENT_CACHE_COLLECTION":            "intent_classification_cache",
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
