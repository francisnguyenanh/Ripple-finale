"""Extension ext/1, FEX-01/02 LOCAL ONLY: a declared LOCAL fixture repository is read at its commit into one source refresh revision (admission, staged blobs, prepared refresh,
CAS commit, read-back), the carry-over analysis then compares the decided knowledge with the repository's bytes, a repeated commit is one job, a rewritten history is labelled,
protected and refused files are skipped and listed. Real routes, worker and store; no network, no remote, no model apart from the STUB understanding of the change request.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from infra.adapters import v2_business, v2_business_commit
from infra.worker import git_sync as gs
from tests.final_loop.test_chg04_review import decide, prepared, review
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import HARBOR_INPUT, drain

YAML = (HARBOR_INPUT / "config" / "approval.yaml").read_bytes()


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "harbor repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "approval.yaml").write_bytes(YAML)
    (repo / "config" / "service.key").write_text("not a secret, but the name is protected\n", encoding="utf-8")
    (repo / "config" / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _config(ws_id, repo):
    return {"workspaces": {ws_id: {"repos": {"harbor": {"path": str(repo), "allowlist": ["config/"], "mount": "harbor_demo"}}}}}


def sync(q, client, repo_id="harbor"):
    return client.post("/v2/sources/git/sync", json={"workspace_id": q.env.ws, "repo_id": repo_id})


def settle(q, job_id, tries=60):
    for _ in range(tries):
        drain(q.env)
        rec = q.env.store.job_record(q.env.tenant, q.env.ws, job_id)
        if rec["execution_status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return rec
        time.sleep(0.3)
    return rec


def test_repository_commit_becomes_a_source_revision_and_the_carry_over_reads_the_decided_value_back(qa_env, fixture_repo, monkeypatch):
    q = qa_env
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, fixture_repo)))
    # a decided knowledge change: the manager threshold becomes 1,500,000 (committed as TARGET knowledge, files still pending)
    cid, approver = prepared(q)
    binding = review(q, approver, cid).json()["binding"]
    assert decide(q, approver, cid, binding).status_code == 200
    v2_business_commit.commit_change(q.env.store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding)
    head0 = q.env.store.head(q.env.tenant, q.env.ws)
    artifacts0 = q.env.store.source_revision_view(q.env.tenant, q.env.ws, head0.source_revision_id)["artifacts"]
    # a developer edits the configuration in the repository and commits
    path = fixture_repo / "config" / "approval.yaml"
    path.write_bytes(path.read_bytes().replace(b"standard_manager_threshold_jpy: 1000000", b"standard_manager_threshold_jpy: 1500000"))
    _git(fixture_repo, "commit", "-q", "-am", "raise the manager threshold")
    sha1 = gs.head_sha(str(fixture_repo))
    r = sync(q, q.admin)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True and r.json()["sha"] == sha1
    rec = settle(q, r.json()["job_id"])
    assert rec["execution_status"] == "COMPLETED", rec.get("execution_reason")
    res = rec["execution_result"]
    assert res["status"] == "SYNCED" and res["history"] == "first_sync" and res["files"]["changed"] == ["harbor_demo/config/approval.yaml"]
    assert res["files"]["deleted"] == ["harbor_demo/config/workflow.yaml"] and res["files"]["added"] == []                              # the repository owns config/: a file it does not hold is a deletion, acknowledged by the sync
    assert {s["path"] for s in res["skipped"]} >= {"config/service.key", "README.md"} and any(s["path"].endswith("blob.bin") for s in res["skipped"])
    head1 = q.env.store.head(q.env.tenant, q.env.ws)
    assert head1.source_revision_id == res["source_revision_id"] != head0.source_revision_id
    artifacts1 = q.env.store.source_revision_view(q.env.tenant, q.env.ws, head1.source_revision_id)["artifacts"]
    assert {a["logical_path"] for a in artifacts1} == {a["logical_path"] for a in artifacts0} - {"harbor_demo/config/workflow.yaml"}        # the other Harbor files are carried unchanged
    yaml_now = q.env.store.read_blob(q.env.tenant, q.env.ws, head1.source_revision_id, next(a["artifact_id"] for a in artifacts1 if a["logical_path"].endswith("config/approval.yaml")))
    assert b"standard_manager_threshold_jpy: 1500000" in yaml_now
    # the Final carry-over compares the decided knowledge with the repository's bytes
    a = q.admin.post("/v2/business/analyse", json={"workspace_id": q.env.ws})
    assert a.status_code == 200, a.text
    view = v2_business.business_view(q.env.store, q.env.tenant, q.env.ws)
    target = next(r_ for r_ in view["pending"]["rules"] if r_["rule_id"] == "rule-standard-manager-review-target")
    assert target["implementation_status"] == "verified"                                                                                  # source-aligned, never behaviour
    # the same commit again is the same job and a no-op
    again = sync(q, q.admin)
    assert again.status_code == 200 and again.json()["job_id"] != r.json()["job_id"] or again.json()["created"] is False
    rec2 = settle(q, again.json()["job_id"])
    assert rec2["execution_status"] == "COMPLETED" and rec2["execution_result"]["status"] in ("NO_OP", "SYNCED") and rec2["execution_result"]["sha"] == sha1
    # a rewritten history is labelled, never hidden
    path.write_bytes(path.read_bytes().replace(b"capex_finance_threshold_jpy: 2000000", b"capex_finance_threshold_jpy: 2500000"))
    _git(fixture_repo, "commit", "-q", "--amend", "-am", "rewritten")
    r3 = sync(q, q.admin)
    rec3 = settle(q, r3.json()["job_id"])
    assert rec3["execution_status"] == "COMPLETED" and rec3["execution_result"]["history"] == "rewritten" and rec3["execution_result"]["status"] == "SYNCED"


def test_undeclared_repository_and_missing_permission_are_refused_and_nothing_is_executed(qa_env, fixture_repo, monkeypatch):
    q = qa_env
    r = sync(q, q.admin)
    assert r.status_code == 409 and r.json()["code"] == "repo_not_declared"
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, fixture_repo)))
    assert sync(q, q.admin, repo_id="other").status_code == 409
    from tests.final_loop.test_chg04_review import login
    viewer = login(q, f"viewer-gs-{q.env.nonce}", ("viewer",))
    assert sync(q, viewer).status_code == 403
    (fixture_repo / "config" / "hook.py").write_text("import os\nos.system('echo never run')\n", encoding="utf-8")
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "a script in the config folder")
    r = sync(q, q.admin)
    rec = settle(q, r.json()["job_id"])
    assert rec["execution_status"] == "COMPLETED"
    res = rec["execution_result"]
    assert "harbor_demo/config/hook.py" in res["files"]["added"]                                                                              # read as a source file (data), never executed
    assert q.env.store.head(q.env.tenant, q.env.ws).source_revision_id == res["source_revision_id"]


SANDBOX_BRANCH = "ripple-test/manager-threshold-2500000"


@pytest.mark.skipif(not os.environ.get("RIPPLE_GIT_SANDBOX_MIRROR"), reason="opt-in: a local clone of the private GitHub sandbox repository (operator tool `git_sandbox.py mirror`)")
def test_a_clone_of_the_github_sandbox_syncs_main_then_the_test_branch_as_a_linear_history(qa_env, tmp_path, monkeypatch):
    """FEX-01/02 over the content of a REAL remote (the private sandbox repository), read through a local clone of the operator's mirror: the product itself still reads only
    declared local paths, never the network. main (the Harbor fixture) is the first sync; the test branch's commit is a linear successor whose only change is approval.yaml."""
    q = qa_env
    repo = tmp_path / "sandbox clone"
    subprocess.run(["git", "clone", "-q", os.environ["RIPPLE_GIT_SANDBOX_MIRROR"], str(repo)], check=True, capture_output=True)
    _git(repo, "fetch", "-q", "origin", "+refs/remotes/origin/*:refs/remotes/sandbox/*")
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_config(q.env.ws, repo)))
    rec = settle(q, sync(q, q.admin).json()["job_id"])
    assert rec["execution_status"] == "COMPLETED", rec.get("execution_reason")
    first = rec["execution_result"]
    assert first["history"] == "first_sync" and first["sha"] == gs.head_sha(str(repo)) and "harbor_demo/config/approval.yaml" not in first["files"]["changed"]
    _git(repo, "checkout", "-q", "--detach", f"sandbox/{SANDBOX_BRANCH}")
    rec2 = settle(q, sync(q, q.admin).json()["job_id"])
    assert rec2["execution_status"] == "COMPLETED", rec2.get("execution_reason")
    res = rec2["execution_result"]
    assert res["status"] == "SYNCED" and res["history"] == "linear" and res["previous_sha"] == first["sha"] and res["files"]["changed"] == ["harbor_demo/config/approval.yaml"]
    head = q.env.store.head(q.env.tenant, q.env.ws)
    arts = q.env.store.source_revision_view(q.env.tenant, q.env.ws, head.source_revision_id)["artifacts"]
    blob = q.env.store.read_blob(q.env.tenant, q.env.ws, head.source_revision_id, next(a["artifact_id"] for a in arts if a["logical_path"].endswith("config/approval.yaml")))
    assert b"standard_manager_threshold_jpy: 2500000" in blob
