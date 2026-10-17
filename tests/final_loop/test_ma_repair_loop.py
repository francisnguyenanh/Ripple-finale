"""AIX-C1 / MA-C1 bounded repair loop (LOCAL, synthetic repairer): the first candidate changes the function literals only; a protected test the evaluator owns also demands the
configuration constant, so the sandbox fails; the repairer may ONLY pick literal sites the server scanned for the current value; the accepted site becomes a new candidate (new
patch hash) and one more sandbox round; the loop stops at the cap or when nothing is proposed, with the whole failed history in the report. No test is ever modified, no new
number is ever written, attempts never reset. 0 real provider calls."""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from infra.worker import code_change as cc
from tests.final_loop.test_fex_code_change import _committed, _config, _git, _settle  # noqa: F401
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_ma_coder_tester import case
from tests.final_loop.test_qry03_qa import use
from tests.final_loop.test_syn01_code import code_env  # noqa: F401  (fixture)
from tests.pitch_brief.conftest import HARBOR_INPUT, drain

CONFIG_PY = "# approval configuration mirrored from the policy\nFREE_FREIGHT_THRESHOLD_JPY = 500_000\nTAX_RATE = 10\n"
EVALUATOR_TEST = '''from app import calculations, config

def test_free_freight_threshold_is_the_decided_value_everywhere():
    import inspect
    src = inspect.getsource(calculations.calculate_freight)
    assert "600_000" in src and "500_000" not in src
    assert config.FREE_FREIGHT_THRESHOLD_JPY == 600_000
'''


@pytest.fixture
def repair_repo(tmp_path):
    repo = tmp_path / "repair repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "calculations.py").write_text((HARBOR_INPUT / "app" / "calculations.py").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "app" / "config.py").write_text(CONFIG_PY, encoding="utf-8")
    (repo / "tests" / "test_freight.py").write_text(EVALUATOR_TEST, encoding="utf-8")
    (repo / "requirements.txt").write_text("# protected\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "add", "-A")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "fixture baseline with a configuration constant")
    return repo, tmp_path / "mock target"


@pytest.fixture
def rl_env(code_env, repair_repo, monkeypatch, tmp_path):     # noqa: F811
    import os
    q = code_env
    repo, target = repair_repo
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, repo, target)))
    monkeypatch.setenv("RIPPLE_EXTENSION_MOCK_TARGET", "1")
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    if "ma.specialist" not in cfg:
        cfg = cfg.replace("role_model_ids:\n", "role_model_ids:\n  ma.specialist: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_rl.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return q, repo


def _candidate(q):
    cid, approver, committed = _committed(q)
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 200, r.text
    got = _settle(q, q.admin, cid, "candidate")
    assert got["candidate"]["status"] == "PROPOSED"
    return cid, r.json()["job_id"]


def _sites(prompt: str) -> list:
    return [{"path": p, "line": int(n), "literal": lit} for p, n, lit in re.findall(r"path=(\S+) line=(\d+) literal=(\S+)", prompt)]


def _run_to_end(q, run_id, tries=60):
    for _ in range(tries):
        drain(q.env)
        got = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
        if got["status"] in ma.TERMINAL:
            return got
    return got


def _start(q, cid, cand_id):
    r = q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "coder_tester", "candidate_job_id": cand_id})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_pure_repair_validation_keeps_only_offered_sites():
    offered = [{"path": "app/config.py", "line": 2, "literal": "500_000"}]
    out = ma.validate_repair_object({"status": "OK", "sites": [offered[0], offered[0], {"path": "app/config.py", "line": 9, "literal": "500_000"}, {"path": "tests/test_freight.py", "line": 1, "literal": "500_000"}],
                                     "rationale": "The constant mirrors the threshold."}, offered)
    assert out["sites"] == offered and [r["reason"] for r in out["rejected"]] == ["duplicate_site", "site_not_offered", "site_not_offered"] and out["status"] == "OK"
    assert ma.validate_repair_object({"status": "OK", "sites": [{"path": "x", "line": 1, "literal": "1"}], "rationale": "x"}, offered)["status"] == "NONE"
    with pytest.raises(ValueError):
        ma.validate_repair_object({"status": "OK", "sites": [{"path": "a", "line": "1", "literal": "1"}], "rationale": "x"}, offered)
    with pytest.raises(ValueError):
        ma.validate_repair_object({"status": "OK", "sites": [], "rationale": "x", "extra": 1}, offered)
    assert ma.max_children(ma.manifest("coder_tester")) == 3                                                               # tester + 2 repairs: the root reservation covers the loop


def test_a_failing_candidate_is_repaired_once_through_a_server_scanned_site_and_the_report_keeps_the_history(rl_env):
    q, repo = rl_env
    cid, cand_id = _candidate(q)
    seen = {"repair_prompts": 0}

    def stub(prompt):
        if "You are the TESTER" in prompt:
            return {"status": "OK", "cases": [case("charged_just_below_new_threshold", "calculate_freight", [599999], "eq", 2000)], "notes": []}
        assert "You are the REPAIRER" in prompt and "<failure_feedback>" in prompt and "verdict protected_tests_not_passed" in prompt
        seen["repair_prompts"] += 1
        sites = _sites(prompt)
        assert all(s["path"].startswith("app/") for s in sites) and not any("test" in s["path"] for s in sites)            # tests are never offered as sites
        cfg = [s for s in sites if s["path"] == "app/config.py"]
        return {"status": "OK", "sites": cfg + [{"path": "tests/test_freight.py", "line": 5, "literal": "500_000"}], "rationale": "The configuration constant mirrors the threshold."}
    use(q, stub)
    run_id = _start(q, cid, cand_id)
    got = _run_to_end(q, run_id)
    assert got["status"] == "REVIEW_READY", json.dumps(got["events"])[:1500]
    t = got["report"]["tests"]
    assert t["attempts"] == 2 and [h["verdict"] for h in t["history"]] == ["protected_tests_not_passed", "review_ready"] and t["history"][0]["patch_hash"] != t["history"][1]["patch_hash"]
    assert len(t["repairs"]) == 1 and t["repairs"][0]["sites"] == [{"path": "app/config.py", "line": 2, "literal": "500_000"}] and t["repairs"][0]["rejected_sites"][0]["reason"] == "site_not_offered"
    assert t["candidate_job_id"] == cc.repair_candidate_job_id(cand_id, 1) and t["verdict"] == "review_ready" and seen["repair_prompts"] == 1
    cand2 = q.admin.get(f"/v2/jobs/{t['candidate_job_id']}", params={"workspace_id": q.env.ws}).json()
    diff = cand2["execution_result"]["diff"]
    assert "FREE_FREIGHT_THRESHOLD_JPY = 600_000" in diff and "tests/" not in diff and cand2["execution_result"]["repair_of"] == cand_id
    roles = {c["role"]: c["status"] for c in got["children"]}
    assert roles == {"tester": "COMPLETED", "sandbox": "COMPLETED", "repairer": "COMPLETED"} and len(got["children"]) == 4
    assert got["budget"]["holds"] is True and got["budget"]["unallocated_reserved_usd"] == "0"
    kinds = [e.get("event_type") or e.get("type") or e.get("event") for e in got["events"]]
    assert "MA_REPAIR_REQUESTED" in kinds and "MA_REPAIR_ROUND" in kinds
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""         # the fixture origin never moves
    assert "never names a new number" in " ".join(got["report"]["limitations"])


def test_repairs_stop_at_the_cap_with_needs_human_and_the_failed_history_and_nothing_proposed_stops_early(rl_env):
    q, repo = rl_env
    cid, cand_id = _candidate(q)

    def useless(prompt):
        if "You are the TESTER" in prompt:
            return {"status": "OK", "cases": [], "notes": []}
        sites = [s for s in _sites(prompt) if s["path"] == "app/calculations.py"]                                            # a site already changed by the candidate: no progress
        return {"status": "OK", "sites": sites[:1], "rationale": "Try the docstring."}
    use(q, useless)
    run_id = _start(q, cid, cand_id)
    got = _run_to_end(q, run_id)
    assert got["status"] == "INCOMPLETE" and got["report"]["incomplete_reason"].startswith("needs_human:")
    t = got["report"]["tests"]
    assert t["attempts"] >= 1 and len(t["repairs"]) >= 1 and all(r["status"] == "completed" for r in t["repairs"]) and got["report"]["human_action_needed"] is True
    assert len([c for c in got["children"] if c["role"] == "repairer"]) <= ma.manifest("coder_tester")["repair_max"]
    assert got["budget"]["holds"] is True
    # a second run on the same binding is the same run (idempotent); a repairer that proposes nothing ends the loop at once
    assert q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "coder_tester", "candidate_job_id": cand_id}).json()["created"] is False
