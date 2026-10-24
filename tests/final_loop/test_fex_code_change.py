"""Extension ext/1 (FEX-03..08), LOCAL ONLY: a candidate code patch for a committed business change is built on a disposable clone of a FIXTURE repository created by this test,
validated by the repository's own declared command, approved by a distinct person bound to both hashes, refused for publishing (no grant), promoted into a MOCK target directory,
verified there, and rolled back. Real routes, worker, store and ledger; 0 provider calls apart from the STUB understanding of the change request. Never the owner's repository.
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from core.change_intelligence.v2 import code_patch
from infra.worker import code_change as cc
from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_chg04_review import decide, prepared, review
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_syn01_code import FREIGHT_REQ, OP_FREIGHT, code_env  # noqa: F401  (code_env is a fixture)
from tests.pitch_brief.conftest import HARBOR_INPUT, drain

CALC = (HARBOR_INPUT / "app" / "calculations.py").read_text(encoding="utf-8")
TEST_FILE = '''from app import calculations

def test_free_freight_threshold_is_the_decided_value():
    import inspect
    src = inspect.getsource(calculations.calculate_freight)
    assert "600_000" in src and "500_000" not in src
'''


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def fixture_repo(tmp_path):
    """A disposable git repository with the Harbor calculations module, a planted acceptance test and a validate command; plus a mock target directory."""
    repo = tmp_path / "fixture repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "calculations.py").write_text(CALC, encoding="utf-8")
    (repo / "tests" / "test_freight.py").write_text(TEST_FILE, encoding="utf-8")
    (repo / "requirements.txt").write_text("# protected file: never patched\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "add", "-A")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "fixture baseline")
    target = tmp_path / "mock target"
    return repo, target


def _config(ws_id, repo, target, validate=True):
    return {"workspaces": {ws_id: {"repos": {"fixture": {"path": str(repo), "allowlist": ["app/"], "environments": ["mock-staging"], "mock_target": str(target),
                                                        "validate": [["{python}", "-m", "pytest", "-q", "tests"]] if validate else [],
                                                        "verify": [["{python}", "-c", "from app import calculations; import inspect; assert '600_000' in inspect.getsource(calculations.calculate_freight)"]]}}}}}


# ---- pure patch planning ------------------------------------------------------------------------------------------------------------------------------------------------------

def test_plan_replaces_only_the_supported_literals_inside_the_observed_text_and_lists_the_rest():
    text = "def calculate_freight(order):\n    '''Free from 500,000 JPY.'''\n    if order >= 500_000:\n        return 0\n    return 1500\n"
    repo_files = ["app/calculations.py", "app/other.py", "requirements.txt"]
    recs = [{"ref": "F001", "status": "SUPPORTED", "op_id": "op-1", "rule_id": "rule-freight-free", "path": "calculations.py", "observed_logic": text,
             "literals": [{"line": 2, "literal": "500,000"}, {"line": 3, "literal": "500_000"}]},
            {"ref": "F002", "status": "UNCERTAIN", "op_id": "op-1", "rule_id": "rule-freight-free", "path": "routing.py", "why": "not a literal"},
            {"ref": "F003", "status": "SUPPORTED", "op_id": "op-1", "rule_id": "rule-freight-free", "path": "requirements.txt", "observed_logic": "x", "literals": [{"line": 1, "literal": "x"}]},
            {"ref": "F004", "status": "SUPPORTED", "op_id": "op-1", "rule_id": "rule-freight-free", "path": "../etc/passwd", "observed_logic": "x", "literals": []}]
    ops = {"op-1": {"kind": "set_value", "desired": {"value": 600000, "unit": "JPY"}}}
    plan = code_patch.plan(recs, ops, repo_files, lambda p: text if p == "app/calculations.py" else "x", allowlist=["app/"])
    assert plan["implemented"] == [{"ref": "F001", "rule_id": "rule-freight-free", "path": "app/calculations.py", "replacements": 2}]
    assert "+    if order >= 600_000:" in plan["diff"] and "Free from 600,000 JPY" in plan["diff"] and "1500" not in plan["diff"].split("+++")[1].split("@@")[0]
    assert sorted(r["reason"] for r in plan["not_implemented"]) == ["path_outside_allowlist", "protected_file", "recommendation_uncertain"]
    assert plan["patch_hash"] == code_patch.patch_hash(plan["diff"]) and plan["files"][0]["path"] == "app/calculations.py"
    again = code_patch.plan(recs, ops, repo_files, lambda p: text, allowlist=["app/"])
    assert again["patch_hash"] == plan["patch_hash"]                                                                   # deterministic identity
    missing = code_patch.plan([dict(recs[0], observed_logic="def other(): pass")], ops, repo_files, lambda p: text, allowlist=["app/"])
    assert missing["diff"] == "" and missing["not_implemented"][0]["reason"] == "observed_text_not_found"


@pytest.mark.parametrize("path, ok", [("app/x.py", True), ("app/../x.py", False), ("/etc/x", False), ("app\\x.py", False), ("lib/x.py", False), ("app/Dockerfile", False)])
def test_paths_outside_the_allowlist_or_protected_are_refused(path, ok):
    assert (code_patch.path_allowed(path, ["app/"]) is None) is ok


# ---- real routes, worker, ledger, fixture repository and mock target --------------------------------------------------------------------------------------------------------

def _settle(q, client, cid, key, tries=120):
    for _ in range(tries):
        drain(q.env)
        got = client.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
        part = got.get(key)
        if part and part.get("job_status") in ("completed", "failed"):       # the job's own state; `status` becomes the result's status once completed
            return got
        time.sleep(0.3)
    return got


def _committed(q):
    """A confirmed freight change, approved by a distinct approver and committed as decided knowledge (the input of every patch)."""
    from infra.adapters import v2_business_commit
    cid, approver = prepared(q, FREIGHT_REQ, [OP_FREIGHT])
    binding = review(q, approver, cid).json()["binding"]
    assert decide(q, approver, cid, binding, acknowledged_flags=True).status_code == 200
    out = v2_business_commit.commit_change(q.env.store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding)
    return cid, approver, out


def test_candidate_validation_approval_refused_publish_mock_promotion_verification_and_rollback(code_env, fixture_repo, monkeypatch):
    q = code_env
    repo, target = fixture_repo
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, repo, target)))
    monkeypatch.setenv("RIPPLE_EXTENSION_MOCK_TARGET", "1")
    cid, approver, committed = _committed(q)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    # candidate: built by the requester on a disposable clone
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True and r.json()["job_id"].startswith("cand-")
    cand_id = r.json()["job_id"]
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"}).json()["created"] is False     # same inputs, same job
    got = _settle(q, q.admin, cid, "candidate")
    c = got["candidate"]
    assert c["status"] == "PROPOSED" and c["base_sha"] == before and c["files"] == [{"path": "app/calculations.py", "replacements": 2}] and c["implemented"][0]["rule_id"] == "rule-freight-free"
    assert "+    if" in c["diff"] and "600_000" in c["diff"] and c["target_pin"]["snapshot_id"] == committed["snapshot_id"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""                                      # the fixture repository itself is untouched
    plain = q.req.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()["candidate"]
    assert "diff" not in plain and plain["files"] == [{"replacements": 2}]                                                                          # no evidence rights: counts only
    # validation: the planted acceptance test fails on the baseline and passes with the patch
    v = q.req.post(f"/v2/business/changes/{cid}/code/candidate/validate", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert v.status_code == 200, v.text
    got = _settle(q, q.admin, cid, "validation")
    val = got["validation"]
    assert val["report_status"] == "PASSED" and val["patch_hash"] == c["patch_hash"] and val["commands"][0]["exit"] == 0 and "advisory" in val["isolation"], val
    # approval: the requester cannot; an approver can, bound to both hashes; a wrong hash is stale
    from tests.final_loop.test_syn01_code import login
    appr = login(q, f"appr-x-{q.env.nonce}", ("change_approver",))
    body = {"workspace_id": q.env.ws, "candidate_job_id": cand_id, "action": "approve", "reason": "Reviewed the diff: the literal change is the decided value.", "patch_hash": c["patch_hash"], "validation_hash": val["validation_hash"]}
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate/decide", json=body).status_code == 403
    bad = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json=dict(body, validation_hash="0" * 64))
    assert bad.status_code == 409 and bad.json()["code"] == "stale_binding"
    ok = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json=body)
    assert ok.status_code == 200, ok.text
    again = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json=body)
    assert again.status_code == 409 and again.json()["code"] == "already_decided"
    # publish: refused, intent recorded
    pub = appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert pub.status_code == 409 and pub.json()["code"] == "no_publish_grant"
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
    assert st["approval"]["actor_id"] == f"appr-x-{q.env.nonce}" and st["pr_intent"]["status"] == "REFUSED" and st["pr_intent"]["branch"] == f"ripple/{cid}"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip() == before                              # nothing pushed or committed to the fixture origin
    # mock promotion by the admin (code.promote), then verification against the mock target
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate/promote", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "mock-staging"}).status_code == 403
    wrong = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/promote", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "production"})
    assert wrong.status_code == 200
    drain(q.env)
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
    assert st["promotions"] == []                                                                                                                    # the undeclared environment was refused by the worker
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, cc.promotion_job_id(cand_id, "production"))
    assert rec["execution_status"] == "FAILED" and rec["execution_reason"].startswith("wrong_environment")
    p = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/promote", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "mock-staging"})
    assert p.status_code == 200, p.text
    drain(q.env)
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
    assert len(st["promotions"]) == 1 and st["promotions"][0]["mock"] is True and (target / "mock-staging" / "current" / "app" / "calculations.py").read_text(encoding="utf-8").count("600_000") >= 1
    ver = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/verify", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "mock-staging"})
    assert ver.status_code == 200, ver.text
    drain(q.env)
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
    assert st["verifications"][0]["status"] == "VERIFIED" and st["verifications"][0]["artifact_hash"] == st["promotions"][0]["artifact_hash"]
    # the business change is NOT implemented by a mock promotion: the Final journey still waits for the read-back
    j = q.admin.get(f"/v2/business/changes/{cid}/journey", params={"workspace_id": q.env.ws}).json()
    assert j["fully_implemented"] is False and j["knowledge_only_complete"] is True
    # rollback restores the previous tree (none before: the first promotion has no previous -> refused), then after a second promotion attempt it is idempotent
    rb = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/rollback", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "mock-staging"})
    assert rb.status_code == 200
    drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, cc.rollback_job_id(cand_id, "mock-staging", 0))
    assert rec["execution_status"] == "FAILED" and rec["execution_reason"].startswith("nothing_to_roll_back")
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""


def test_without_a_committed_change_a_declared_repository_or_a_passed_validation_nothing_proceeds(code_env, fixture_repo, monkeypatch):
    q = code_env
    repo, target = fixture_repo
    cid, _ = confirmed(q, FREIGHT_REQ, [OP_FREIGHT])
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 409 and r.json()["code"] == "repo_not_declared"                                                   # no RIPPLE_EXTENSION_CONFIG
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, repo, target, validate=False)))
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 409 and r.json()["code"] == "knowledge_not_committed"                                             # a patch implements decided knowledge only
    cid, approver, committed = _committed(q)
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 200
    cand_id = r.json()["job_id"]
    got = _settle(q, q.admin, cid, "candidate")
    assert got["candidate"]["status"] == "PROPOSED"
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate/validate", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id}).status_code == 200
    got = _settle(q, q.admin, cid, "validation")
    assert got["validation"]["report_status"] == "NOT_RUN"                                                                      # no declared command: not a pass
    from tests.final_loop.test_syn01_code import login
    appr = login(q, f"appr-y-{q.env.nonce}", ("change_approver",))
    d = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "action": "approve", "reason": "trying",
                                                                              "patch_hash": got["candidate"]["patch_hash"], "validation_hash": got["validation"]["validation_hash"]})
    assert d.status_code == 409 and d.json()["code"] == "validation_not_passed"
    pr = appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert pr.status_code == 409 and pr.json()["code"] == "not_approved"
    monkeypatch.delenv("RIPPLE_EXTENSION_MOCK_TARGET", raising=False)
    p = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/promote", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "mock-staging"})
    assert p.status_code == 409 and p.json()["code"] in ("validation_not_passed", "not_approved")
    viewer = login(q, f"viewer-fx-{q.env.nonce}", ("viewer",))
    assert viewer.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).status_code == 404
    assert viewer.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"}).status_code == 403


# ---- FEX-08: a successful rollback after a SECOND promotion into the same mock environment ---------------------------------------------------------------------------------

def _config_two_repos(ws_id, repo, repo_b, target):
    one = _config(ws_id, repo, target)["workspaces"][ws_id]["repos"]["fixture"]
    return {"workspaces": {ws_id: {"repos": {"fixture": one, "fixture-b": dict(one, path=str(repo_b))}}}}


def _second_fixture_repo(repo, tmp_path):
    """A second disposable repository: the first one plus one more committed file, so its promoted artifact differs from the first one's (distinct tree hash)."""
    import shutil
    repo_b = tmp_path / "fixture repo b"
    shutil.copytree(repo, repo_b)
    (repo_b / "app" / "release_note.txt").write_text("second artifact", encoding="utf-8")
    _git(repo_b, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "add", "-A")
    _git(repo_b, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "second fixture revision")
    return repo_b


def _promoted(q, cid, repo_id, approver_name):
    """candidate -> validation -> approval by a distinct approver -> mock promotion; returns (candidate job id, promoted artifact hash)."""
    from tests.final_loop.test_syn01_code import login
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": repo_id})
    assert r.status_code == 200, r.text
    cand_id = r.json()["job_id"]
    c = _settle(q, q.admin, cid, "candidate")["candidate"]
    assert c["status"] == "PROPOSED", c
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate/validate", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id}).status_code == 200
    val = _settle(q, q.admin, cid, "validation")["validation"]
    assert val["report_status"] == "PASSED", val
    appr = login(q, approver_name, ("change_approver",))
    ok = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "action": "approve", "reason": "Reviewed the diff.",
                                                                              "patch_hash": c["patch_hash"], "validation_hash": val["validation_hash"]})
    assert ok.status_code == 200, ok.text
    p = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/promote", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "environment_id": "mock-staging"})
    assert p.status_code == 200, p.text
    drain(q.env)
    prom = q.env.store.job_record(q.env.tenant, q.env.ws, cc.promotion_job_id(cand_id, "mock-staging"))
    assert prom["execution_status"] == "COMPLETED", prom
    return cand_id, prom["execution_result"]["artifact_hash"]


def test_rollback_after_a_second_promotion_restores_the_first_artifact_and_a_repeat_is_refused(code_env, fixture_repo, monkeypatch, tmp_path):
    """FEX-08 success path. Two candidates of the same committed change (two declared repositories: the fixture and a copy with one more revision) are promoted in turn
    into ONE mock environment;
    the second promotion keeps the first artifact as `previous`; rollback restores exactly that artifact (hash equality), verification of the second candidate then
    reports the mismatch, a second rollback finds nothing to restore, and the fixture origin never moves."""
    q = code_env
    repo, target = fixture_repo
    repo_b = _second_fixture_repo(repo, tmp_path)
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config_two_repos(q.env.ws, repo, repo_b, target)))
    monkeypatch.setenv("RIPPLE_EXTENSION_MOCK_TARGET", "1")
    cid, _approver, _committed_out = _committed(q)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    env_dir = target / "mock-staging"
    cand_a, art_a = _promoted(q, cid, "fixture", f"appr-ra-{q.env.nonce}")
    assert cc.tree_hash(str(env_dir / "current")) == art_a and not (env_dir / "previous").exists()                     # first promotion: nothing to roll back to yet
    cand_b, art_b = _promoted(q, cid, "fixture-b", f"appr-rb-{q.env.nonce}")
    assert cand_b != cand_a and art_b != art_a                                                                                              # a genuinely different artifact
    assert cc.tree_hash(str(env_dir / "current")) == art_b and cc.tree_hash(str(env_dir / "previous")) == art_a           # second promotion kept the first artifact
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws, "candidate_job_id": cand_b}).json()
    assert len(st["promotions"]) == 1 and st["promotions"][0]["artifact_hash"] == art_b
    # rollback of the second promotion: the first artifact is current again, recorded with its hash
    rb = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/rollback", json={"workspace_id": q.env.ws, "candidate_job_id": cand_b, "environment_id": "mock-staging"})
    assert rb.status_code == 200 and rb.json()["created"] is True, rb.text
    drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, cc.rollback_job_id(cand_b, "mock-staging", 0))
    assert rec["execution_status"] == "COMPLETED" and rec["execution_result"]["status"] == "ROLLED_BACK" and rec["execution_result"]["restored_artifact_hash"] == art_a and rec["execution_result"]["mock"] is True, rec
    assert cc.tree_hash(str(env_dir / "current")) == art_a and not (env_dir / "previous").exists()
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws, "candidate_job_id": cand_b}).json()
    assert len(st["rollbacks"]) == 1 and st["rollbacks"][0]["restored_artifact_hash"] == art_a and st["approval"] is not None                      # approvals untouched
    # the target no longer holds the second artifact: its verification says so instead of passing
    ver = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/verify", json={"workspace_id": q.env.ws, "candidate_job_id": cand_b, "environment_id": "mock-staging"})
    assert ver.status_code == 200, ver.text
    drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, cc.verify_job_id(cand_b, "mock-staging", art_b))
    assert rec["execution_status"] == "FAILED" and rec["execution_reason"].startswith("artifact_mismatch"), rec
    # a second rollback is a new job (n=1) and finds nothing to restore; the first restoration stays the only rollback record
    rb2 = q.admin.post(f"/v2/business/changes/{cid}/code/candidate/rollback", json={"workspace_id": q.env.ws, "candidate_job_id": cand_b, "environment_id": "mock-staging"})
    assert rb2.status_code == 200 and rb2.json()["created"] is True and rb2.json()["job_id"] == cc.rollback_job_id(cand_b, "mock-staging", 1)
    drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, cc.rollback_job_id(cand_b, "mock-staging", 1))
    assert rec["execution_status"] == "FAILED" and rec["execution_reason"].startswith("nothing_to_roll_back")
    st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws, "candidate_job_id": cand_b}).json()
    assert len(st["rollbacks"]) == 1 and cc.tree_hash(str(env_dir / "current")) == art_a
    # the business change is still not implemented by any of this, and the fixture origin never moved
    j = q.admin.get(f"/v2/business/changes/{cid}/journey", params={"workspace_id": q.env.ws}).json()
    assert j["fully_implemented"] is False
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip() == before
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""
