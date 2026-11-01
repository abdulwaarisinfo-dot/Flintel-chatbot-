import sys, os, json
sys.path.insert(0, "/tmp/pchk2"); os.environ.setdefault("SESSION_SECRET_KEY", "x"*40)
import reddit_live as rl
import mongomock
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/tmp/pchk2")
from tests.test_reddit_live import Env, message, answer
c = rl.config; c.REDDIT_LIVE_LINKS_ENABLED=True; c.REDDIT_LIVE_DAILY_CAP_GLOBAL=10; c.REDDIT_LIVE_DAILY_CAP_PER_OWNER=5; c.REDDIT_LIVE_MAX_INFLIGHT=2; c.OPENAI_API_KEY="test-key-not-real"
env = Env([message(0, results=[{"post_url": "https://www.reddit.com/r/SaaS/comments/zzz999/hist/", "title": "historical"}])])
before_msg = json.dumps(env.chats.find_one({"chat_id":"c1"})["messages"][0], default=str, sort_keys=True)
res = env.run()
after = env.chats.find_one({"chat_id":"c1"})["messages"][0]
changed = {k for k in set(after) | set(json.loads(before_msg)) if json.dumps(after.get(k), default=str, sort_keys=True) != json.dumps(json.loads(before_msg).get(k), default=str, sort_keys=True)}
print("state:", res["state"], "| message keys changed by the feature:", sorted(changed))
print("results/claude_answer untouched:", after["results"] == message(0, results=[{"post_url": "https://www.reddit.com/r/SaaS/comments/zzz999/hist/", "title": "historical"}])["results"] and after["claude_answer"] == message(0)["claude_answer"])
print("collections written:", sorted(n for n in env.chats.database.list_collection_names()))
heavy = ["logics","index","routes","flintel","google","billing","search_credits","credit_cost","guest_gate","usage_meter","database","turn_finisher","scheduler"]
print("heavy modules loaded by importing reddit_live + running process():", [m for m in heavy if m in sys.modules])
print("answer_integrity loaded (read-only is_evidence_row import):", "answer_integrity" in sys.modules)
