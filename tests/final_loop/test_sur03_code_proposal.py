"""SUR-03 proposal-only coder, LOCAL: a confirmed freight change on the Harbor knowledge, a disposable FIXTURE repository created by the test, a SYNTHETIC model (stub through
the production pinned-call path), the real route, worker, budget and ledger. Covers kept / refused edits (offered files only, allowlist / protected / test files, unique find,
offered refs), the diff built in memory and parsed (a syntax error is reported, not hidden), the repository left untouched and nothing executed, idempotent requests,
visibility and permissions. 0 real provider calls; model quality is NOT measured here; model-written code is never run (no VM-class sandbox granted)."""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_fex_code_change import _config, fixture_repo  # noqa: F401  (fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import use
from tests.final_loop.test_syn01_code import FREIGHT_REQ, OP_FREIGHT, code_env, login  # noqa: F401  (code_env is a fixture)
from tests.pitch_brief.conftest import drain

LITERAL = "if goods_net_jpy >= 500_000:"


@pytest.fixture
def prop_env(code_env, fixture_repo, monkeypatch, tmp_path):     # noqa: F811
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  code.diff_proposal: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_prop.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    repo, target = fixture_repo
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(code_env.env.ws, repo, target)))
    code_env.repo = repo
    return code_env


def offered(prompt):
    files = dict(re.findall(r"=== file (\S+)\n(.*?)\n=== end \1", prompt, re.S))
    refs = re.findall(r"- ref=(\S+) file=", prompt)
    return files, refs


def proposer(break_syntax=False):
    def fn(prompt):
        files, refs = offered(prompt)
        assert list(files) == ["app/calculations.py"] and refs
        edits = [{"path": "app/calculations.py", "ref": refs[0], "find": LITERAL, "replace": LITERAL.replace("500_000", "600_000"), "rationale": "The decided threshold."},
                 {"path": "tests/test_freight.py", "ref": refs[0], "find": "500_000", "replace": "600_000", "rationale": "Bend the test."},
                 {"path": "requirements.txt", "ref": refs[0], "find": "#", "replace": "pinned", "rationale": "x"},
                 {"path": "app/calculations.py", "ref": refs[0], "find": "if order >= 1:", "replace": "pass", "rationale": "x"},
                 {"path": "app/calculations.py", "ref": refs[0], "find": "return", "replace": "return  ", "rationale": "x"},
                 {"path": "app/calculations.py", "ref": "F999", "find": "def calculate_tax(", "replace": "def calculate_tax2(", "rationale": "x"}]
        if break_syntax:
            edits.append({"path": "app/calculations.py", "ref": refs[0], "find": "def calculate_tax(", "replace": "def calculate_tax((", "rationale": "A typo."})
        return {"status": "proposed", "explanation": "Orders with goods of at least 600,000 JPY ship free; the comparator stays >= so exactly the threshold is free.",
                "edits": edits, "suggested_tests": ["An order of exactly 600,000 JPY ships free.", "An order of 599,999 JPY pays freight."], "notes": []}
    return fn


def post(q, client, cid, **body):
    return client.post(f"/v2/business/changes/{cid}/code/proposal", json={"workspace_id": q.env.ws, "repo_id": "fixture", **body})


def get(q, client, cid, **params):
    return client.get(f"/v2/business/changes/{cid}/code/proposal", params={"workspace_id": q.env.ws, "repo_id": "fixture", **params})


def test_proposed_edits_are_checked_the_diff_is_built_and_parsed_in_memory_and_nothing_is_applied_or_run(prop_env):
    q = prop_env
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.repo, capture_output=True, text=True).stdout.strip()
    cid, _approver = prepared(q, FREIGHT_REQ, [OP_FREIGHT])                                           # confirmed, not committed: a proposal needs no commit
    model = use(q, proposer())
    assert get(q, q.admin, cid).json()["status"] == "not_requested"
    r = post(q, q.admin, cid)
    assert r.status_code == 200, r.text
    assert r.json()["base_sha"] == head_before and model.calls == 0
    drain(q.env)
    got = get(q, q.admin, cid).json()
    assert got["status"] == "done", got
    assert got["applied"] is False and got["executed"] is False and got["label"].startswith("Proposal only")
    assert [f["path"] for f in got["files"]] == ["app/calculations.py"] and got["files"][0]["parses"] is True and got["static_checks_passed"] is True
    assert "+    if goods_net_jpy >= 600_000:" in got["diff"] and "-    if goods_net_jpy >= 500_000:" in got["diff"] and got["patch_hash"]
    assert sorted(x["reason"] for x in got["rejected"]) == ["file_not_offered", "file_not_offered", "find_not_in_file", "find_not_unique", "ref_not_offered"]
    assert got["suggested_tests"] and ">=" in got["explanation"] and got["ai_outcome"]["contribution"] == "retained"
    assert {w["path"] for w in got["coverage"]["withheld"]} <= {"requirements.txt"}                    # protected / unrecommended files are never offered
    # the repository is untouched: same HEAD, clean tree; no candidate job exists for this change
    assert subprocess.run(["git", "status", "--porcelain"], cwd=q.repo, capture_output=True, text=True).stdout == ""
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.repo, capture_output=True, text=True).stdout.strip() == head_before
    again = post(q, q.admin, cid)
    drain(q.env)
    assert again.json()["job_id"] == r.json()["job_id"] and model.calls == 1


def test_a_syntax_error_is_reported_and_refusals_hold(prop_env):
    q = prop_env
    cid, _approver = prepared(q, FREIGHT_REQ, [OP_FREIGHT])
    use(q, proposer(break_syntax=True))
    post(q, q.admin, cid, attempt=2)
    drain(q.env)
    got = get(q, q.admin, cid).json()
    assert got["status"] == "done" and got["files"][0]["parses"] is False and "SyntaxError" in got["files"][0]["check"] and got["static_checks_passed"] is False
    use(q, lambda p: {"status": "proposed", "explanation": "x", "edits": "not a list", "suggested_tests": [], "notes": []})
    post(q, q.admin, cid, attempt=3)
    drain(q.env)
    assert get(q, q.admin, cid).json()["reason_code"] == "schema_violation"
    assert post(q, q.admin, cid, base="no-such-commit").json()["code"] == "unknown_reference"
    assert post(q, q.admin, cid, repo_id="other").status_code == 404
    viewer = login(q, f"viewer-prop-{q.env.nonce}", ("viewer",))
    assert post(q, viewer, cid).status_code == 403 and get(q, viewer, cid).status_code == 403
    from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj, understand
    got2, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]), client=q.req)
    assert post(q, q.admin, got2["change_id"]).json()["code"] == "not_confirmed"
