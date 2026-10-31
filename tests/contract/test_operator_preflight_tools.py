"""The two operator preflight tools of a deployment window (2026-10-30): the connection envelope (REL-04) with the rollout overlap and an
unverified worker bound refused, and the budget preflight (AI-03) that fails on a stale price snapshot or a reservation above the cap. Pure tools,
no database, no provider."""
import pathlib

from tools import budget_preflight, db_envelope

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "config" / "ai_closed_loop" / "final_budgets.production.example.yaml"


def test_envelope_arithmetic():
    import argparse
    a = argparse.Namespace(old_web_instances=3, web_instances=3, web_pool=4, worker_executions=2, worker_pool=3, migration=1, operators=2, margin=2,
                           max_connections=25, reserved=3)
    b = db_envelope.budget(a)
    assert b["used"] == 12 + 12 + 6 + 1 + 2 + 2 == 35 and b["limit"] == 22 and not b["holds"], "the approved topology does not fit while both revisions are alive"
    a.old_web_instances = 0
    b = db_envelope.budget(a)
    assert b["used"] == 23 and not b["holds"], "even without the old revision, 2 worker executions and margins exceed 22: the GO-day inputs must be measured, not assumed"
    a.worker_executions = 1
    assert db_envelope.budget(a)["used"] == 20 and db_envelope.budget(a)["holds"]


def test_envelope_exit_codes():
    ok = ["--max-connections", "25", "--reserved", "3", "--web-instances", "3", "--web-pool", "4", "--worker-executions", "1", "--worker-pool", "3"]
    assert db_envelope.main(ok) == 3, "unverified worker bound is not accepted"
    assert db_envelope.main(ok + ["--worker-executions-verified", "test_execution_lease"]) == 0
    assert db_envelope.main(ok + ["--worker-executions-verified", "x", "--old-web-instances", "3"]) == 2
    assert db_envelope.main(ok + ["--worker-executions-verified", "x", "--observed-connections", "22"]) == 2


def test_budget_preflight_passes_on_a_fresh_day_and_fails_when_stale_or_over_cap(capsys):
    assert budget_preflight.main([str(EXAMPLE), "--today", "2026-11-09"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert budget_preflight.main([str(EXAMPLE), "--today", "2026-12-01"]) == 2, "window 2 opens 34 days after the snapshot: stale, the price must be re-read first"
    out = capsys.readouterr().out
    assert "STALE" in out and "FAIL" in out
    assert budget_preflight.main([str(EXAMPLE), "--today", "2026-11-09", "--per-job-max", "0.50"]) == 2
    assert budget_preflight.main(["no-such-file.yaml"]) == 3
