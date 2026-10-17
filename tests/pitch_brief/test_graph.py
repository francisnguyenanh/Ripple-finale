"""J1: GET /v2/knowledge/graph over the real Harbor import + stub-provider extraction (no model call)."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import APPROVAL, FINGUIDE, POLICY, build, cand_id
from tests.pitch_brief.conftest import create_job, drain


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _graph(client, env):
    r = client.get("/v2/knowledge/graph", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def test_graph_files_rules_evidence_and_disclosed_coverage(env, ch):
    g = _graph(ch.admin, env)
    types = {}
    for n in g["nodes"]:
        types.setdefault(n["type"], []).append(n)
    assert len(types["artifact"]) == 6          # all six Harbor files appear, analysed or not
    assert types["candidate"] and types["evidence"]
    cov = {a["logical_path"]: a for a in g["coverage"]["artifacts"]}
    # calculations.py was not sent to extraction in build(): shown as not analysed, not hidden
    assert any(a["extraction"] == "NO_EXTRACTION" for a in cov.values())
    assert g["head"]["source_revision_id"] == ch.rev
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    ids = {n["id"] for n in g["nodes"]}
    assert tid in ids
    # file -> rule and evidence -> rule edges exist and every edge endpoint is a node
    assert any(e["type"] == "source-of" and e["to"] == tid for e in g["edges"])
    assert any(e["type"] == "evidence-of" and e["to"] == tid for e in g["edges"])
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    # the freight rule stays its own node (no auto link to a Manager rule via the shared literal)
    freight = cand_id(ch, POLICY, "free_freight_threshold")
    assert not any(e["type"] == "relation" and freight in (e["from"], e["to"]) for e in g["edges"])
    assert all(e.get("projection") is True for e in g["edges"] if e["type"] == "relation")


def test_confirm_decision_changes_state_not_source(env, ch):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    before = {n["id"]: n for n in _graph(ch.admin, env)["nodes"]}[tid]
    assert before["state"] == "pending-confirmation"
    r = ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": tid,
                                                     "rationale": "judge check"})
    assert r.status_code == 200, r.text
    g = _graph(ch.admin, env)
    after = {n["id"]: n for n in g["nodes"]}[tid]
    assert after["state"] == "confirmed"
    assert any(e["type"] == "decided-by" and e["to"] == tid for e in g["edges"])
    assert g["head"]["source_revision_id"] == ch.rev      # source revision untouched


def test_findings_edges_and_scope_mismatch_not_called_conflict(env, ch):
    pol = cand_id(ch, POLICY, "capex_finance_review_threshold")
    fin = cand_id(ch, FINGUIDE, "capex_finance_review_threshold")
    _, r = create_job(ch.admin, env, "finding", {"candidate_ids": [pol, fin]})
    assert r.status_code == 200, r.text
    drain(env)
    g = _graph(ch.admin, env)
    findings = [n for n in g["nodes"] if n["type"] == "finding"]
    for f in findings:
        if f["detail"]["kind"] == "scope_mismatch":
            assert f["state"] == "scope-mismatch"
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])


def test_requester_without_evidence_read_gets_redacted_graph(env, ch):
    g = _graph(env.req_client(), env)
    assert g["coverage"]["evidence_redacted"] is True
    assert not [n for n in g["nodes"] if n["type"] == "evidence"]
    assert all(n["detail"].get("evidence_redacted") for n in g["nodes"] if n["type"] == "candidate")


def test_denials(env, ch):
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app, base_url="https://judge.test")
    assert anon.get("/v2/knowledge/graph", params={"workspace_id": env.ws}).status_code in (401, 403)
    r = ch.admin.get("/v2/knowledge/graph", params={"workspace_id": "ws-other-nonexistent"})
    assert r.status_code in (401, 403, 404)
    assert "candidate" not in r.text


def test_explorer_shows_the_most_recently_queued_extraction_not_the_largest_job_id(env, ch, monkeypatch):
    """Found by the live trial: `knowledge_draft` ordered by the random hex job id, so a NEWER re-extraction could be hidden behind an older
    one. The newer run (smaller id on purpose) must be the one Explorer lists."""
    from infra.worker import extraction
    from tests.pitch_brief.test_loop_feedback import PostApplyExtraction
    art = ch.arts[APPROVAL]
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: PostApplyExtraction())
    new_id = "job-extraction-0000000000"
    r = env.req_client().post("/v2/jobs", json={"workspace_id": env.ws, "kind": "extraction", "job_id": new_id,
                                                "payload": {"artifact_id": art, "source_role": "unknown"}})
    assert r.status_code == 200, r.text
    drain(env)
    old_id = ch.jobs[APPROVAL]
    assert new_id < old_id, "the test needs the newer job to sort BEFORE the older one by id"
    node = {n["id"]: n for n in _graph(ch.admin, env)["nodes"]}[art]
    assert node["detail"]["extraction_job_id"] == new_id
