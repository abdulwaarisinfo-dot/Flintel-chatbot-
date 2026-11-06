"""tools/staging_seed.py: synthetic, labelled, bounded, idempotent, insert-only, and gated. mongomock only: no real database, no network, no paid call."""
import importlib.util
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
import search_modes as sm
import signal_intel as si
from tests.test_staging_tools import GOOD, PAID, load, pf  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
seedmod = load("staging_seed")


@pytest.fixture(autouse=True)
def _no_dotenv_seed(tmp_path, monkeypatch):
    import tools.staging_preflight as _pf
    for mod in (pf, _pf):
        monkeypatch.setattr(mod, "DOTENV_START", str(tmp_path))
        monkeypatch.setattr(mod, "DOTENV_STOP", str(tmp_path))
DB = GOOD["MONGODB_DB"]


def factory_for(client):
    calls = []

    def f(uri):
        calls.append(uri)
        return client
    f.calls = calls
    return f


@pytest.fixture
def client():
    return mongomock.MongoClient()


def coll(client):
    return client[DB]["flintel_signals"]


# ── the data ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_records_are_deterministic_unique_and_unmistakably_synthetic():
    a, b = seedmod.build_records(), seedmod.build_records()
    assert [d["message_id"] for d in a] == [d["message_id"] for d in b]
    assert len({d["message_id"] for d in a}) == len(a) and len(a) > 40
    for d in a:
        assert d["synthetic"] is True and d["_seed"] == seedmod.SEED_TAG and d["message_id"].startswith("stg-seed-")
        assert d["username"].startswith("synthetic_user_") and d["post_url"].startswith("https://synthetic.flintel-staging.invalid/") or "?utm_source=synthetic" in d["post_url"]
        assert ".invalid/" in d["post_url"] and "reddit.com" not in d["post_url"] and "ycombinator" not in d["post_url"]
    assert {"reddit", "hackernews"} == {d["platform"] for d in a}


def test_every_required_scenario_is_present():
    cats = {d["_seed_category"] for d in seedmod.build_records()}
    assert cats >= {"direct_demand", "product_request", "problem", "competitor_comparison", "market_discussion", "question_no_intent", "ambiguous",
                    "duplicate_pair", "title_only", "empty_text", "undated"}


def test_the_duplicate_pair_is_one_post_to_the_deduplicator():
    pair = [d for d in seedmod.build_records() if d["_seed_category"] == "duplicate_pair"]
    assert len(pair) == 2 and pair[0]["message_id"] != pair[1]["message_id"]
    assert sm.post_identity(pair[0]) == sm.post_identity(pair[1])


def test_incomplete_evidence_records_are_really_incomplete():
    by = {d["_seed_category"]: d for d in seedmod.build_records() if d["_seed_category"] in ("title_only", "empty_text", "undated")}
    assert by["title_only"]["text"] == by["title_only"]["title"] and not by["empty_text"]["text"].strip() and "created_utc" not in by["undated"]


def test_the_intended_classes_hold_for_the_clear_categories_and_no_intentless_record_is_a_buyer():
    got = {}
    for d in seedmod.build_records():
        got.setdefault(d["_seed_category"], set()).add(si.classify_post(d, "invoicing tool")["class"])
    assert got["direct_demand"] == {si.DIRECT_DEMAND} and got["problem"] == {si.PROBLEM_SIGNAL} and got["competitor_comparison"] == {si.COMPETITOR_SIGNAL}
    for cat in ("market_discussion", "question_no_intent", "ambiguous"):
        assert si.DIRECT_DEMAND not in got[cat], cat                      # a question or a vague musing is never labelled a buyer


def test_max_records_bounds_the_set():
    assert len(seedmod.build_records(5)) == 5 and len(seedmod.build_records(0)) == 0 and len(seedmod.build_records(10_000)) == len(seedmod.build_records(500))


# ── dry run and the write gate ──────────────────────────────────────────────────────────────────────────────────────────
def test_dry_run_is_the_default_and_opens_no_connection(client, capsys):
    f = factory_for(client)
    assert seedmod.main([], env=GOOD, client_factory=f) == 0 and f.calls == [] and coll(client).count_documents({}) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert seedmod.main(["--confirm-db", DB], env={}, client_factory=f) == 0 and f.calls == []                   # even with nothing configured


@pytest.mark.parametrize("mutate,why", [
    (lambda e: {}, "empty environment"),
    (lambda e: dict(e, FLINTEL_ENV="production"), "not staging"),
    (lambda e: dict(e, MONGODB_URI="mongodb+srv://u:p@prod-cluster.example.net/"), "production host"),
    (lambda e: dict(e, MONGODB_DB="flintel_prod"), "production database name"),
    (lambda e: dict(e, MONGODB2="mongodb://u:p@stg-cluster.example.net/"), "forbidden mirror"),
    (lambda e: {k: v for k, v in e.items() if k != "STAGING_FORBIDDEN_KEY_SHA256_PREFIXES"}, "isolation incomplete"),
    (lambda e: dict(e, TRENDING_KEYWORDS_ENABLED="true"), "scheduler on"),
])
def test_apply_is_refused_unless_isolation_is_proven(client, mutate, why):
    f = factory_for(client)
    env = mutate(GOOD)
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", env.get("MONGODB_DB", DB)], env=env, client_factory=f) == 2, why
    assert f.calls == [] and coll(client).count_documents({}) == 0


def test_apply_needs_the_database_name_retyped_exactly(client):
    f = factory_for(client)
    for args in (["--apply"], ["--apply", "--i-confirm-single-run", "--confirm-db", "other"], ["--apply", "--i-confirm-single-run", "--confirm-db", DB + " "]):
        assert seedmod.main(args, env=GOOD, client_factory=f) == 2
    assert f.calls == [] and coll(client).count_documents({}) == 0


def test_bad_arguments_are_rejected_before_anything_else(client):
    f = factory_for(client)
    for args in (["--max-records", "0"], ["--max-records", "501"], ["--embed", "--synthetic-vectors"]):
        assert seedmod.main(args, env=GOOD, client_factory=f) == 3
    assert f.calls == []


# ── writing ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def run_apply(client, *extra, env=GOOD):
    return seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", env["MONGODB_DB"], *extra], env=env, client_factory=factory_for(client))


def test_first_seed_inserts_everything_and_labels_it(client, capsys):
    assert run_apply(client) == 0
    docs = list(coll(client).find({}))
    assert len(docs) == len(seedmod.build_records()) and all(d["synthetic"] and d["_seed"] == seedmod.SEED_TAG for d in docs)
    assert "inserted=%d" % len(docs) in capsys.readouterr().out
    assert client.list_database_names() == [DB]                                  # it wrote only to the confirmed database


def test_a_repeat_seed_is_idempotent_and_reports_duplicates(client, capsys):
    run_apply(client)
    n = coll(client).count_documents({})
    capsys.readouterr()
    assert run_apply(client) == 0
    assert coll(client).count_documents({}) == n and f"inserted=0 already_present={n}" in capsys.readouterr().out


def test_an_existing_record_is_never_overwritten(client):
    coll(client).insert_one({"message_id": "stg-seed-0001", "title": "PRE-EXISTING", "text": "keep me"})
    run_apply(client)
    kept = coll(client).find_one({"message_id": "stg-seed-0001"})
    assert kept["title"] == "PRE-EXISTING" and kept["text"] == "keep me" and "synthetic" not in kept
    assert coll(client).count_documents({"message_id": "stg-seed-0001"}) == 1


def test_unrelated_data_is_never_touched_or_deleted(client):
    other = client[DB]["flintel_signals"]
    other.insert_many([{"message_id": f"real-{i}", "platform": "reddit", "text": "a real-looking row"} for i in range(5)])
    client[DB]["users"].insert_one({"email": "someone@example.net"})
    run_apply(client)
    assert other.count_documents({"message_id": {"$regex": "^real-"}}) == 5 and client[DB]["users"].count_documents({}) == 1


def test_the_tool_has_no_delete_drop_or_index_code():
    import re
    src = (ROOT / "tools" / "staging_seed.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", '"""')))
    assert not re.search(r"\.(delete_one|delete_many|drop\w*|replace_one|bulk_write|create_index|insert_many|find_one_and_\w+|remove)\(", code)
    assert len(re.findall(r"\.update_one\(", code)) == 1 and "$setOnInsert" in code and "$set\"" not in code and "'$set'" not in code


def test_a_partial_failure_stops_reports_and_is_safe_to_rerun(client, capsys):
    real = coll(client)

    class Flaky:
        def __init__(self):
            self.n = 0

        def update_one(self, *a, **k):
            self.n += 1
            if self.n == 4:
                raise RuntimeError("write failed: mongodb+srv://u:SECRET@host")
            return real.update_one(*a, **k)
    flaky = Flaky()
    docs = seedmod.build_records()
    s = seedmod.seed(flaky, docs, True)
    assert s["inserted"] == 3 and s["failed"] == 1 and s["error"] == "RuntimeError" and real.count_documents({}) == 3
    assert "SECRET" not in str(s)
    assert seedmod.seed(real, docs, True)["inserted"] == len(docs) - 3 and real.count_documents({}) == len(docs)      # rerun completes, no duplicates


def test_a_database_error_is_reported_without_leaking_it(client, capsys):
    def boom(uri):
        raise ConnectionError("mongodb+srv://u:SECRET@host")
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB], env=GOOD, client_factory=boom) == 1
    assert "SECRET" not in capsys.readouterr().out


# ── embeddings ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_no_embeddings_by_default_and_synthetic_vectors_are_free_and_labelled(client):
    run_apply(client)
    assert coll(client).count_documents({"embedding": {"$exists": True}}) == 0
    c2 = mongomock.MongoClient()
    run_apply(c2, "--synthetic-vectors")
    d = c2[DB]["flintel_signals"].find_one({"_seed_category": "direct_demand"})
    assert len(d["embedding"]) == 1536 and d["embedding_synthetic"] is True
    assert seedmod._vector("a") == seedmod._vector("a") and seedmod._vector("a") != seedmod._vector("b")


def test_paid_embeddings_need_the_explicit_authorisation_and_the_paid_preflight(client, monkeypatch):
    called = []
    monkeypatch.setattr(seedmod, "openai_embedder", lambda *a, **k: (called.append(1), lambda texts: [[0.0] * 1536 for _ in texts])[1])
    f = factory_for(client)
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB, "--embed"], env=PAID, client_factory=f) == 2                         # no authorisation flag
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB, "--embed", "--i-authorize-paid-embeddings"], env=GOOD, client_factory=f) == 2   # no key / limit attestation
    assert called == [] and f.calls == [] and coll(client).count_documents({}) == 0
    assert seedmod.main(["--embed"], env=GOOD, client_factory=f) == 2 and called == []                                         # --embed alone is refused, even as a dry run
    assert seedmod.main(["--embed", "--i-authorize-paid-embeddings"], env=GOOD, client_factory=f) == 0 and called == []       # a dry run never embeds
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB, "--embed", "--i-authorize-paid-embeddings"], env=PAID, client_factory=f) == 0
    assert called == [1] and coll(client).count_documents({"embedding_provider": "openai"}) > 0
    assert coll(client).count_documents({"_seed_category": "empty_text", "embedding": {"$exists": True}}) == 0                  # nothing to embed


def test_the_openai_embedder_is_bounded_and_batches(monkeypatch):
    seen = []

    class R:
        def __init__(self, n): self.n = n
        def raise_for_status(self): pass
        def json(self): return {"data": [{"embedding": [0.1]} for _ in range(self.n)]}

    def post(url, headers, json, timeout):
        seen.append((url, len(json["input"]), timeout))
        return R(len(json["input"]))
    out = seedmod.openai_embedder("k", "m", http_post=post)(["x"] * 120)
    assert len(out) == 120 and [s[1] for s in seen] == [50, 50, 20] and all(s[0].startswith("https://api.openai.com/") and s[2] == 30 for s in seen)


# ── concurrency: one seed run at a time (release hardening) ──────────────────────────────────────────────────────────────────
import subprocess
import sys
import textwrap
import time


def test_apply_requires_the_single_run_attestation(client):
    f = factory_for(client)
    assert seedmod.main(["--apply", "--confirm-db", DB], env=GOOD, client_factory=f) == 2
    assert f.calls == [] and coll(client).count_documents({}) == 0


def test_a_second_seed_process_on_the_same_machine_is_refused_before_any_connection(client, tmp_path):
    """A REAL second process holds the lock; the run must exit 2 immediately without connecting (no waiting, no queueing, no writes)."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from tools import staging_seed as s
        with s.single_run_lock({DB!r}, {GOOD['MONGODB_URI']!r}) as held:
            print('HELD' if held else 'NOT-HELD', flush=True)
            time.sleep(8)
    """)
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        f = factory_for(client)
        t0 = time.monotonic()
        assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB], env=GOOD, client_factory=f) == 2
        assert time.monotonic() - t0 < 3 and f.calls == [] and coll(client).count_documents({}) == 0       # refused at once, nothing written
    finally:
        holder.kill()
        holder.wait()
    assert run_apply(client) == 0                                                                          # once the holder is gone the lock is free


def test_the_lock_is_released_after_every_run_including_a_failed_one(client):
    assert run_apply(client) == 0 and run_apply(client) == 0
    def boom(uri):
        raise ConnectionError("down")
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB], env=GOOD, client_factory=boom) == 1
    assert run_apply(client) == 0


def test_the_lock_key_depends_on_the_database_and_hosts_never_on_the_password():
    a = "mongodb+srv://u:pw-one@stg-cluster.example.net/"
    b = "mongodb+srv://u:pw-two@stg-cluster.example.net/"
    with seedmod.single_run_lock(DB, a) as h1:
        assert h1
        with seedmod.single_run_lock(DB, b) as h2:
            assert h2 is False                                        # same database + host, different password: still the SAME lock
        with seedmod.single_run_lock("another_db", a) as h3:
            assert h3 is True                                         # a different database is independent


def test_overlapping_runs_that_did_duplicate_a_record_are_detected_and_reported_never_deleted(client, capsys):
    run_apply(client)
    c = coll(client)
    dup = c.find_one({"message_id": "stg-seed-0002"})
    dup.pop("_id")
    c.insert_one(dup)                                                 # what two overlapping upserts without a unique index could leave behind
    n = c.count_documents({})
    capsys.readouterr()
    assert run_apply(client) == 1
    out = capsys.readouterr().out
    assert "DUPLICATES FOUND for 1 message_id" in out and "stg-seed-0002" in out and "Nothing was deleted" in out
    assert c.count_documents({}) == n                                  # reported, not repaired


def test_the_duplicate_check_is_read_only_and_a_clean_run_says_so(client, capsys, monkeypatch):
    run_apply(client)
    writes = []
    Coll = mongomock.collection.Collection
    for name in ("insert_one", "update_one", "delete_one", "delete_many", "create_index", "replace_one", "insert_many"):
        orig = getattr(Coll, name)
        monkeypatch.setattr(Coll, name, (lambda o, n: lambda self, *a, **k: (writes.append(n), o(self, *a, **k))[1])(orig, name))   # restored by monkeypatch
    assert seedmod.find_duplicates(coll(client)) == []
    assert writes == []                                                # the check itself performed no write of any kind
    monkeypatch.undo()
    capsys.readouterr()
    assert run_apply(client) == 0 and "duplicate check: none" in capsys.readouterr().out


def test_an_unrunnable_duplicate_check_is_unknown_not_success(client, capsys):
    class NoAgg:
        def __init__(self, real): self.real = real
        def update_one(self, *a, **k): return self.real.update_one(*a, **k)
        def aggregate(self, *a, **k): raise RuntimeError("aggregate unavailable")
    class Cl:
        def __getitem__(self, name): return types_ns(self)
    def types_ns(_):
        class D:
            def __getitem__(self, c): return NoAgg(coll(client))
        return D()
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", DB], env=GOOD, client_factory=lambda uri: Cl()) == 1
    assert "UNKNOWN" in capsys.readouterr().out


def test_concurrency_hardening_adds_no_index_marker_document_or_other_collection_write(client):
    run_apply(client)
    assert list(coll(client).index_information()) == ["_id_"]                                  # no index was created
    assert coll(client).count_documents({"synthetic": {"$ne": True}}) == 0                    # no marker/lock document in the signals collection
    assert client[DB].list_collection_names() == ["flintel_signals"]                           # no other collection exists
