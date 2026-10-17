"""`GET /v2/work/chain`: a page opened with only part of a link finds its own stages again from the server. Same right as reading the proposal (project.read); it only names job ids of
the chain behind ONE proposal; nothing from a source; another workspace and anonymous callers learn nothing. Real caller, stub model."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import build
from tests.pitch_brief.test_draft_to_proposal import _to_approval, chain_to_proposal


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_the_chain_of_a_proposal_is_resolved_from_the_server_for_every_signed_in_role(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    r = ch.admin.get("/v2/work/chain", params={"workspace_id": env.ws, "proposal_job_id": c.proposal})
    assert r.status_code == 200 and r.json()["params"] == {"intent": c.intent, "draft": c.draft, "impact": c.impact, "simulation": c.simulation, "proposal": c.proposal}
    op, review, aj, appr = _to_approval(env, ch, c)
    for who in (c.req, ch.admin, op):
        p = who.get("/v2/work/chain", params={"workspace_id": env.ws, "proposal_job_id": c.proposal}).json()["params"]
        assert p["approval"] == aj and p["review"] and p["proposal"] == c.proposal and "apply" not in p          # only what exists; nothing invented
    assert c.req.get("/v2/work/chain", params={"workspace_id": env.ws, "proposal_job_id": aj}).status_code == 400   # not a proposal job: refused, nothing named
    assert c.req.get("/v2/work/chain", params={"workspace_id": env.ws, "proposal_job_id": "no-such-job"}).status_code in (400, 404)
    assert c.req.get("/v2/work/chain", params={"workspace_id": "ws-not-mine", "proposal_job_id": c.proposal}).status_code in (403, 404)


def test_an_anonymous_caller_gets_401_and_learns_nothing(env, ch, monkeypatch):
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app, base_url="https://judge.test")
    r = anon.get("/v2/work/chain", params={"workspace_id": env.ws, "proposal_job_id": "x"})
    assert r.status_code == 401 and "params" not in r.text
