"""tools/lead_validation: the approval guard, the dry-run defaults, the source-verification report format (with injected fake connectors) and a self-test of the
multi-process MongoDB harness against an in-memory double (which proves the orchestration, NOT MongoDB behaviour)."""
import json
import sys

import pytest

from tools.lead_validation import _guard, mongo_concurrency, verify_sources


def test_real_io_is_refused_without_every_approval_condition(monkeypatch, capsys):
    for k in ("FLINTEL_STAGING_APPROVED", "FLINTEL_STAGING_ENV_NAME"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit) as e:
        _guard.require_approval(None, "x", dry_run=False)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--approved-by" in err and "FLINTEL_STAGING_APPROVED" in err and "FLINTEL_STAGING_ENV_NAME" in err
    monkeypatch.setenv("FLINTEL_STAGING_APPROVED", "yes")
    monkeypatch.setenv("FLINTEL_STAGING_ENV_NAME", "production-eu")
    with pytest.raises(SystemExit):
        _guard.require_approval("Owner", "x", dry_run=False)                                          # "prod" in the name is refused even when approved
    monkeypatch.setenv("FLINTEL_STAGING_ENV_NAME", "lead-staging-1")
    assert _guard.require_approval("Owner", "x", dry_run=False) is True


def test_a_dry_run_contacts_nothing_and_says_so(capsys):
    assert _guard.require_approval(None, "scan the sources", dry_run=True) is False
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "nothing was contacted" in out


def test_credentials_are_redacted_from_connection_strings():
    assert "secretpw" not in _guard.redact("mongodb+srv://user:secretpw@cluster0.example.net/db") and "***@" in _guard.redact("mongodb://u:p@h/x")


def test_the_scripts_default_to_a_dry_run(capsys, tmp_path):
    prof = tmp_path / "p.json"; prof.write_text("{}")
    assert verify_sources.main(["--profile", str(prof)]) == 0 and mongo_concurrency.main([]) == 0
    assert capsys.readouterr().out.count("nothing was contacted") == 2


def test_the_mongo_harness_refuses_to_run_for_real_without_approval(monkeypatch, capsys):
    for k in ("FLINTEL_STAGING_APPROVED", "FLINTEL_STAGING_ENV_NAME", "FLINTEL_STAGING_MONGO_URI"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        mongo_concurrency.main(["--execute"])


def test_verify_sources_writes_the_report_and_the_manual_check_sheet(tmp_path):
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"business_name": "P", "sells": "plumbing and boiler repair services", "ideal_customers": "homeowners", "problems_solved": "leaking pipes"}))

    class Fake:
        key, label, retrieval_path = "reddit", "Reddit", "historical"

        def fetch(self, concepts, days, limit):
            return {"status": "ok", "detail": "", "records": [{"title": "Need a plumber", "post_text": "I need a plumber for a leaking pipe under my sink.",
                                                                "post_url": "https://www.reddit.com/r/t/comments/a1/x/", "platform": "reddit", "author": "u1",
                                                                "published_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}]}

    def factory(selected, down, counters):
        counters["embedding_calls"] += 2
        return [Fake()]
    rep = verify_sources.run(prof, ["reddit"], 30, tmp_path / "out", connectors_factory=factory)
    assert rep["counts"]["displayed"] == 1 and rep["provider_calls_counted"]["embedding_calls"] == 2 and "NOT computed" in rep["note"]
    sheet = (tmp_path / "out" / "manual_check.csv").read_text(encoding="utf-8").splitlines()
    assert sheet[0].startswith("platform,url,author_shown") and "reddit.com/r/t/comments/a1" in sheet[1]
    assert (tmp_path / "out" / "scan_report.json").exists()


def test_the_multi_process_orchestration_runs_every_scenario_against_the_double(capsys):
    assert mongo_concurrency.main(["--backend", "fake-manager", "--processes", "3"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["mongo"] is False and rep["verified_multi_worker_mongo"] is False and "NOT evidence about MongoDB" in rep["note"]
    assert {s["scenario"] for s in rep["scenarios"]} == {"quota_exact", "lock_exclusive", "lock_expiry_after_crash", "failed_scan_consumes", "duplicate_requests",
                                                         "daily_boundary", "other_owner_isolated"}
    assert all(s["result"] == "PASS" for s in rep["scenarios"])
