"""R4: feedback continues a durable change cycle. A draft can be REVISED (new draft job that names the one it revises); the server
keeps the lineage, reports the work done for older drafts as out of date, and refuses to build a proposal from, or apply an approval
of, a draft that has been revised. Lineage is read from stored jobs, so it survives a refresh or another browser. Stub provider only."""
from __future__ import annotations

import pytest

from infra.worker.change_draft import validate_payload
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import APPROVAL, _to_approval, chain_to_proposal


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _revise(client, env, intent, previous, expect=200):
    jid, r = create_job(client, env, "change.draft", {"intent_id": intent, "revises_draft_job_id": previous})
    assert r.status_code == expect, r.text
    return jid, r


def _lineage(client, env, draft):
    r = client.get("/v2/change/draft-lineage", params={"workspace_id": env.ws, "draft_job_id": draft})
    assert r.status_code == 200, r.text
    return r.json()


def test_payload_contract_accepts_only_a_string_revision_reference():
    assert validate_payload({"intent_id": "i", "revises_draft_job_id": "d"}) is None
    assert validate_payload({"intent_id": "i", "revises_draft_job_id": 5}) == "revises_draft_job_id_invalid"
    assert validate_payload({"intent_id": "i", "revises_draft_job_id": ""}) == "revises_draft_job_id_invalid"


def test_a_revision_links_to_its_draft_and_marks_older_downstream_work_out_of_date(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    before = _lineage(ch.admin, env, c.draft)
    assert before["revised_by"] is None and before["is_latest"] is True and before["stale_downstream"] == []
    new, _ = _revise(c.req, env, c.intent, c.draft)
    old = _lineage(env.admin_client(), env, c.draft)         # read from stored jobs by another session
    assert old["revised_by"] == new and old["is_latest"] is False
    assert any(x["job_id"] == c.proposal and x["kind"] == "proposal" for x in old["stale_downstream"])
    cur = _lineage(ch.admin, env, new)
    assert cur["revises"] == c.draft and cur["is_latest"] is True and cur["older_drafts"] == [c.draft]


def test_the_same_draft_cannot_be_revised_twice_a_foreign_request_or_unknown_draft_is_refused(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    _revise(c.req, env, c.intent, c.draft)
    _revise(c.req, env, c.intent, c.draft, expect=409)
    _revise(c.req, env, c.intent, "job-nope", expect=400)
    _revise(c.req, env, "some-other-intent", c.draft, expect=400)
    assert ch.admin.get("/v2/change/draft-lineage", params={"workspace_id": env.ws, "draft_job_id": "job-nope"}).status_code == 404


def test_a_proposal_cannot_be_built_from_a_draft_that_was_revised(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    prev = job_body(ch.admin, env, c.proposal)["payload"]
    _revise(c.req, env, c.intent, c.draft)
    pj, r = create_job(c.req, env, "proposal", {k: prev[k] for k in ("intent_id", "impact_job_id", "simulation_job_id", "target_artifact_id", "edit_kind", "draft_job_id")})
    assert r.status_code == 409 and "revised" in r.text


def test_an_approval_made_for_a_revised_draft_cannot_be_applied(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    _op, _review, aj, appr = _to_approval(env, ch, c)
    _revise(c.req, env, c.intent, c.draft)
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 409 and "fresh exact approval" in r.text
