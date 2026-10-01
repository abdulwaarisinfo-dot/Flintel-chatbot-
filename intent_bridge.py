"""
INTENT BRIDGE
============================================================================
Prototype (intent_prototype/) aur production (logics.py / routes.py) ke
beech ka jor.

    rerank_with_intent(user_query, candidates, evidence_required, *,
                       topic_sims=None, config_overrides=None) -> list
    get_query_intent_summary(user_query) -> dict | None

FAIL-SAFE CONTRACT: koi exception bahar nahi aati. Flag band ho, interpreter
fail ho, timeout ho, ya kuch bhi ghalat ho — candidates waisi hi wapas
(log.warning mein wajah ke saath). Return value hamesha candidates ke apne
dicts hain, unchanged (jab tak attach_intent override on na ho).

ASAL PROTOTYPE API (intent_prototype/ ki files parh kar):
    query_interpreter.interpret(query, model=None) -> QueryIntent dict
        (malformed reply par "_interpreter_fallback": True; khali query par
        ValueError)
    doc_classifier.classify(docs, batch_size=None, model=None, progress=None)
        -> list of dicts, docs ke index-aligned. docs mein "title"/"post_text".
        Key "intent_confidence". Fail batch => placeholder jis mein "_error".
    opportunity.load_weights(path=None) -> weights (weights.json)
    ranker.rank(candidates, qi, weights, top_n) / ranker.flatten(result)
        candidates = [(doc, topic_sim, classification)]
        flatten() doc["id"] se de-duplicate karta hai => "id" lazmi.
    classification_cache.get_many(urls) / save_many([(url, cls)])

Hard filter + intent gate + ranking ab poori tarah ranker.py ke andar hai;
bridge sirf adapter hai (koi apni filter/rank logic nahi).
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import config as _cfg

log = logging.getLogger(__name__)

_SUMMARY_STR_LIMIT = 300

_warned_once = set()


def _warn_once(key, msg, *args):
    if key not in _warned_once:
        _warned_once.add(key)
        log.warning(msg, *args)


# ── Settings (config + per-call overrides) ───────────────────────────────
def _settings(overrides):
    s = {
        "enabled":        bool(getattr(_cfg, "INTENT_BRIDGE_ENABLED", False)),
        "head":           int(getattr(_cfg, "INTENT_SHORTCIRCUIT_HEAD", 50)),
        "min_passing":    int(getattr(_cfg, "INTENT_SHORTCIRCUIT_MIN_PASSING", 15)),
        "min_confidence": float(getattr(_cfg, "INTENT_SHORTCIRCUIT_MIN_CONFIDENCE", 0.70)),
        "parallel":       max(1, int(getattr(_cfg, "INTENT_CLASSIFY_PARALLEL_BATCHES", 3))),
        "timeout":        float(getattr(_cfg, "INTENT_BRIDGE_TIMEOUT_SECONDS", 25)),
        "cache_enabled":  bool(getattr(_cfg, "INTENT_CACHE_ENABLED", True)),
        "batch_size":     None,    # None => schemas.CLASSIFIER_BATCH_SIZE
        "attach_intent":  False,   # True => candidate ki copy par "_intent" key
    }
    if overrides:
        s.update({k: v for k, v in overrides.items() if k in s})
    return s


# ── Lazy prototype loading (import-safe) ─────────────────────────────────
def _load(module_name):
    import importlib
    return importlib.import_module(f"intent_prototype.{module_name}")


# ── Adapters (asal prototype API) ────────────────────────────────────────
@lru_cache(maxsize=128)
def _interpret(user_query):
    """query_interpreter.interpret(query) -> QueryIntent dict."""
    intent = _load("query_interpreter").interpret(user_query)
    if not isinstance(intent, dict):
        raise RuntimeError("interpreter ne dict nahi diya")
    return intent


def _batch_size(s):
    if s["batch_size"]:
        return max(1, int(s["batch_size"]))
    try:
        return max(1, int(_load("schemas").CLASSIFIER_BATCH_SIZE))
    except Exception:
        return 17     # schemas.CLASSIFIER_BATCH_SIZE ka asal default


def _is_failed(cls):
    """
    Placeholder / fallback classification? doc_classifier._unclassified() "_error"
    set karta hai, lekin schemas.normalize_classification() naya dict banata hai
    jis mein "_error" key BACHTI NAHI — asal file mein sirf intent_confidence 0.0
    (aur ambiguous True) reh jata hai. Is liye dono signal dekhte hain:
    "_error" ya intent_confidence <= 0.0 (normalize unrecognised label par bhi
    0.0 deta hai — wo bhi "classification" nahi, fallback hai).
    """
    if not isinstance(cls, dict) or cls.get("_error"):
        return True
    try:
        return float(cls.get("intent_confidence") or 0.0) <= 0.0
    except (TypeError, ValueError):
        return True


def _classify_batch(batch, bs):
    """
    doc_classifier.classify(docs, batch_size=...) -> {post_url: cls} sirf THEEK
    classifications ke liye. Failed placeholders (_is_failed) wapas nahi aatin.
    Returns (good: dict, n_failed: int).
    """
    res = _load("doc_classifier").classify(batch, batch_size=bs)
    good, failed = {}, 0
    if not isinstance(res, (list, tuple)):
        return good, len(batch)
    for doc, cls in zip(batch, res):
        if not _is_failed(cls):
            good[doc["post_url"]] = cls
        else:
            failed += 1
    failed += max(0, len(batch) - len(res))
    return good, failed


def _rank_rows(qi, weights, entries):
    """
    entries: [(doc_for_rank, topic_sim, cls)]. ranker.rank + flatten ->
    ordered rows. top_n = len(entries) taake koi row truncate na ho.
    """
    if not entries:
        return []
    ranker = _load("ranker")
    result = ranker.rank(entries, qi, weights, top_n=len(entries))
    return ranker.flatten(result)


def _rank_doc(candidate, url):
    """Ranker ko doc ki shallow copy: "id" = post_url (flatten() de-dup ke liye).
    created_utc/source (hard_filter/opportunity.score ko chahiye) copy mein
    rehte hain agar candidate mein hon."""
    d = dict(candidate)
    d["id"] = url
    return d


# ── Core ─────────────────────────────────────────────────────────────────
def _run(user_query, candidates, evidence_required, topic_sims, s, deadline):
    cache = _load("classification_cache")
    opportunity = _load("opportunity")

    qi = _interpret(user_query)
    if qi.get("_interpreter_fallback"):
        log.warning("intent bridge: interpreter fallback (koi intent nahi mila) — "
                    "classify skip, purana result")
        return list(candidates)

    # topic_sims: candidates ke barabar floats; None / mismatch => 0.0
    sims = [0.0] * len(candidates)
    if topic_sims is not None:
        try:
            vals = [float(x) for x in topic_sims]
            if len(vals) == len(candidates):
                sims = vals
            else:
                log.warning("intent bridge: topic_sims length %d != candidates %d — 0.0 maana",
                            len(vals), len(candidates))
        except Exception as e:
            log.warning("intent bridge: topic_sims invalid (%s) — 0.0 maana", e)

    weights = opportunity.load_weights()
    if not weights.get("calibrated"):
        _warn_once("uncalibrated", "weights uncalibrated: starting values")

    urls = [c.get("post_url") for c in candidates]
    cls_map = cache.get_many([u for u in urls if u]) if s["cache_enabled"] else {}
    cls_map = dict(cls_map or {})

    stats = {"classified": 0, "failed": 0}
    lock = threading.Lock()

    def classify_missing(idxs):
        seen, todo = set(), []
        for i in idxs:
            u = urls[i]
            if u and u not in cls_map and u not in seen:
                seen.add(u)
                todo.append(candidates[i])
        if not todo:
            return
        bs = _batch_size(s)
        batches = [todo[i:i + bs] for i in range(0, len(todo), bs)]

        def work(batch):
            if time.monotonic() >= deadline:
                return {}, 0
            good, failed = _classify_batch(batch, bs)
            if good and s["cache_enabled"]:
                cache.save_many(list(good.items()))   # sirf theek wali; turant save
            return good, failed

        with ThreadPoolExecutor(max_workers=s["parallel"]) as ex:
            futs = [ex.submit(work, b) for b in batches]
            for f in as_completed(futs):
                try:
                    good, failed = f.result()
                except Exception as e:
                    log.warning("intent bridge: ek classify batch fail (%s) — skip", e)
                    continue
                with lock:
                    cls_map.update(good)
                    stats["classified"] += len(good)
                    stats["failed"] += failed
        if stats["failed"]:
            log.warning("intent bridge: %d posts classify nahi ho sakin (_error) — "
                        "cache mein save nahi", stats["failed"])

    def entries_for(idxs):
        out, seen = [], set()
        for i in idxs:
            u = urls[i]
            if not u or u in seen or u not in cls_map:
                continue
            seen.add(u)
            out.append((_rank_doc(candidates[i], u), sims[i], cls_map[u]))
        return out

    # ── short-circuit: pehle HEAD classify + rank ─────────────────────────
    all_idx = list(range(len(candidates)))
    head_idx = all_idx[:s["head"]]
    classify_missing(head_idx)

    if len(candidates) > len(head_idx):
        head_rows = _rank_rows(qi, weights, entries_for(head_idx))
        strong = sum(1 for r in head_rows
                     if float(r.get("effective_confidence") or 0.0) >= s["min_confidence"])
        if strong <= s["min_passing"]:
            classify_missing(all_idx[len(head_idx):])

    # ── final rank (sab classified posts) ────────────────────────────────
    rows = _rank_rows(qi, weights, entries_for(all_idx))

    # ranked row -> ASAL candidate dict (post_url se)
    first_by_url = {}
    for i, u in enumerate(urls):
        if u and u not in first_by_url:
            first_by_url[u] = i

    n = max(0, int(evidence_required))
    result, used = [], set()
    for r in rows:
        if len(result) >= n:
            break
        u = (r.get("doc") or {}).get("post_url")
        i = first_by_url.get(u)
        if i is None or i in used:
            continue
        d = candidates[i]
        if s["attach_intent"]:
            d = dict(d)
            d["_intent"] = {
                "confidence": r.get("effective_confidence"),
                "ranking_score": r.get("ranking_score"),
                "classification": r.get("classification"),
            }
        result.append(d)
        used.add(i)

    if not rows:
        log.warning("intent bridge: koi post intent filter se pass nahi hui — similarity order")

    # ── FILL TO N: bachi hui candidates similarity order mein ─────────────
    rest = sorted((i for i in all_idx if i not in used), key=lambda i: -sims[i])
    for i in rest:
        if len(result) >= n:
            break
        result.append(candidates[i])
    return result


def rerank_with_intent(user_query, candidates, evidence_required, *,
                       topic_sims=None, config_overrides=None):
    """Intent ke hisab se rerank; kisi bhi masle par candidates waisi hi wapas."""
    try:
        s = _settings(config_overrides)
        if not s["enabled"]:
            return candidates
        if not candidates:
            return candidates

        start = time.monotonic()
        deadline = start + s["timeout"]
        box = {}

        def target():
            try:
                box["res"] = _run(user_query, candidates, evidence_required,
                                  topic_sims, s, deadline)
            except Exception as e:
                box["err"] = e

        t = threading.Thread(target=target, daemon=True, name="intent-bridge")
        t.start()
        t.join(s["timeout"])
        if t.is_alive():
            log.warning("intent bridge: %.1fs timeout — purana result", s["timeout"])
            return candidates
        if "err" in box:
            log.warning("intent bridge: fail (%s) — purana result", box["err"])
            return candidates
        res = box.get("res")
        if not res:
            log.warning("intent bridge: khali result — purana result")
            return candidates
        return res
    except Exception as e:
        log.warning("intent bridge: unexpected error (%s) — purana result", e)
        return candidates


def _to_dict(obj):
    if isinstance(obj, dict):
        return obj
    for m in ("model_dump", "dict", "to_dict", "_asdict"):
        fn = getattr(obj, m, None)
        if callable(fn):
            return fn()
    return dict(vars(obj)) if hasattr(obj, "__dict__") else {"intent": str(obj)}


def _shrink(v):
    if isinstance(v, str):
        return v[:_SUMMARY_STR_LIMIT]
    if isinstance(v, (list, tuple)):
        return [_shrink(x) for x in v[:10]]
    if isinstance(v, dict):
        return {k: _shrink(x) for k, x in list(v.items())[:15]}
    return v


def get_query_intent_summary(user_query):
    """Analysis prompt ke liye chhota intent summary dict, ya None."""
    try:
        if not bool(getattr(_cfg, "INTENT_BRIDGE_ENABLED", False)) or not user_query:
            return None
        return _shrink(_to_dict(_interpret(user_query)))   # lru_cache: dobara Claude call nahi
    except Exception as e:
        log.warning("intent bridge: intent summary nahi ban saka (%s)", e)
        return None
