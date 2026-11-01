"""Review probe (NOT part of the patch): apply one deliberate bug at a time to a THROWAWAY copy and see whether the tests notice."""
import shutil, subprocess, sys, os, re
SRC = "/tmp/pchk2"; DST = "/tmp/pchk2_mut"
MUTS = [
 ("routes: drop the custom-header check", "routes.py", '    if request.headers.get("x-flintel-live-links") != "1":\n        return JSONResponse({"state": "forbidden"}, status_code=403)\n', ''),
 ("routes: drop the Origin check", "routes.py", '    if origin and urlparse(origin).netloc != request.url.netloc:\n        return JSONResponse({"state": "forbidden"}, status_code=403)\n', ''),
 ("process: chat lookup ignores owner", "reddit_live.py", 'chat = deps.chats.find_one({"chat_id": chat_id, "owner_key": owner_key})\n        if not chat:\n            return {"state": "not_found"}', 'chat = deps.chats.find_one({"chat_id": chat_id})\n        owner_key = (chat or {}).get("owner_key", owner_key)\n        if not chat:\n            return {"state": "not_found"}'),
 ("process: guests allowed", "reddit_live.py", 'if owner_type != "email" or not owner_key:', 'if not owner_key:'),
 ("_http_post: follow redirects", "reddit_live.py", 'follow_redirects=False', 'follow_redirects=True'),
 ("validator: any *.reddit.com host", "reddit_live.py", 'if host not in _HOSTS or not _POST_PATH.fullmatch(parts.path):', 'if not host.endswith("reddit.com") or not _POST_PATH.fullmatch(parts.path):'),
 ("validator: any path", "reddit_live.py", 'if host not in _HOSTS or not _POST_PATH.fullmatch(parts.path):', 'if host not in _HOSTS:'),
 ("validator: non-ASCII allowed + any host suffix", "reddit_live.py", 'if not _PRINTABLE_ASCII.fullmatch(url) or "\\\\" in url:\n        return None', 'pass'),
 ("process: slot never released", "reddit_live.py", '        if entered:\n            INFLIGHT.leave()', '        pass'),
 ("process: skip in-flight guard", "reddit_live.py", 'if not INFLIGHT.try_enter(_inflight_limit()):', 'if False:'),
 ("eligible: ignore time_range", "reddit_live.py", 'if msg.get("time_range") or msg.get("time_window_days") is not None:', 'if msg.get("time_window_days") is not None:'),
 ("eligible: ignore website_only", "reddit_live.py", 'if msg.get("website_only") or msg.get("website_evidence") or msg.get("website_context"):', 'if msg.get("website_evidence") or msg.get("website_context"):'),
 ("eligible: ignore credit_request_id", "reddit_live.py", 'if not isinstance(msg.get("credit_request_id"), str) or not msg.get("credit_request_id"):', 'if False:'),
 ("eligible: threshold 3 -> 4", "reddit_live.py", 'THRESHOLD = 3 ', 'THRESHOLD = 4 '),
 ("render_links: no validation", "reddit_live.py", 'return _safe_links(live.get("links"))\n    except Exception:\n        pass\n    return []', 'return list(live.get("links"))\n    except Exception:\n        pass\n    return []'),
 ("present: no re-validation", "reddit_live.py", 'links = _safe_links(live.get("links"))\n        if links:', 'links = list(live.get("links") or [])\n        if links:'),
 ("budget: ceiling not enforced", "reddit_live.py", 'if _pos_int(g) and _pos_int(o) and g <= GLOBAL_CAP_CEILING and o <= OWNER_CAP_CEILING:', 'if _pos_int(g) and _pos_int(o):'),
 ("budget: owner cap ignored", "reddit_live.py", 'f"owners.{owner_hash}": {"$not": {"$gte": cap_o}},', ''),
 ("budget: global cap ignored", "reddit_live.py", '"count": {"$lt": cap_g},', ''),
 ("budget: interval ignored", "reddit_live.py", 'f"last.{owner_hash}": {"$not": {"$gt": now - timedelta(seconds=interval)}}}', '}'),
 ("process: retry/reclaim on stale (claim filter dropped)", "reddit_live.py", 'f"messages.{idx}.live_reddit": {"$exists": False},\n', ''),
 ("process: call before budget (swap reserve to always True)", "reddit_live.py", 'if not _reserve_budget(deps, _owner_hash(owner_key), now):', 'if False:'),
]
results = []
for name, f, old, new in MUTS:
    shutil.rmtree(DST, ignore_errors=True); shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    p = os.path.join(DST, f); s = open(p).read()
    if old not in s: results.append((name, "MUTATION NOT APPLIED")); continue
    open(p, "w").write(s.replace(old, new, 1))
    env = dict(os.environ, SESSION_SECRET_KEY="test-only-session-key-not-for-deployment", CREDIT_ENFORCEMENT_DISABLED="true", GUEST_SEARCH_LIMIT="0", PYTHONDONTWRITEBYTECODE="1")
    o = subprocess.run([sys.executable, "-m", "pytest", "tests/test_reddit_live.py", "tests/test_reddit_live_endpoint.py", "-p", "no:cacheprovider", "-q", "-x",
                        "-W", "ignore::DeprecationWarning", "-k", "not browser"], cwd=DST, env=env, capture_output=True, text=True, timeout=300)
    tail = [l for l in o.stdout.strip().splitlines() if l.strip()][-1] if o.stdout.strip() else o.stderr[-120:]
    first = next((l for l in o.stdout.splitlines() if l.startswith("FAILED") or l.startswith("ERROR")), "")
    results.append((name, ("KILLED  " + first[:110]) if o.returncode != 0 else "SURVIVED (all tests still pass)"))
for n, r in results: print(f"{n:62s} {r}")
print("survivors:", sum(1 for _, r in results if r.startswith("SURVIVED")), "of", len(results))
shutil.rmtree(DST, ignore_errors=True)
