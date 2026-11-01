"""Review probe (NOT part of the patch): FORCE the race the claim exists for. Both callers read the message (no live_reddit yet)
before either one writes. A clock that jumps 60 s per read keeps the per-owner interval from masking a double claim."""
import sys, os, threading
root = sys.argv[1]; sys.path.insert(0, root); os.environ.setdefault("SESSION_SECRET_KEY", "x" * 40)
import reddit_live as rl
from tests.test_reddit_live import Env, message, OWNER
c = rl.config; c.REDDIT_LIVE_LINKS_ENABLED = True; c.REDDIT_LIVE_DAILY_CAP_GLOBAL = 100; c.REDDIT_LIVE_DAILY_CAP_PER_OWNER = 100
c.REDDIT_LIVE_MAX_INFLIGHT = 8; c.OPENAI_API_KEY = "test-key-not-real"
worst = 0
for trial in range(30):
    n = 6
    env = Env()
    barrier = threading.Barrier(n)
    real = env.chats
    class Gate:
        """The first find_one of every thread is held at a barrier, so all n callers see 'no live_reddit' first."""
        local = threading.local()
        def find_one(self, *a, **k):
            doc = real.find_one(*a, **k)
            if not getattr(Gate.local, "passed", False):
                Gate.local.passed = True
                barrier.wait(10)
            return doc
        def update_one(self, *a, **k): return real.update_one(*a, **k)
    Gate.local = threading.local()
    env.deps.chats = Gate()
    tick = {"t": 0}; lock = threading.Lock()
    def clock():
        with lock:
            tick["t"] += 60
            return env.clock.t.__class__.fromtimestamp(env.clock.t.timestamp() + tick["t"], tz=env.clock.t.tzinfo)
    env.deps.now = clock
    out = []
    ts = [threading.Thread(target=lambda: out.append(rl.process(env.deps, owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r0"))) for _ in range(n)]
    [t.start() for t in ts]; [t.join(30) for t in ts]
    worst = max(worst, len(env.calls))
print(f"{root}: max paid calls for ONE message over 30 forced-race trials of 6 simultaneous callers = {worst}")
