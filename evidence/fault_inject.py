"""Review probe (NOT part of the patch): inject a failure at every DB operation / the API call / usage recording, one at a time."""
import sys, os
sys.path.insert(0, "/tmp/pchk2")
os.environ.setdefault("SESSION_SECRET_KEY", "x" * 40)
import reddit_live as rl
from tests.test_reddit_live import Env, message, good_response, OWNER, NOW
import tests.test_reddit_live as T

class On:
    def __enter__(self):
        c = rl.config
        self.old = {k: getattr(c, k, None) for k in ("REDDIT_LIVE_LINKS_ENABLED","REDDIT_LIVE_DAILY_CAP_GLOBAL","REDDIT_LIVE_DAILY_CAP_PER_OWNER","REDDIT_LIVE_MAX_INFLIGHT","OPENAI_API_KEY")}
        c.REDDIT_LIVE_LINKS_ENABLED = True; c.REDDIT_LIVE_DAILY_CAP_GLOBAL = 100; c.REDDIT_LIVE_DAILY_CAP_PER_OWNER = 10
        c.REDDIT_LIVE_MAX_INFLIGHT = 2; c.OPENAI_API_KEY = "test-key-not-real"
    def __exit__(self, *a):
        for k, v in self.old.items(): setattr(rl.config, k, v)

class Faulty:
    """Wrap a collection; raise on the n-th call of the named methods."""
    def __init__(self, inner, fail_at, methods, counter):
        self.inner, self.fail_at, self.methods, self.counter = inner, fail_at, methods, counter
    def __getattr__(self, name):
        f = getattr(self.inner, name)
        if name not in self.methods: return f
        def wrapped(*a, **k):
            self.counter[0] += 1
            if self.counter[0] == self.fail_at: raise RuntimeError("injected db failure")
            return f(*a, **k)
        return wrapped

violations = []; runs = 0
def scenario(label, fail_at, target):
    global runs
    runs += 1
    env = Env()
    seen = {"post": 0, "checked": False}
    real_post = env._post
    def guarded_post(url, headers, payload, timeout):
        seen["post"] += 1
        # at the moment of the paid call: guard slot held, budget reserved, message marked 'calling'
        bud = env.budget.find_one({}) or {}
        st = (env.chats.find_one({"chat_id": "c1"})["messages"][0].get("live_reddit") or {}).get("state")
        if rl.INFLIGHT.count < 1 or bud.get("count", 0) < 1 or st != "calling":
            violations.append((label, fail_at, "call before guard/budget/calling-mark", rl.INFLIGHT.count, bud.get("count"), st))
        if target == "post": raise RuntimeError("injected api failure")
        return real_post(url, headers, payload, timeout)
    env.deps.post = guarded_post
    ctr = [0]
    if target == "chats": env.deps.chats = Faulty(env.chats, fail_at, {"find_one", "update_one"}, ctr)
    if target == "budget": env.deps.budget = Faulty(env.budget, fail_at, {"find_one_and_update"}, ctr)
    if target == "attempts": env.deps.attempts = Faulty(env.attempts, fail_at, {"insert_one"}, ctr)
    if target == "usage":
        def boom(*a, **k): raise RuntimeError("injected usage failure")
        env.deps.record_usage = boom
    for _ in range(3):                                  # the same request repeated, as a reconnect would
        res = rl.process(env.deps, owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r0")
        if rl.INFLIGHT.count != 0: violations.append((label, fail_at, "slot leaked", rl.INFLIGHT.count))
        if not isinstance(res, dict) or "state" not in res: violations.append((label, fail_at, "bad result", res))
    if seen["post"] > 1: violations.append((label, fail_at, "more than one paid call", seen["post"]))
    cnt = (env.budget.find_one({}) or {}).get("count", 0)
    if seen["post"] > cnt: violations.append((label, fail_at, "call without a counted slot", seen["post"], cnt))

with On():
    scenario("no fault", 0, "none")
    for target, n in (("chats", 8), ("budget", 3), ("attempts", 3)):
        for k in range(1, n + 1): scenario(target, k, target)
    scenario("usage", 0, "usage"); scenario("api", 0, "post")
print(f"fault-injection scenarios: {runs}; violations: {len(violations)}")
for v in violations[:10]: print("  VIOLATION", v)
# static: the only call site of the HTTP function, and what precedes it
src = open("/tmp/pchk2/reddit_live.py").read()
i_post = src.index("deps.post("); i_res = src.index("_reserve_budget(deps, _owner_hash"); i_enter = src.index("INFLIGHT.try_enter"); i_claim = src.index("res = deps.chats.update_one(")
print("order in process(): try_enter < claim < reserve < post :", i_enter < i_claim < i_res < i_post, "| call sites of deps.post:", src.count("deps.post("))
print("leave() only in finally:", src.rstrip().endswith("INFLIGHT.leave()") and src.count("INFLIGHT.leave()") == 1)
