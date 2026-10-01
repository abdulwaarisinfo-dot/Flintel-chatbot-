"""
INTENT BRIDGE
============================================================================
Prototype (intent_prototype/) aur production (logics.py / routes.py) ke
beech ka jor.

    rerank_with_intent(user_query, candidates, evidence_required, *, config_overrides=None) -> list
    get_query_intent_summary(user_query) -> dict | None

FAIL-SAFE CONTRACT: koi exception bahar nahi aati. Flag band ho, interpreter
fail ho, timeout ho, ya kuch bhi ghalat ho — candidates waisi hi wapas
(log.warning mein wajah ke saath). Return value hamesha candidates ke apne
dicts hain, unchanged (jab tak attach_intent override on na ho).

PROTOTYPE ADAPTERS: intent_prototype ki files mere saamne nahi thin, is liye
unke function names/signatures ke andaze _call_* adapters mein ek hi jagah
band hain. Naam alag hon to sirf wahi adapters (aur _NAMES tables) badlo.
Ranker/filter na mil sake to built-in fallback (confidence + similarity)
chalta hai aur warning log hoti hai.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from functools import lru_cache

import config as _cfg

log = logging.getLogger(__name__)

# ── Prototype function-name tables (adapter points) ──────────────────────
_NAMES = {
    "interpret": ("interpret_query", "interpret", "parse_query", "extract_intent", "analyze_query"),
    "classify":  ("classify_batch", "classify_documents", "classify_docs", "classify_posts", "classify"),
    "passes":    ("passes_intent_filter", "passes_filter", "apply_intent_filter", "matches_intent", "is_opportunity"),
    "rank":      ("rank_documents", "rank_docs", "rank_posts", "rank", "rerank"),
}
_PASS_KEYS = ("passes_filter", "passes", "matches_intent", "is_match", "match", "is_opportunity")
_DEFAULT_BATCH_SIZE = 10
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
        "batch_size":     None,    # None => prototype ka apna / default
        "attach_intent":  False,   # True => candidate ki copy par "_intent" key
    }
    if overrides:
        s.update({k: v for k, v in overrides.items() if k in s})
    return s


# ── Lazy prototype loading (import-safe) ─────────────────────────────────
def _load(module_name):
    import importlib
    return importlib.import_module(f"intent_prototype.{module_name}")


def _find(mod, kind):
    for n in _NAMES[kind]:
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    return None


# ── Adapters ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=128)
def _interpret(user_query):
    fn = _find(_load("query_interpreter"), "interpret")
    if fn is None:
        raise RuntimeError("query_interpreter mein interpret function nahi mila")
    intent = fn(user_query)
    if intent is None:
        raise RuntimeError("interpreter ne None diya")
    return intent


def _classify_batch(intent, docs):
    """docs ki classification: list (aligned) ya {post_url: cls} -> {post_url: cls}."""
    mod = _load("doc_classifier")
    fn = _find(mod, "classify")
    if fn is None:
        raise RuntimeError("doc_classifier mein classify function nahi mila")
    res = fn(intent, docs)
    out = {}
    if isinstance(res, dict):
        out = {u: c for u, c in res.items() if isinstance(c, dict)}
    elif isinstance(res, (list, tuple)):
        for doc, cls in zip(docs, res):
            if isinstance(cls, dict) and doc.get("post_url"):
                out[doc["post_url"]] = cls
    return out


def _batch_size(s):
    if s["batch_size"]:
        return max(1, int(s["batch_size"]))
    try:
        v = getattr(_load("doc_classifier"), "BATCH_SIZE", None)
        if v:
            return max(1, int(v))
    except Exception:
        pass
    return _DEFAULT_BATCH_SIZE


def _confidence(cls):
    try:
        return float(cls.get("confidence", 0.0))
    except Exception:
        return 0.0


def _passes(intent, cls):
    """Intent filter: (passes: bool, confidence: float)."""
    try:
        fn = _find(_load("opportunity"), "passes")
    except Exception:
        fn = None
    if fn is not None:
        r = fn(intent, cls)
        return (bool(r[0]), float(r[1])) if isinstance(r, tuple) else (bool(r), _confidence(cls))
    for k in _PASS_KEYS:
        if k in cls:
            return bool(cls[k]), _confidence(cls)
    _warn_once("no_pass_filter",
               "intent bridge: opportunity filter/pass key nahi mila — koi post 'pass' nahi maani jayegi")
    return False, _confidence(cls)


def _rank(intent, entries):
    """
    entries: [{"doc", "cls", "conf", "idx"}] (sirf passing). Order mein wapas.
    Prototype ranker chale to wahi; warna confidence desc, phir similarity order.
    """
    try:
        fn = _find(_load("ranker"), "rank")
        if fn is not None:
            ranked = fn(intent, [(e["doc"], e["cls"]) for e in entries])
            urls = []
            for r in ranked:
                d = r[0] if isinstance(r, (tuple, list)) else r
                urls.append(d.get("post_url") if isinstance(d, dict) else d)
            by_url = {e["doc"].get("post_url"): e for e in entries}
            ordered = [by_url[u] for u in urls if u in by_url]
            if ordered:
                return ordered
    except Exception as e:
        log.warning("intent bridge: prototype ranker fail (%s) — built-in ranking", e)
    return sorted(entries, key=lambda e: (-e["conf"], e["idx"]))


# ── Core ─────────────────────────────────────────────────────────────────
def _run(user_query, candidates, evidence_required, s, deadline):
    from intent_prototype import classification_cache as cache  # fail-safe module

    intent = _interpret(user_query)

    urls = [c.get("post_url") for c in candidates]
    cls_map = cache.get_many([u for u in urls if u]) if s["cache_enabled"] else {}

    def classify_missing(docs):
        todo = [d for d in docs if d.get("post_url") and d["post_url"] not in cls_map]
        if not todo:
            return
        bs = _batch_size(s)
        batches = [todo[i:i + bs] for i in range(0, len(todo), bs)]

        def work(batch):
            if time.monotonic() >= deadline:
                return {}
            res = _classify_batch(intent, batch)
            if res and s["cache_enabled"]:
                cache.save_many(list(res.items()))   # turant save, timeout par bhi kaam zaya na ho
            return res

        with ThreadPoolExecutor(max_workers=s["parallel"]) as ex:
            futs = [ex.submit(work, b) for b in batches]
            for f in as_completed(futs):
                try:
                    cls_map.update(f.result())
                except Exception as e:
                    log.warning("intent bridge: ek classify batch fail (%s) — skip", e)

    def passing_count(docs):
        n = 0
        for d in docs:
            c = cls_map.get(d.get("post_url"))
            if c:
                ok, conf = _passes(intent, c)
                if ok and conf >= s["min_confidence"]:
                    n += 1
        return n

    head = candidates[:s["head"]]
    classify_missing(head)
    if len(candidates) > len(head) and passing_count(head) < s["min_passing"]:
        classify_missing(candidates[len(head):])

    entries = []
    for i, d in enumerate(candidates):
        c = cls_map.get(d.get("post_url"))
        if not c:
            continue
        ok, conf = _passes(intent, c)
        if ok:
            entries.append({"doc": d, "cls": c, "conf": conf, "idx": i})

    ranked = _rank(intent, entries)

    n = max(0, int(evidence_required))
    result, seen = [], set()
    for e in ranked:
        if len(result) >= n:
            break
        d = e["doc"]
        if s["attach_intent"]:
            d = dict(d)
            d["_intent"] = {"confidence": e["conf"], "classification": e["cls"]}
        result.append(d)
        seen.add(id(e["doc"]))
    for d in candidates:                      # FILL TO N — similarity order
        if len(result) >= n:
            break
        if id(d) not in seen:
            result.append(d)
    return result


def rerank_with_intent(user_query, candidates, evidence_required, *, config_overrides=None):
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
                box["res"] = _run(user_query, candidates, evidence_required, s, deadline)
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
