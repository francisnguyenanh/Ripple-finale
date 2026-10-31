"""AH-01 residual (2026-10-30): the four `db_route`-converted routes that had no HTTP test are reached through the real application and keep their guards:
restore approve, chat cancel, interview refresh and work-proof result. Anonymous callers are refused, a signed-in viewer is refused by permission or finds
nothing to act on, a missing or malformed body is a client error, never a server error (the conversion to the bounded database admission holds)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from infra import judge
from tests.final_loop.test_chg04_review import login
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)

ROUTES = [
    ("/v2/restore/approve", {"workspace_id": "{ws}", "target_revision_id": "rev-does-not-exist", "expected_preview_digest": "0" * 64, "reason": "ah01"}),   # a complete body, so the permission check is reached
    ("/v2/chat/conv-does-not-exist/cancel", {"workspace_id": "{ws}"}),
    ("/v2/business/interviews/sess-does-not-exist/refresh", {"workspace_id": "{ws}"}),
    ("/v2/changes/job-does-not-exist/work-proof", {"workspace_id": "{ws}", "work_index": 0, "evidence_ref": "ah01"}),   # a complete body, so the permission check is reached
]


def _anon():
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    return c


def test_anonymous_and_viewer_callers_are_refused_and_malformed_bodies_are_client_errors(qa_env):
    q = qa_env
    viewer = login(q, f"viewer-ah01-{q.env.nonce}", ("viewer",))
    anon = _anon()
    for path, body in ROUTES:
        body = {k: (v.replace("{ws}", q.env.ws) if isinstance(v, str) else v) for k, v in body.items()}
        r = anon.post(path, json=body)
        assert r.status_code in (400, 401, 403) and "lines" not in r.text, (path, r.status_code, r.text[:120])   # refused or a client error; never served, never 5xx
        r = viewer.post(path, json=body)
        assert r.status_code in (403, 404, 409), (path, r.status_code, r.text[:160])
        assert r.status_code < 500
        r = viewer.post(path, json={})
        assert 400 <= r.status_code < 500, (path, r.status_code, r.text[:160])
        r = viewer.post(path, content=b"not json", headers={"Content-Type": "application/json"})
        assert 400 <= r.status_code < 500, (path, r.status_code, r.text[:160])


CHAT_ROUTES = ["/v2/chat/start", "/v2/chat/conv-does-not-exist/message", "/v2/chat/conv-does-not-exist/allow", "/v2/chat/conv-does-not-exist/edit",
               "/v2/chat/conv-does-not-exist/confirm", "/v2/chat/conv-does-not-exist/cancel", "/v2/chat/conv-does-not-exist/revoke",
               "/v2/chat/conv-does-not-exist/reopen", "/v2/chat/conv-does-not-exist/executed"]


def test_every_chat_route_without_a_workspace_is_a_client_error_never_a_server_error(qa_env):
    """Found in the C18 image run (2026-10-31): a signed-in caller posting a body without `workspace_id` to the chat cancel route reached the store with
    no tenant (KeyError -> 500). The shared guard now answers 400 before any identity or store work, on every chat route, for an empty body, a blank
    workspace and a non-string workspace alike; a signed-in viewer with a real workspace is still refused by permission, never served, never 5xx."""
    q = qa_env
    viewer = login(q, f"viewer-ah01-chat-{q.env.nonce}", ("viewer",))
    for path in CHAT_ROUTES:
        for body in ({}, {"workspace_id": ""}, {"workspace_id": "   "}, {"workspace_id": 7}, {"text": "hello"}):
            r = viewer.post(path, json=body)
            assert r.status_code == 400 and "workspace_id required" in r.text, (path, body, r.status_code, r.text[:160])
        r = viewer.post(path, json={"workspace_id": q.env.ws, "text": "hello"})
        assert r.status_code in (400, 403, 404, 409), (path, r.status_code, r.text[:160])
        r = _anon().post(path, json={"workspace_id": q.env.ws, "text": "hello"})
        assert r.status_code in (400, 401, 403), (path, r.status_code, r.text[:160])
