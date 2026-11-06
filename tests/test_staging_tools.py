"""The staging tools themselves (preflight + verifier + seed), exercised offline. These tests prove the TOOLS behave; they are not staging results."""
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pf, sv = load("staging_preflight"), load("staging_verify")
PROD_SECRET = "the-production-key"
STRONG_SESSION = "q7Zk3vN9xLw2Rj8TfH5mYc1BpD6sUe4GoA0iKnV7tXb2WdJ"      # a fixed test value with high entropy (not a real secret)
@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch):
    """Hermetic: the preflight's `.env` search starts (and stops) in an empty temp directory, whatever files exist on the machine running the tests."""
    import tools.staging_preflight as _pf
    for mod in (pf, _pf):
        monkeypatch.setattr(mod, "DOTENV_START", str(tmp_path))
        monkeypatch.setattr(mod, "DOTENV_STOP", str(tmp_path))


GOOD = {"FLINTEL_ENV": "staging", "SESSION_SECRET_KEY": STRONG_SESSION, "MONGODB_URI": "mongodb+srv://u:p@stg-cluster.example.net/?x=1",
        "MONGODB3": "mongodb+srv://u:p@stg-cluster.example.net/?x=1", "MONGODB_DB": "flintel_staging", "SITE_URL": "https://app.stg.example.net",
        "TRENDING_KEYWORDS_ENABLED": "false", "LAZY_EMBED_ENABLED": "0", "SESSION_COOKIE_SECURE": "true", "TRUSTED_HOSTS": "app.stg.example.net", "ORIGIN_CHECK_ENABLED": "true",
        "STAGING_FORBIDDEN_HOSTS": "prod-cluster.example.net,flintel_prod,www.flintel.example",
        "STAGING_FORBIDDEN_KEY_SHA256_PREFIXES": hashlib.sha256(PROD_SECRET.encode()).hexdigest()[:12]}
PAID = dict(GOOD, OPENAI_API_KEY="k-staging", STAGING_OPENAI_LIMIT_CONFIRMED="true")


def by(env, status, phase="isolation"):
    return {x["name"] for x in pf.run(env, phase) if x["status"] == status}


def failed(env, phase="isolation"):
    return by(env, "FAIL", phase)


def verdict(env, phase="isolation"):
    return pf.verdict(pf.run(env, phase), phase)


# ── preflight: verdicts ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_complete_isolated_environment_passes_the_isolation_phase_without_any_paid_credential():
    assert verdict(GOOD) == "PASSED" and failed(GOOD) == set()
    assert "paid credentials (OPENAI_API_KEY, spend limit)" in by(GOOD, "SKIPPED")      # reported, and said to be out of scope, not silently passed


def test_the_paid_phase_needs_the_key_and_the_spend_limit_attestation():
    assert verdict(GOOD, "paid") == "FAILED"
    assert verdict(dict(GOOD, OPENAI_API_KEY="k"), "paid") == "FAILED"
    assert verdict(PAID, "paid") == "PASSED"


def test_a_skipped_isolation_check_is_INCOMPLETE_never_passed():
    no_fp = {k: v for k, v in GOOD.items() if k != "STAGING_FORBIDDEN_KEY_SHA256_PREFIXES"}
    assert verdict(no_fp) == "INCOMPLETE" and "production secret fingerprints supplied" in by(no_fp, "SKIPPED")
    assert verdict(dict(no_fp, MONGODB_URI="mongodb://u:p@prod-cluster.example.net/")) == "FAILED"      # a failure outranks incompleteness


def test_missing_configuration_fails_closed():
    assert verdict({}) == "FAILED"
    for k in ("FLINTEL_ENV", "SESSION_SECRET_KEY", "MONGODB_URI", "MONGODB3", "MONGODB_DB", "SITE_URL", "STAGING_FORBIDDEN_HOSTS", "TRENDING_KEYWORDS_ENABLED", "LAZY_EMBED_ENABLED", "SESSION_COOKIE_SECURE", "TRUSTED_HOSTS", "ORIGIN_CHECK_ENABLED"):
        assert verdict({x: y for x, y in GOOD.items() if x != k}) == "FAILED", k


@pytest.mark.parametrize("var,value", [("MONGODB_URI", "mongodb+srv://u:p@prod-cluster.example.net/"), ("MONGODB3", "mongodb://u:p@PROD-CLUSTER.example.net/"),
                                       ("MONGODB_DB", "flintel_prod"), ("MYSQL_HOST", "prod-cluster.example.net"),
                                       ("SITE_URL", "https://www.flintel.example"), ("SOME_OTHER_DB_URL", "postgres://u:p@prod-cluster.example.net/x"),
                                       ("MONGODB_ANALYTICS_URI", "mongodb://u:p@prod-cluster.example.net/")])
def test_every_production_target_is_rejected_whatever_the_variable_is_called(var, value):
    assert verdict(dict(GOOD, **{var: value})) == "FAILED"


@pytest.mark.parametrize("var", ["MONGODB2", "MONGODB4", "RAPIDAPI_KEY", "RAPIDAPI_GOOGLE_HOST", "SMTP_HOST", "EMAIL_BACKEND"])
def test_forbidden_integrations_fail_even_with_harmless_values(var):
    assert f"{var} is unset" in failed(dict(GOOD, **{var: "x"}))


@pytest.mark.parametrize("flag", ["INTENT_BRIDGE_ENABLED", "ACTIVE_INTENT_ENABLED", "REDDIT_LIVE_LINKS_ENABLED", "STRICT_INTENT_MODE", "TURN_FINISH_ENABLED"])
def test_risky_flags_fail_when_on(flag):
    assert f"{flag} is off" in failed(dict(GOOD, **{flag: "true"})) and f"{flag} is off" not in failed(dict(GOOD, **{flag: "false"}))


def test_the_credit_kill_switch_scheduler_and_lazy_embedding_are_enforced():
    assert "credit enforcement is ON (kill switch absent)" in failed(dict(GOOD, CREDIT_ENFORCEMENT_DISABLED="true"))
    assert "trending-keywords scheduler explicitly disabled" in failed(dict(GOOD, TRENDING_KEYWORDS_ENABLED="true"))
    assert "trending-keywords scheduler explicitly disabled" in failed({k: v for k, v in GOOD.items() if k != "TRENDING_KEYWORDS_ENABLED"})   # not left to a default
    assert "lazy query-time embedding explicitly off" in failed(dict(GOOD, LAZY_EMBED_ENABLED="1"))


def test_session_secret_must_be_new_strong_and_not_a_production_secret():
    assert "SESSION_SECRET_KEY is strong" in failed(dict(GOOD, SESSION_SECRET_KEY="dev-only-change-me"))
    assert "SESSION_SECRET_KEY is strong" in failed(dict(GOOD, SESSION_SECRET_KEY="short"))
    assert "SESSION_SECRET_KEY is strong" in failed(dict(GOOD, SESSION_SECRET_KEY="s" * 40))              # the audited gap: 40 repeated characters
    assert "SESSION_SECRET_KEY is not a production secret" in failed(dict(GOOD, SESSION_SECRET_KEY=PROD_SECRET))


def test_a_reused_production_secret_is_detected_by_fingerprint_for_every_secret_variable():
    for n in ("OPENAI_API_KEY", "GOOGLE_CLIENT_SECRET", "MYSQL_PASSWORD", "LEMONSQUEEZY_API_KEY", "MONGODB_URI"):
        assert f"{n} is not a production secret" in failed(dict(GOOD, **{n: PROD_SECRET})), n


def test_billing_and_mysql_rules():
    assert "billing is in test mode" in failed(dict(GOOD, LEMONSQUEEZY_API_KEY="x", LEMONSQUEEZY_TEST_MODE="false"))
    assert "billing is in test mode" not in failed(dict(GOOD, LEMONSQUEEZY_API_KEY="x", LEMONSQUEEZY_TEST_MODE="true"))
    assert "MySQL probe only after verification" in failed(dict(GOOD, ZERO_RESULT_MYSQL_PROBE_ENABLED="true"))
    assert "MySQL probe only after verification" not in failed(dict(GOOD, ZERO_RESULT_MYSQL_PROBE_ENABLED="true", STAGING_MYSQL_PROBE_VERIFIED="true"))
    assert "MySQL host is explicit" in failed(dict(GOOD, MYSQL_READ_ENABLED="true"))
    assert "MySQL settings consistent" in failed(dict(GOOD, MYSQL_HOST="stg-mysql.example.net"))


def test_site_url_must_be_https_and_not_production():
    for bad in ("", "http://app.stg.example.net", "https://www.flintel.example"):
        assert "SITE_URL is a staging https URL" in failed(dict(GOOD, SITE_URL=bad))


@pytest.mark.parametrize("url,ok", [("https://app.stg.example.net", True), ("https://prod-cluster.example.net/x", False), ("http://stg.example.net", False),
                                    ("ftp://stg.example.net", False), ("", False), ("http://localhost:8080", True), ("https://", False)])
def test_base_url_guard(url, ok):
    assert pf.check_base_url(url, GOOD)[0] is ok
    assert pf.check_base_url("https://app.stg.example.net", {})[0] is False


def test_the_exit_codes_and_output_never_contain_a_secret(capsys, monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith(("MONGO", "STAGING_", "FLINTEL_", "OPENAI", "SESSION", "SITE", "TRENDING", "LAZY", "MYSQL", "RAPID", "SMTP", "EMAIL", "CREDIT", "TURN_", "STRICT", "INTENT", "ACTIVE_", "REDDIT", "LEMON", "ZERO_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in PAID.items():
        monkeypatch.setenv(k, v)
    assert pf.main(["--phase", "paid"]) == 0
    out = capsys.readouterr().out
    for secret in ("k-staging", "u:p@", "stg-cluster.example.net", STRONG_SESSION, pf.fp(STRONG_SESSION), pf.fp("k-staging")):
        assert secret not in out
    monkeypatch.delenv("STAGING_FORBIDDEN_KEY_SHA256_PREFIXES")
    assert pf.main([]) == 4
    monkeypatch.setenv("MONGODB2", "x")
    assert pf.main([]) == 1


# ── verifier ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_mongo_coverage_reports_counts_only():
    mongomock = pytest.importorskip("mongomock")
    db = mongomock.MongoClient()["t"]
    now = datetime.now(timezone.utc)
    db.flintel_signals.insert_many([
        {"platform": "reddit", "created_utc": now - timedelta(days=1), "text": "PRIVATE BODY"},
        {"platform": "reddit", "created_utc": now - timedelta(days=40), "text": "PRIVATE BODY"},
        {"platform": "hackernews", "created_utc": now - timedelta(hours=2), "text": "PRIVATE BODY"}])
    cov = sv.mongo_coverage(db, now)
    assert cov["reddit"]["total_capped_at_100000"] == 2 and cov["reddit"]["last_7d"] == 1 and cov["hackernews_comment"]["total_capped_at_100000"] == 0
    assert "PRIVATE BODY" not in json.dumps(cov, default=str)


def _rec(i):
    return {"post_url": f"https://news.ycombinator.com/item?id={i}", "created_utc": 1700000000.0}


def test_hn_live_check_passes_only_with_real_records():
    ok = lambda kw, **k: ([_rec(1)], {"state": "ok", "requests": 1, "failed_requests": 0})                 # noqa: E731
    empty = lambda kw, **k: ([], {"state": "empty", "requests": 1, "failed_requests": 0})                   # noqa: E731
    down = lambda kw, **k: ([], {"state": "failed", "requests": 1, "failed_requests": 1, "reason": "timeout"})   # noqa: E731
    assert sv.hn_live(ok)[0] == "PASS"
    assert sv.hn_live(empty)[0] == "UNKNOWN"        # reachable but nothing came back: retrieval is not demonstrated (never PASS)
    assert sv.hn_live(down)[0] == "FAIL"


def test_a_crashing_check_is_a_fail_with_only_the_error_type():
    r = sv.Report()
    r.run("x", lambda: (_ for _ in ()).throw(ConnectionError("mongodb+srv://u:SECRET@host")))
    assert r.checks[0]["status"] == "FAIL" and r.checks[0]["detail"] == {"error": "ConnectionError"} and "SECRET" not in json.dumps(r.checks)


def test_mysql_probe_review_flags_a_full_scan_and_a_slow_query():
    from tests.test_coverage_mysql import FakeConn, FakeCursor, naive
    import mysql_signals as ms

    class Cur(FakeCursor):
        def __init__(self, key):
            super().__init__([naive(1)])
            self.key = key

        def fetchall(self):
            if self._sql.startswith("EXPLAIN"):
                return [(1, "SIMPLE", "t", None, "range", "k", self.key, "8", None, 120, 100.0, "")]
            return super().fetchall()
    src = ms.MySQLSource("flintel")
    verdict, d = sv.mysql_probe_review(lambda: FakeConn(Cur("idx_created")), [src], None)
    assert verdict == "PASS" and d["sources"][0]["explain_key_used"] == ["idx_created"]
    verdict, d = sv.mysql_probe_review(lambda: FakeConn(Cur(None)), [src], None)
    assert verdict == "FAIL" and "do NOT enable" in d["verdict"]


# ── second-pass audit: fail-closed holes found and closed ───────────────────────────────────────────────────────────────────
def test_every_mongo_uri_is_checked_including_the_secondary_one():
    assert any("production" in n for n in failed(dict(GOOD, MONGODB2="mongodb+srv://u:p@prod-cluster.example.net/")))
    assert any("production" in n for n in failed(dict(GOOD, MONGODB_ANALYTICS_URI="mongodb://u:p@prod-cluster.example.net/")))


@pytest.mark.parametrize("url,ok", [("https://app.stg.example.net", True), ("https://prod-cluster.example.net/x", False), ("http://stg.example.net", False),
                                    ("ftp://stg.example.net", False), ("", False), ("http://localhost:8080", True), ("https://", False)])
def test_base_url_guard(url, ok):
    assert pf.check_base_url(url, GOOD)[0] is ok


def test_base_url_guard_needs_the_production_list():
    assert pf.check_base_url("https://app.stg.example.net", {})[0] is False


def _scrub(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith(("MONGO", "STAGING_", "FLINTEL_", "OPENAI", "SESSION", "SITE", "TRENDING", "LAZY", "MYSQL", "RAPID", "SMTP", "EMAIL", "CREDIT", "TURN_", "STRICT", "INTENT", "ACTIVE_", "REDDIT", "LEMON", "ZERO_")):
            monkeypatch.delenv(k, raising=False)


def test_verifier_gates_in_order_preflight_then_base_url_then_acknowledgement_and_never_imports_the_app_early(monkeypatch):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delitem(sys.modules, "database", raising=False)
    run = lambda *args: (monkeypatch.setattr(sys, "argv", ["staging_verify.py", *args]), sv.main())[1]       # noqa: E731
    _scrub(monkeypatch)
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    assert run("--base-url", "https://prod-cluster.example.net") == 2          # production URL refused
    assert run() == 3 and "database" not in sys.modules                           # no acknowledgement: never imported
    assert run("--allow-paid-calls", "--allow-index-creation") == 2 and "database" not in sys.modules      # the paid flag needs the paid-phase preflight (no key here)
    monkeypatch.delenv("STAGING_FORBIDDEN_KEY_SHA256_PREFIXES")
    assert run("--allow-index-creation") == 5 and "database" not in sys.modules # INCOMPLETE isolation also blocks
    monkeypatch.setenv("FLINTEL_ENV", "production")
    assert run("--allow-index-creation") == 2 and "database" not in sys.modules # FAILED beats the flag


def test_offline_mode_touches_nothing_and_is_never_complete(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "database", raising=False)
    monkeypatch.setattr(sys, "argv", ["staging_verify.py", "--offline", "--report", str(tmp_path / "o.json")])
    assert sv.main() == 5 and "database" not in sys.modules
    rep = json.loads((tmp_path / "o.json").read_text())
    assert rep["complete"] is False and rep["summary"]["FAIL"] == 0 and rep["summary"]["SKIPPED"] >= 5
    assert all(c["status"] in ("PASS", "SKIPPED") for c in rep["checks"])


def test_the_tools_contain_no_database_write_call():
    import re
    for f in ("staging_preflight.py", "staging_verify.py"):
        src = (ROOT / "tools" / f).read_text()
        assert not re.search(r"\.(insert_one|insert_many|update_one|update_many|replace_one|delete_one|delete_many|bulk_write|create_index|drop\w*|find_one_and_\w+)\(", src), f
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\s", src.replace("CREATE INDEX", "")), f


def test_a_failed_or_empty_check_can_never_pass():
    zero = lambda **k: dict({"results": 0, "duplicates": 0, "live": "ok", "historical": "ok"}, **k)           # noqa: E731
    # empty database
    assert sv.judge_mongo({"reddit": {"total_capped_at_100000": 0}, "hackernews": {"total_capped_at_100000": 0}})[0] == "FAIL"
    assert sv.judge_mongo({"reddit": {"total_capped_at_100000": 3}, "hackernews": {"total_capped_at_100000": 0}})[0] == "PASS"
    good = {"Reddit|live|topic": zero(live="unavailable", historical="not_requested"), "Reddit|combined|topic": zero(results=2, live="unavailable"),
            "Hacker News|historical|topic": zero(results=1, live="not_requested")}
    assert sv.judge_modes(good)[0] == "PASS"
    assert sv.judge_modes({k: zero() for k in good})[0] == "FAIL"                                                      # nothing returned anywhere
    assert sv.judge_modes(dict(good, **{"all|combined|topic": zero(results=3, duplicates=1)}))[0] == "FAIL"           # a duplicate
    assert sv.judge_modes(dict(good, **{"all|combined|general": zero(results=3, live="failed")}))[0] == "FAIL"        # a failed path
    assert sv.judge_modes(dict(good, **{"Reddit|live|general": zero(results=2, live="ok")}))[0] == "FAIL"            # Reddit cannot be live
    assert sv.judge_modes(dict(good, **{"all|historical|topic": zero(results=1, live="ok")}))[0] == "FAIL"           # historical touched live
    assert sv.judge_zero({}, "NONE_IN_RANGE")[0] == "PASS" and sv.judge_zero({}, "NO_POSTS_COLLECTED")[0] == "PASS"
    assert sv.judge_zero({}, "UNKNOWN")[0] == "UNKNOWN" and sv.judge_zero({"note": "MySQL sources were not probed"}, "UNKNOWN")[0] == "PASS"
    assert sv.judge_zero({"error": "x"}, "DATABASE_UNAVAILABLE")[0] == "FAIL"


def test_hn_live_rejects_malformed_records():
    bad = lambda kw, **k: ([{"post_url": "https://evil.example/x", "created_utc": 1}], {"state": "ok", "requests": 1, "failed_requests": 0})   # noqa: E731
    assert sv.hn_live(bad)[0] == "FAIL"


def test_report_is_complete_only_when_nothing_failed_or_was_skipped():
    r = sv.Report()
    r.checks += [{"status": "PASS"}, {"status": "SKIPPED"}]
    assert sv.out_skipped_or_failed(r) is True
    r.checks = [{"status": "PASS"}]
    assert sv.out_skipped_or_failed(r) is False


def test_the_env_template_holds_only_placeholders_and_is_internally_consistent():
    import re
    text = (ROOT / ".env.staging.example").read_text()
    env = {}
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, val = line.split("=", 1)
        val = val.split("  #")[0].strip()
        env[k] = val
        assert not re.search(r"sk-[A-Za-z0-9]{10}|[0-9a-f]{32}", val), line                 # no key-shaped or hash-shaped value
    assert env["FLINTEL_ENV"] == "staging" and env["TRENDING_KEYWORDS_ENABLED"] == "false" and env["LAZY_EMBED_ENABLED"] == "0"
    assert env["LEMONSQUEEZY_TEST_MODE"] == "true" and env["ZERO_RESULT_MYSQL_PROBE_ENABLED"] == "false" and env["STAGING_OPENAI_LIMIT_CONFIRMED"] == "false"
    assert "ANTHROPIC_API_KEY" not in env and "MONGODB2" not in env and "MONGODB4" not in env and "RAPIDAPI_KEY" not in env
    rows = pf.run(env)
    assert not any(x["name"].startswith(("trending", "lazy", "credit", "billing")) and x["status"] == "FAIL" for x in rows)   # the cost controls in the template are right


def test_only_openai_counts_as_an_llm_key_in_the_paid_phase():
    only_deprecated = dict(GOOD, ANTHROPIC_API_KEY="x", STAGING_OPENAI_LIMIT_CONFIRMED="true")
    assert any(n.startswith("OPENAI_API_KEY is configured") for n in failed(only_deprecated, "paid"))


# ── regression: found by the independent release audit (a production host hidden behind the first host of a connection string) ──────────────
@pytest.mark.parametrize("uri", [
    "mongodb://stg-cluster.example.net:27017,prod-cluster.example.net:27017/db",           # replica-set list, production listed SECOND
    "mongodb://stg-a.example.net,stg-b.example.net,PROD-CLUSTER.example.net:27017/db?replicaSet=rs0",
    "mongodb://stg-cluster.example.net/db?replicaSet=x&hosts=prod-cluster.example.net",    # production named in a query parameter
    "mongodb://[::1]:27017,prod-cluster.example.net/db",
    "mongodb://u:p@stg-cluster.example.net:27017,prod-cluster.example.net:27017/",
])
@pytest.mark.parametrize("var", ["MONGODB_URI", "MONGODB3", "MONGODB2", "SOME_URL"])
def test_a_production_host_anywhere_in_a_connection_string_is_rejected(uri, var):
    assert verdict(dict(GOOD, **{var: uri})) == "FAILED"


def test_credentials_never_cause_a_false_match_and_staging_lists_still_pass():
    ok = "mongodb://prod-cluster.example.net:p%40ss@stg-a.example.net:27017,stg-b.example.net:27017/db?replicaSet=rs0"   # 'prod...' only inside the password
    assert verdict(dict(GOOD, MONGODB_URI=ok)) == "PASSED"
    assert pf.matched_production("mongodb://u:p@stg-a,prod-cluster.example.net/", ["prod-cluster.example.net"]) == ["prod-cluster.example.net"]


def test_base_url_userinfo_and_multi_host_tricks_are_rejected():
    for u in ("https://stg.example.net@prod-cluster.example.net/", "https://prod-cluster.example.net:443/", "https://stg.example.net/?next=prod-cluster.example.net"):
        assert pf.check_base_url(u, GOOD)[0] is False


def test_paid_calls_are_off_by_default_even_when_every_paid_credential_is_present(monkeypatch, tmp_path):
    """The default run must never start the matrix (the only paid step), however complete the environment is: stub modules record any call."""
    import types
    import mongomock
    _scrub(monkeypatch)                          # the test process itself sets CREDIT_ENFORCEMENT_DISABLED (conftest)
    for k, v in PAID.items():
        monkeypatch.setenv(k, v)
    seen = {"matrix": 0}
    db = types.ModuleType("database")
    mdb = mongomock.MongoClient()["t"]
    mdb.flintel_signals.insert_one({"platform": "reddit", "created_utc": datetime.now(timezone.utc)})
    db.client = types.SimpleNamespace(admin=types.SimpleNamespace(command=lambda c: {"ok": 1}))
    db.signals_collection = mdb.flintel_signals
    hn = types.ModuleType("hackernews_live")
    hn.fetch = lambda kw, **k: ([{"post_url": "https://news.ycombinator.com/item?id=1", "created_utc": 1.0}], {"state": "ok", "requests": 1, "failed_requests": 0})
    lg = types.ModuleType("logics")
    lg.search_mode_scope = lambda m: (_ for _ in ()).throw(AssertionError("matrix ran"))
    lg.get_matched_signals = lambda *a, **k: seen.__setitem__("matrix", seen["matrix"] + 1)
    lg.probe_stored_coverage = lambda *a, **k: {"total": 1, "in_range": 0, "newest": None}
    for name, mod in (("database", db), ("hackernews_live", hn), ("logics", lg)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(sys, "argv", ["staging_verify.py", "--allow-index-creation", "--report", str(tmp_path / "r.json")])
    sv.main()
    rep = json.loads((tmp_path / "r.json").read_text())
    modes = next(c for c in rep["checks"] if c["check"] == sv.CHK_MODES)
    assert modes["status"] == "SKIPPED" and "--allow-paid-calls" in modes["detail"]["reason"] and seen["matrix"] == 0
    assert rep["complete"] is False                                           # a skipped check can never make the report complete


# ── session-secret entropy (release hardening) ──────────────────────────────────────────────────────────────────────────────
import secrets as _secrets


@pytest.mark.parametrize("weak", [
    "s" * 40, "a" * 64, "ab" * 20, "abc" * 14, "abcdefghij" * 4, "1234567890" * 4, "0123456789abcdef" * 3,             # repeated / patterned
    "abcdefghijklmnopqrstuvwxyz012345", "zyxwvutsrqponmlkjihgfedcba987654", "qwertyuiopasdfghjklzxcvbnm1234567890",   # sequential / keyboard
    "dev-only-change-me", "", "   ", "short", "x7Kp2" * 2,                                                             # defaults / short
    "correct-horse-battery-staple-flintel-1", "this-is-my-password-for-the-staging-app-2026", "changeme-changeme-changeme-12345",   # predictable words
    "AaBbCcDdAaBbCcDdAaBbCcDdAaBbCcDd", "q7Zk3vN9" * 5,                                                                 # a short block repeated
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab", "9" * 31 + "k",                                                                 # one odd character in a run
])
def test_weak_session_secrets_are_rejected(weak):
    assert pf.secret_weaknesses(weak)
    assert "SESSION_SECRET_KEY is strong" in failed(dict(GOOD, SESSION_SECRET_KEY=weak))
    assert verdict(dict(GOOD, SESSION_SECRET_KEY=weak)) == "FAILED"


def test_strong_random_secrets_pass_and_false_rejections_are_rare():
    assert pf.secret_weaknesses(STRONG_SESSION) == []
    for gen in (lambda: _secrets.token_hex(32), lambda: _secrets.token_hex(16), lambda: _secrets.token_urlsafe(48)):
        rejected = sum(1 for _ in range(3000) if pf.secret_weaknesses(gen()))
        assert rejected <= 6, rejected                      # measured about 0.015% for urlsafe; a rejected value is simply regenerated
    assert verdict(dict(GOOD, SESSION_SECRET_KEY=_secrets.token_hex(32))) in ("PASSED", "FAILED")     # never an error


def test_weakness_reasons_and_check_output_never_reveal_the_secret_or_its_fingerprint(capsys, monkeypatch):
    secret = "Zq" * 20                                                      # weak AND distinctive, so a leak would be visible
    reasons = " ".join(pf.secret_weaknesses(secret))
    assert secret not in reasons and pf.fp(secret) not in reasons and "Zq" not in reasons
    rows = pf.run(dict(GOOD, SESSION_SECRET_KEY=secret))
    blob = json.dumps(rows)
    assert secret not in blob and pf.fp(secret) not in blob
    import os
    for k in list(os.environ):
        if k.startswith(("MONGO", "STAGING_", "FLINTEL_", "OPENAI", "SESSION", "SITE", "TRENDING", "LAZY", "CREDIT")):
            monkeypatch.delenv(k, raising=False)
    for k, v in dict(GOOD, SESSION_SECRET_KEY=secret).items():
        monkeypatch.setenv(k, v)
    pf.main([])
    out = capsys.readouterr().out
    assert secret not in out and pf.fp(secret) not in out and "SESSION_SECRET_KEY is strong" in out


def test_a_reused_production_secret_fails_even_if_it_is_strong():
    prod = _secrets.token_hex(32)
    env = dict(GOOD, SESSION_SECRET_KEY=prod, STAGING_FORBIDDEN_KEY_SHA256_PREFIXES=pf.fp(prod))
    assert "SESSION_SECRET_KEY is not a production secret" in failed(env) and "SESSION_SECRET_KEY is strong" not in failed(env)


# ── verifier completion semantics (release hardening) ───────────────────────────────────────────────────────────────────────
def _rows(status="PASS", skip=()):
    return [{"check": n, "status": ("SKIPPED" if n in skip else status), "elapsed_ms": 0, "detail": {}} for n in sv.LIVE_REQUIRED]


def test_a_live_run_with_every_required_check_passing_is_complete_but_never_staging_or_production_verified():
    r = sv.finalize_report(_rows(), "live")
    assert r["complete"] is True and r["claims"] == {"integration_verified": True, "staging_verified": False, "production_approved": False}
    assert r["manual_steps_pending"] and any("credit" in m for m in r["manual_steps_pending"]) and "not 'staging verified'" in r["complete_meaning"]
    assert r["evidence_level"].startswith("live") and "not production evidence" in r["evidence_level"]


def test_offline_can_never_be_complete_or_claim_integration_even_if_every_row_passes():
    r = sv.finalize_report(_rows(), "offline")
    assert r["complete"] is False and r["claims"] == {"integration_verified": False, "staging_verified": False, "production_approved": False}
    assert r["evidence_level"].startswith("offline")


@pytest.mark.parametrize("status", ["SKIPPED", "UNKNOWN", "FAIL"])
def test_one_non_passing_required_check_makes_the_report_incomplete_with_no_integration_claim(status):
    for i in range(len(sv.LIVE_REQUIRED)):
        rows = _rows()
        rows[i]["status"] = status
        r = sv.finalize_report(rows, "live")
        assert r["complete"] is False and r["claims"]["integration_verified"] is False and sv.LIVE_REQUIRED[i] in r["required_not_passed"]


def test_a_missing_or_duplicated_required_check_cannot_be_complete():
    rows = _rows()
    assert sv.finalize_report(rows[1:], "live")["complete"] is False and sv.finalize_report(rows[1:], "live")["missing_required"] == [sv.LIVE_REQUIRED[0]]
    assert sv.finalize_report(rows + [dict(rows[0])], "live")["complete"] is False         # the same check reported twice is not a clean pass
    assert sv.finalize_report([], "live")["complete"] is False


def test_a_failing_optional_row_still_blocks_completion_but_a_skipped_optional_row_does_not():
    extra_fail = _rows() + [{"check": sv.CHK_MYSQL, "status": "FAIL", "elapsed_ms": 0, "detail": {}}]
    assert sv.finalize_report(extra_fail, "live")["complete"] is False
    extra_skip = _rows() + [{"check": sv.CHK_MYSQL, "status": "SKIPPED", "elapsed_ms": 0, "detail": {}}]
    assert sv.finalize_report(extra_skip, "live")["complete"] is True                      # MySQL is not enabled for this deployment
    assert sv.finalize_report(extra_skip, "live", mysql_required=True)["complete"] is False  # but it is required when MYSQL_READ_ENABLED is on


def test_the_offline_report_uses_the_same_check_names_and_makes_no_integration_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["staging_verify.py", "--offline", "--report", str(tmp_path / "o.json")])
    sv.main()
    rep = json.loads((tmp_path / "o.json").read_text())
    names = {c["check"] for c in rep["checks"]}
    assert set(sv.LIVE_REQUIRED) <= names and rep["complete"] is False and rep["claims"]["integration_verified"] is False
    assert rep["claims"]["staging_verified"] is False and rep["claims"]["production_approved"] is False and rep["evidence_level"].startswith("offline")
    assert all(c["status"] == "SKIPPED" for c in rep["checks"] if c["check"] in sv.LIVE_REQUIRED)


# ── a leftover .env would be loaded by the application but is invisible to an environment-only check ─────────────────────────────
def _put_env(path, text="MONGODB2=mongodb://u:SUPERSECRETPW@prod-cluster.example.net/\nTRENDING_KEYWORDS_ENABLED=true\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".env").write_text(text)


def test_a_dotenv_file_in_the_app_directory_fails_the_preflight(tmp_path):
    assert verdict(GOOD) == "PASSED"
    _put_env(tmp_path)
    assert verdict(GOOD) == "FAILED" and "no .env file can be loaded by the application" in failed(GOOD)


def test_a_dotenv_in_a_parent_directory_also_fails_because_python_dotenv_walks_upward(tmp_path, monkeypatch):
    child = tmp_path / "app" / "deeper"
    child.mkdir(parents=True)
    _put_env(tmp_path)
    monkeypatch.setattr(pf, "DOTENV_START", str(child))
    assert "no .env file can be loaded by the application" in failed(GOOD)
    assert len(pf.dotenv_files(str(child), str(tmp_path))) == 1


def test_only_a_real_dotenv_file_counts(tmp_path):
    for name in (".env.example", ".env.staging.example", ".env.local", "env", "dotenv"):
        (tmp_path / name).write_text("X=1\n")
    (tmp_path / ".env").mkdir()                                  # a directory named .env is not loaded by python-dotenv
    assert verdict(GOOD) == "PASSED"


def test_the_dotenv_file_is_never_opened_and_its_content_never_printed(tmp_path, capsys, monkeypatch):
    import builtins
    _put_env(tmp_path)
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    rows = pf.run(GOOD)
    monkeypatch.undo()
    assert not [p for p in opened if p.endswith("/.env")]        # existence check only
    blob = json.dumps(rows)
    assert "SUPERSECRETPW" not in blob and "prod-cluster" not in blob


def test_seed_and_verifier_refuse_while_a_dotenv_exists_and_open_nothing(tmp_path, monkeypatch):
    import mongomock
    _put_env(tmp_path)
    opened = []
    seedmod = load("staging_seed")
    assert seedmod.main(["--apply", "--i-confirm-single-run", "--confirm-db", GOOD["MONGODB_DB"]], env=GOOD, client_factory=lambda uri: opened.append(uri) or mongomock.MongoClient()) == 2
    assert opened == []
    _scrub(monkeypatch)
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delitem(sys.modules, "database", raising=False)
    monkeypatch.setattr(sys, "argv", ["staging_verify.py", "--allow-index-creation"])
    assert sv.main() == 2 and "database" not in sys.modules


# ── session cookie hardening (master completion mission) ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("val", [None, "", "false", "0", "no"])
def test_a_staging_environment_without_a_secure_session_cookie_fails(val):
    env = {k: v for k, v in GOOD.items() if k != "SESSION_COOKIE_SECURE"}
    if val is not None:
        env["SESSION_COOKIE_SECURE"] = val
    assert "session cookie is Secure (SESSION_COOKIE_SECURE=true)" in failed(env) and verdict(env) == "FAILED"


def test_the_session_cookie_flag_is_wired_to_the_middleware_and_defaults_to_unchanged_behaviour():
    import re
    idx = (ROOT / "index.py").read_text()
    assert re.search(r"add_middleware\(SessionMiddleware, secret_key=SESSION_SECRET_KEY, https_only=bool\(getattr\(_cookie_cfg, \"SESSION_COOKIE_SECURE\", False\)\)\)", idx)
    cfg = (ROOT / "config.py").read_text()
    assert 'SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false")' in cfg           # default false: production unchanged unless configured


def test_starlette_really_marks_the_cookie_secure_when_the_flag_is_on():
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    def view(request):
        request.session["x"] = 1
        return PlainTextResponse("ok")
    for secure in (True, False):
        app = Starlette(routes=[Route("/", view)], middleware=[Middleware(SessionMiddleware, secret_key="k" * 40, https_only=secure)])
        header = TestClient(app).get("/").headers["set-cookie"].lower()
        assert ("secure" in header) is secure and "httponly" in header and "samesite=lax" in header
