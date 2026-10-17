"""Server-side record of a replaced request: the old/new relation and the out-of-date downstream work survive a reload,
the back button and another person opening the link (no page memory). Stub provider only."""
from __future__ import annotations

import pytest

from tests.pitch_brief.conftest import create_job
from tests.pitch_brief.chain import build
from tests.pitch_brief.test_intent_chat import READY, Interp, allow, api, settle, start, use  # noqa: F401


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _confirmed_request(env, ch, req, adm, model_text):
    c = start(env, req, model_text)
    got = settle(env, req, c["conversation_id"])
    assert got["understanding"]["status"] == "READY", got["understanding"]
    r = api(req, "POST", f"/v2/chat/{c['conversation_id']}/confirm", env, version=got["version"])
    assert r.status_code == 200, r.text
    return c["conversation_id"], r.json()["intent_id"]


def test_replacing_a_request_records_old_new_and_the_stale_downstream_on_the_server(env, ch, use):
    use(Interp(READY, READY))
    req, adm = env.req_client(), ch.admin
    cid, first = _confirmed_request(env, ch, req, adm, "I want to lower the manager approval limit to 500,000 yen")
    assert adm.post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": first}).status_code == 200
    jid, r = create_job(adm, env, "change.draft", {"intent_id": first})
    assert r.status_code == 200, r.text
    # nothing is superseded yet
    before = api(req, "GET", f"/v2/intent/{first}/lineage", env).json()
    assert before["superseded_by"] is None and before["stale_jobs"] == []
    # the person changes the confirmed request and confirms a NEW understanding
    assert api(req, "POST", f"/v2/chat/{cid}/reopen", env).status_code == 200
    assert api(req, "POST", f"/v2/chat/{cid}/message", env, text="Actually make it 500,000 yen for standard and emergency orders").status_code == 200
    got = settle(env, req, cid)
    r = api(req, "POST", f"/v2/chat/{cid}/confirm", env, version=got["version"])
    assert r.status_code == 200, r.text
    second = r.json()["intent_id"]
    assert second != first
    # any account that can read the workspace sees the same relation, from the server
    for client in (req, adm, env.admin_client()):
        old = api(client, "GET", f"/v2/intent/{first}/lineage", env).json()
        new = api(client, "GET", f"/v2/intent/{second}/lineage", env).json()
        assert old["superseded_by"] == second and old["conversation_id"] == cid
        assert [s["job_id"] for s in old["stale_jobs"]] == [jid] and old["stale_jobs"][0]["kind"] == "change.draft"
        assert old["applied"] is False
        assert new["supersedes"] == first and new["superseded_by"] is None and new["stale_jobs"] == []
        assert [s["job_id"] for s in new["replaced_work"]] == [jid] and new["replaced_applied"] is False


def test_lineage_is_scoped_to_the_workspace_and_needs_a_session(env, ch):
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app)
    r = anon.get(f"/v2/intent/intent-x/lineage", params={"workspace_id": env.ws}, headers={"X-Ripple-Judge": "1"})
    assert r.status_code in (401, 403)
    got = api(ch.admin, "GET", "/v2/intent/intent-unknown/lineage", env)
    assert got.status_code == 200 and got.json()["superseded_by"] is None       # unknown id: nothing is invented
