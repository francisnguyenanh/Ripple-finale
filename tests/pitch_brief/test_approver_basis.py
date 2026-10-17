"""What an APPROVER can see before approving, through the real API, without evidence.read (raw source quotes stay hidden).
The decision basis is: exact before/after, source revision, review (checks/blockers), the independent check (contract), simulation and its
limits, reviewer notes. The page (DecisionBasis) is built only from these; this test pins that the API gives them to the approver and
nothing more."""
from __future__ import annotations

import json

import pytest

from tests.pitch_brief.chain import build
from tests.pitch_brief.test_draft_to_proposal import _to_approval, chain_to_proposal
from tests.pitch_brief.test_role_model import member, note


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_the_approver_gets_the_whole_basis_and_no_raw_quote(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c)
    rv = member(env, "reviewer")
    note(env, rv, c.proposal, "comment", "Matches the request.")
    ws = {"workspace_id": env.ws}
    # 1 before/after, exact
    diff = op.get(f"/v2/proposals/{c.proposal}/diff", params=ws).json()
    kinds = {l["kind"] for l in diff["lines"]}
    assert {"add", "remove"} <= kinds and diff["logical_path"].endswith("approval.yaml") and not diff["is_no_op"]
    prop = op.get(f"/v2/jobs/{c.proposal}", params=ws).json()["execution_result"]
    assert [(e["path"], e["before"], e["after"]) for e in prop["edits"]] == [("routing_rules.standard_manager_threshold_jpy", 1000000, 500000)]
    # 2 source and revision the bundle was built on
    assert appr["snapshot"]["source_revision_id"] == env.store.head(env.tenant, env.ws).source_revision_id
    assert op.get(f"/v2/sources/revisions/{appr['snapshot']['source_revision_id']}", params=ws).status_code == 200
    # 3 review, 4 independent check, 5 simulation and its limits, 6 reviewer notes
    assert review["readiness"] == "READY_FOR_HUMAN_REVIEW" and appr["readiness"] == "READY" and appr["blocked_reasons"] == []
    assert op.get("/v2/verification/contracts", params={**ws, "proposal_job_id": c.proposal}).json()["contracts"][0]["expected"] == 500000
    sim = op.get(f"/v2/jobs/{c.simulation}", params=ws).json()["execution_result"]
    assert {"status", "reason", "coverage"} <= set(sim)
    notes = op.get(f"/v2/changes/{c.proposal}/review-notes", params=ws).json()
    assert [n["verdict"] for n in notes["notes"]] == ["comment"] and notes["open_change_requests"] == []
    # redaction is unchanged: no original quote in anything the approver read, and no evidence.read
    quotes = [e["quote"] for cs in ch.cands.values() for cand in cs for e in cand.get("evidence", []) if len(e.get("quote", "")) > 25]
    assert quotes
    blob = json.dumps([appr, review, sim])      # the diff legitimately shows the file's own lines; the bundle, review and simulation carry no source quote
    assert not [q for q in quotes if q in blob]
    from infra.adapters.v2_policy import permissions_for
    assert "evidence.read" not in permissions_for(("change_approver",))
