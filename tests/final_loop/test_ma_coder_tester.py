"""MA-C1 Coder + Tester (LOCAL): the deterministic FEX-03 candidate is the coder; a SYNTHETIC tester proposes TYPED cases before seeing the patch; the server renders them into one
pytest file (no model-written code) and runs them with the protected declared commands on the base and the patched disposable clone; the join reports protected vs generated
outcomes and a verdict. Real routes, worker, ledger, fixture repository. 0 real provider calls."""
from __future__ import annotations

import json

import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from tests.final_loop.test_fex_code_change import _committed, _config, _settle, fixture_repo  # noqa: F401  (fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import use
from tests.final_loop.test_syn01_code import code_env  # noqa: F401  (fixture)
from tests.pitch_brief.conftest import drain


@pytest.fixture
def ct_env(code_env, fixture_repo, monkeypatch, tmp_path):     # noqa: F811
    import os
    q = code_env
    repo, target = fixture_repo
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, repo, target)))
    monkeypatch.setenv("RIPPLE_EXTENSION_MOCK_TARGET", "1")
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    if "ma.specialist" not in cfg:
        cfg = cfg.replace("role_model_ids:\n", "role_model_ids:\n  ma.specialist: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_ct.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return q, repo


def case(name, fn, args, op, value, module="app.calculations"):
    return {"name": name, "module": module, "function": fn, "args": args, "expect_op": op, "expect_value": value, "rationale": "distinguishes the decided threshold"}


def stub_tester(prompt: str):
    assert "You are the TESTER" in prompt and "module=app.calculations" in prompt and "calculate_freight" in prompt     # the tester sees modules/functions from the code evidence only
    return {"status": "OK", "cases": [case("free_at_new_threshold", "calculate_freight", [600000], "eq", 0),
                                      case("charged_just_below_new_threshold", "calculate_freight", [599999], "eq", 2000),
                                      case("old_threshold_still_free", "calculate_freight", [550000], "eq", 0),
                                      case("outside_allowlist", "system", ["echo"], "eq", 0, module="os"),
                                      case("unknown_function", "calculate_bonus", [1], "eq", 0),
                                      case("Bad Name", "calculate_freight", [1], "eq", 0)], "notes": ["Cases test the decision, not the patch."]}


def _candidate(q):
    cid, approver, committed = _committed(q)
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 200, r.text
    cand_id = r.json()["job_id"]
    got = _settle(q, q.admin, cid, "candidate")
    assert got["candidate"]["status"] == "PROPOSED"
    return cid, cand_id


def test_tester_cases_are_validated_rendered_and_run_on_base_and_patched_clones_next_to_the_protected_tests(ct_env):
    q, repo = ct_env
    cid, cand_id = _candidate(q)
    use(q, stub_tester)
    r = q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "coder_tester"})
    assert r.status_code == 409 and r.json()["code"] == "candidate_required"
    r = q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "coder_tester", "candidate_job_id": "cand-nope"})
    assert r.status_code == 409 and r.json()["code"] == "candidate_not_completed"
    r = q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "coder_tester", "candidate_job_id": cand_id})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    st = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert st["caps"]["max_children"] == 3 and [c["role"] for c in st["children"]] == ["tester"]                                   # tester + up to 2 repair calls (AIX-C1) are reserved at the root and st["binding"]["candidate_job_id"] == cand_id
    for _ in range(40):
        drain(q.env)
        got = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
        if got["status"] in ma.TERMINAL:
            break
    assert got["status"] == "REVIEW_READY", json.dumps(got["events"])[:800]
    rep = got["report"]
    t = rep["tests"]
    assert t["proposed_cases"] == 3 and sorted(r["reason"] for r in t["rejected_cases"]) == ["function_not_in_code_evidence", "module_not_in_code_evidence", "name_not_identifier"]
    assert t["verdict"] == "review_ready" and t["protected"]["patched"] == "PASSED" and t["protected"]["base"] == "FAILED"                 # the planted acceptance test flips with the patch
    by = {c["name"].rsplit("_", 1)[0]: c for c in t["cases"]}
    assert by["test_free_at_new_threshold"]["reading"] == "passes_both"                                                                       # 600,000 is free under both thresholds: no discrimination
    assert by["test_charged_just_below_new_threshold"]["reading"] == "distinguishes" and by["test_charged_just_below_new_threshold"]["base"] == "FAILED" and by["test_charged_just_below_new_threshold"]["patched"] == "PASSED"
    assert by["test_old_threshold_still_free"]["reading"] == "regression" and by["test_old_threshold_still_free"]["base"] == "PASSED" and by["test_old_threshold_still_free"]["patched"] == "FAILED"
    assert t["repairs"] == [] and t["attempts"] == 1 and t["history"][0]["verdict"] == "review_ready" and t["test_file"].startswith("tests/test_ripple_generated_") and "advisory" in t["isolation"] and t["patch_hash"]
    sandbox = next(c for c in got["children"] if c["role"] == "sandbox")
    assert sandbox["status"] == "COMPLETED" and sandbox["entry_state"] in (None, "SETTLED") and (sandbox["cost_usd"] in (None, "0", "0.0") or float(sandbox["cost_usd"]) == 0)   # deterministic child: no provider cost
    assert got["budget"]["holds"] is True and got["budget"]["unallocated_reserved_usd"] == "0"
    import subprocess
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""                           # the fixture origin never moves
    assert "generated" in " ".join(rep["limitations"]).lower()
