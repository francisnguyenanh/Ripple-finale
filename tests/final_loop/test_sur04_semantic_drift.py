"""SUR-04 semantic drift ADVISORY next to the exact MON-CI check, LOCAL: a FIXTURE repository created by the test (a comparator turned around without any number changing,
a pure rename, a secret-like file), the accepted Harbor knowledge, a SYNTHETIC model (stub through the production pinned-call path), the real route, worker, budget and ledger.
Covers kept / refused findings (witness lines must be verbatim changed lines; comparators must differ), the exact result stored unchanged, withheld paths never sent, nothing
sendable = no call, idempotent requests, malformed output, visibility and permissions. 0 real provider calls; model quality is NOT measured here."""
from __future__ import annotations

import json
import subprocess

import pytest

from core.change_intelligence.v2 import business_ci as ci
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.conftest import drain

RULE = "rule-standard-manager-review"
PLANTED = "=".join(["API" + "_TOKEN", "do-not-send-{}"])                                   # assembled at run time: a fake secret line, never a literal in the source
BASE_CODE = "def approver(amount):\n    if amount >= 1000000:\n        return \"manager\"\n    return None\n\n\ndef helper(x):\n    return x\n"
HEAD_CODE = "def approver(amount):\n    if amount > 1000000:\n        return \"manager\"\n    return None\n\n\ndef renamed_helper(x):\n    return x\n"


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=f", "-c", "user.email=f@localhost", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def drift_env(qa_env, monkeypatch, tmp_path):                 # noqa: F811
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  ci.semantic_drift: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_drift.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "config" / "approval.yaml").write_text("routing_rules:\n  manager_threshold_jpy: 1000000\n", encoding="utf-8")
    (repo / "src" / "routing.py").write_text(BASE_CODE, encoding="utf-8")
    (repo / ".env").write_text(PLANTED.format(123) + "\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "src" / "routing.py").write_text(HEAD_CODE, encoding="utf-8")
    (repo / ".env").write_text(PLANTED.format(456) + "\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "head")
    from infra.adapters import v2_business
    acc = v2_business.business_view(qa_env.env.store, qa_env.env.tenant, qa_env.env.ws)["accepted"]
    policy = {"snapshot_id": acc["snapshot"]["snapshot_id"], "business_revision": acc["snapshot"]["business_revision"], "rules": list(acc["rules"])}
    mapping = {"profile": ci.PROFILE, "repo_id": "fixture", "policy_hash": ci.policy_hash(policy), "files": {"approval.yaml": "config/approval.yaml"}}

    def declare(**extra):
        monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps({"workspaces": {qa_env.env.ws: {"repos": {"fixture": {"path": str(repo), "ci_mapping": mapping, **extra}}}}}))
    declare()
    qa_env.repo, qa_env.declare, qa_env.policy, qa_env.mapping = repo, declare, policy, mapping
    return qa_env


def drift_model(prompt: str):
    assert "do-not-send" not in prompt                                                                # a secret-like file is never sent
    return {"status": "findings", "notes": ["The rename of helper keeps its behaviour."], "findings": [
        {"kind": "operator_change", "rule_id": RULE, "path": "src/routing.py", "witness_removed": "if amount >= 1000000:", "witness_added": "if amount > 1000000:",
         "statement": "A purchase of exactly 1000000 JPY no longer reaches the manager.", "confidence": "high"},
        {"kind": "operator_change", "rule_id": RULE, "path": "src/routing.py", "witness_removed": "", "witness_added": "if amount > 999:",
         "statement": "Invented line.", "confidence": "low"},
        {"kind": "condition_change", "rule_id": RULE, "path": ".env", "witness_removed": "", "witness_added": PLANTED.format(456), "statement": "Secret.", "confidence": "low"},
        {"kind": "actor_change", "rule_id": "rule-not-offered", "path": "src/routing.py", "witness_removed": "", "witness_added": "return \"manager\"", "statement": "x", "confidence": "low"},
        {"kind": "operator_change", "rule_id": RULE, "path": "src/routing.py", "witness_removed": "if amount >= 1000000:", "witness_added": "if amount > 1000000:",
         "statement": "This costs 5000000 JPY of exposure.", "confidence": "medium"}]}


def post(q, client, **body):
    return client.post("/v2/business/ci/semantic-drift", json={"workspace_id": q.env.ws, "repo_id": "fixture", "base": "HEAD~1", "head": "HEAD", **body})


def get(q, client, jid):
    return client.get(f"/v2/business/ci/semantic-drift/{jid}", params={"workspace_id": q.env.ws})


def test_witnessed_comparator_drift_is_kept_as_advisory_next_to_the_unchanged_exact_check(drift_env):
    q = drift_env
    model = use(q, drift_model)
    r = post(q, q.admin)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert r.json()["status"] in ("running", "waiting_for_approval") and len(r.json()["base_sha"]) == 40 and model.calls == 0
    drain(q.env)
    got = get(q, q.admin, jid).json()
    assert got["status"] == "done", got
    adv = got["advisory"]
    assert [f["kind"] for f in adv["findings"]] == ["operator_change"] and adv["findings"][0]["operator_witness"] == "confirmed"
    assert adv["findings"][0]["status"] == "advisory" and adv["conclusion"] == "advisory_findings" and got["never_a_pass"] is True
    assert sorted(x["reason"] for x in adv["rejected"]) == ["figure_not_in_witness_or_rule:5000000", "path_not_in_diff", "rule_not_offered", "witness_added_not_in_diff"]
    assert got["coverage"]["withheld_paths"] == [".env"] and got["coverage"]["sent_paths"] == ["src/routing.py"]
    # the exact MON-CI result is the one business_ci computes on the same commits: the advisory did not touch it
    from infra.worker import ci_semantic_drift as csd
    exact = csd.exact_check({"path": str(q.repo), "ci_mapping": q.mapping}, "fixture", q.policy, got["base_sha"], got["head_sha"],
                            ["src/routing.py", ".env"])
    assert got["exact"]["status"] == "ran" and got["exact"]["report_hash"] == exact["report_hash"] and got["exact"]["conclusion"] == exact["conclusion"]
    assert got["ai_outcome"]["contribution"] == "retained"
    again = post(q, q.admin)
    drain(q.env)
    assert again.json()["job_id"] == jid and model.calls == 1                                         # same request, same job, no second call


def test_nothing_sendable_makes_no_call_malformed_output_fails_and_permissions_hold(drift_env):
    q = drift_env
    model = use(q, drift_model)
    q.declare(allowlist=["docs/*"])                                                                    # nothing in the diff may be sent
    r = post(q, q.admin, attempt=2)
    drain(q.env)
    got = get(q, q.admin, r.json()["job_id"]).json()
    assert got["status"] == "done" and got["advisory"]["model_status"] == "not_run" and model.calls == 0 and got["exact"]["status"] == "ran"
    assert got["advisory"]["conclusion"] == "not_run_nothing_sendable" and got["never_a_pass"] is True
    q.declare()
    use(q, lambda p: {"status": "findings", "findings": "not a list", "notes": []})
    bad = post(q, q.admin, attempt=3)
    drain(q.env)
    st = get(q, q.admin, bad.json()["job_id"]).json()
    assert st["status"] == "failed" and st["reason_code"] == "schema_violation" and "Nothing is concluded" in st["reason"]
    assert post(q, q.admin, base="no-such-ref").json()["code"] == "unknown_reference"
    assert post(q, q.admin, repo_id="other").status_code == 404
    assert post(q, q.admin, base="--upload-pack=x").json()["code"] == "unknown_reference"            # an option is never passed to git
    viewer = login(q, f"viewer-drift-{q.env.nonce}", ("viewer",))
    assert post(q, viewer).status_code == 403 and get(q, viewer, r.json()["job_id"]).status_code == 403
    q.declare(visible_to=["admin_only_role"])
    assert post(q, q.admin, attempt=1).status_code == 404 and get(q, q.admin, r.json()["job_id"]).status_code == 404   # a hidden repository is indistinguishable
