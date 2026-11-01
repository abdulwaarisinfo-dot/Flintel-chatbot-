import shutil, subprocess, sys, os
SRC, DST = "/tmp/pchk2", "/tmp/pchk2_mut2"
MUTS = [
 ("routes: drop custom-header check", "routes.py", '    if request.headers.get("x-flintel-live-links") != "1":\n        return JSONResponse({"state": "forbidden"}, status_code=403)\n', ''),
 ("routes: drop Origin check", "routes.py", '    if origin and urlparse(origin).netloc != request.url.netloc:\n        return JSONResponse({"state": "forbidden"}, status_code=403)\n', ''),
 ("process: chat lookup ignores owner", "reddit_live.py", 'chat = deps.chats.find_one({"chat_id": chat_id, "owner_key": owner_key})\n        if not chat:\n            return {"state": "not_found"}', 'chat = deps.chats.find_one({"chat_id": chat_id})\n        owner_key = (chat or {}).get("owner_key", owner_key)\n        if not chat:\n            return {"state": "not_found"}'),
 ("process: guests allowed", "reddit_live.py", 'if owner_type != "email" or not owner_key:', 'if not owner_key:'),
 ("routes: trust a client-supplied owner (form field)", "routes.py", '    owner_key, owner_type = get_owner(request)\n    try:\n        result = reddit_live.process(', '    owner_key, owner_type = get_owner(request)\n    owner_key = topic_key or owner_key\n    try:\n        result = reddit_live.process('),
 ("routes: accept GET too", "routes.py", '@app.post("/chat/{chat_id}/live-links")', '@app.api_route("/chat/{chat_id}/live-links", methods=["GET", "POST"])'),
]
for name, f, old, new in MUTS:
    shutil.rmtree(DST, ignore_errors=True); shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    p = os.path.join(DST, f); s = open(p).read()
    if old not in s: print(f"{name:55s} MUTATION NOT APPLIED"); continue
    open(p, "w").write(s.replace(old, "" if new == "" else new, 1))
    env = dict(os.environ, SESSION_SECRET_KEY="test-only-session-key-not-for-deployment", CREDIT_ENFORCEMENT_DISABLED="true", GUEST_SEARCH_LIMIT="0", PYTHONDONTWRITEBYTECODE="1")
    o = subprocess.run([sys.executable, "-m", "pytest", "tests/test_reddit_live_endpoint.py", "-p", "no:cacheprovider", "-q", "-k", "asgi", "-W", "ignore::DeprecationWarning"],
                       cwd=DST, env=env, capture_output=True, text=True, timeout=300)
    failed = [l.split(" - ")[0].replace("FAILED tests/test_reddit_live_endpoint.py::", "") for l in o.stdout.splitlines() if l.startswith("FAILED")]
    print(f"{name:55s} {'KILLED by ' + str(len(failed)) + ' ASGI test(s): ' + ', '.join(failed[:3]) if failed else 'SURVIVED the ASGI tests'}")
shutil.rmtree(DST, ignore_errors=True)
