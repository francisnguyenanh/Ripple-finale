"""BFF authentication and four-step journey over the production RPC handlers."""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from infra import judge
from tests.e2e.test_two_instance_session import durable
from tests.intent_support import model_result


@pytest.fixture
def browser(monkeypatch, durable):
    backend_client, registry, backend = durable
    monkeypatch.setenv("RIPPLE_JUDGE_COOKIE_KEY", "test-signing-key-" * 3)
    monkeypatch.setenv("RIPPLE_JUDGE_ACCESS_CODE", "test-invitation-" * 3)
    def forward(body):
        response = backend_client.post("/rpc", json=body)
        return response.status_code, response.json()
    monkeypatch.setattr(judge, "_forward", forward)
    client = TestClient(judge.app, base_url="https://judge.test")
    client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    return client, registry, backend


def login(client):
    response = client.post("/login", json={"code": "test-invitation-" * 3})
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]


def rpc(client, operation, **payload):
    response = client.post("/rpc", json={"operation": operation, "payload": payload})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_four_screen_journey_uses_same_rpc(browser):
    client, registry, backend = browser
    login(client)
    assert rpc(client, "findings.list")["findings"]
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value=model_result()):
        candidate = rpc(client, "intent.parse", text="For all orders lower approval threshold to JPY 500000")
    intent = rpc(client, "intent.create", candidate_id=candidate["candidate_id"], content_hash=candidate["content_hash"])
    shadow = rpc(client, "whatif.simulate", target=candidate["semantic_id"], proposed_value=candidate["desired_value"])
    assert rpc(client, "impact.show", shadow_id=shadow["shadow_id"])["direct"]
    cs = rpc(client, "changeset.prepare", intent_id=intent["intent_id"], shadow_id=shadow["shadow_id"])
    assert rpc(client, "changeset.review", changeset_id=cs["changeset_id"])["text"]
    rpc(client, "changeset.decide", changeset_id=cs["changeset_id"], decision="approve", reason="reviewed evidence")
    assert rpc(client, "changeset.apply", changeset_id=cs["changeset_id"])["written"]
    result = rpc(client, "verification.run", changeset_id=cs["changeset_id"])
    assert result["passed"] and result["lineage_version"] == 1
    result = rpc(client, "scenario.external_edit", artifact="data/approval_matrix.xlsx", locator="Thresholds!C2", value=300000)
    assert result["alignment"] in ("DRIFT", "CONFLICT")
    assert rpc(client, "lineage.show")["version"] == 1


def test_cookie_auth_and_origin_fail_closed(browser):
    client, _, _ = browser
    assert client.post("/rpc", json={"operation": "state.observe"}).status_code == 401
    assert client.post("/login", json={"code": "wrong"}).status_code == 401
    login(client)
    assert client.post("/rpc", json={"operation": "session.create"}).status_code == 403
    assert client.post("/rpc", headers={"Origin": "https://attacker.test"}, json={"operation": "state.observe"}).status_code == 403
    client.cookies.clear()
    client.cookies.set("ripple_judge", "SESSION-other|actor|9999999999|forged")
    assert client.post("/rpc", json={"operation": "state.observe"}).status_code == 401


def test_browser_cannot_select_another_session_or_actor(browser):
    client, registry, backend = browser
    login(client)
    mine = rpc(client, "state.observe")["session_id"]
    response = client.post("/rpc", json={"operation": "state.observe", "session_id": "someone-else", "actor": "admin"})
    assert response.json()["data"]["session_id"] == mine


def test_model_budget_survives_reset_and_restart(browser):
    client, registry, backend = browser
    login(client)
    sid = rpc(client, "state.observe")["session_id"]
    backend.rows[sid].work["judge_usage"] = {"commands": 10, "model_calls": 10}
    rpc(client, "session.reset")
    registry._sessions.clear()
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt") as model:
        response = client.post("/rpc", json={"operation": "intent.parse", "payload": {"text": "all orders JPY 500000"}})
    assert response.status_code == 429
    model.assert_not_called()


def test_model_failure_consumes_attempt_budget(browser):
    client, _, backend = browser
    login(client)
    sid = rpc(client, "state.observe")["session_id"]
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", side_effect=TimeoutError):
        reply = client.post("/rpc", json={"operation": "intent.parse", "payload": {"text": "all orders JPY 500000"}})
    assert reply.status_code == 200 and reply.json()["data"]["abstained"] is True
    assert backend.rows[sid].work["judge_usage"]["model_calls"] == 1
    assert not backend.rows[sid].listing("candidate")
    assert backend.rows[sid].listing("gap") == (reply.json()["data"]["gap"]["gap_id"],)


def test_session_ttl_is_enforced_by_backend(browser):
    client, _, backend = browser
    login(client)
    sid = rpc(client, "state.observe")["session_id"]
    backend.rows[sid].created_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert client.post("/rpc", json={"operation": "state.observe"}).status_code == 410


def test_page_assets_are_local_and_have_no_cloud_credentials(browser):
    client, _, _ = browser
    page = client.get("/")
    assert page.status_code == 200 and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    script = client.get("/judge.js").text
    assert "Authorization" not in script and "googleapis" not in script
    assert "textContent" in script and "innerHTML" not in script



def test_single_service_runs_rpc_in_process_and_hides_raw_rpc(monkeypatch, durable):
    """ADR-004: the one public app runs /rpc in-process, only behind the cookie."""
    _, _, backend = durable
    monkeypatch.setenv("RIPPLE_JUDGE_COOKIE_KEY", "test-signing-key-" * 3)
    monkeypatch.setenv("RIPPLE_JUDGE_ACCESS_CODE", "test-invitation-" * 3)
    client = TestClient(judge.app, base_url="https://judge.test")
    client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert client.post("/rpc", json={"operation": "session.create"}).status_code == 401
    login(client)
    sid = rpc(client, "state.observe")["session_id"]
    assert sid in backend.rows
    assert client.get("/health").json()["status"] == "ok"


def test_secret_with_trailing_newline_still_accepts_the_typed_code(browser, monkeypatch):
    client, _, _ = browser
    monkeypatch.setenv("RIPPLE_JUDGE_ACCESS_CODE", "test-invitation-" * 3 + "\r\r\n")
    login(client)


def test_quota_limits_are_configurable_by_environment(browser, monkeypatch):
    client, _, backend = browser
    login(client)
    sid = rpc(client, "state.observe")["session_id"]
    backend.rows[sid].work["judge_usage"] = {"commands": 10, "model_calls": 10}
    monkeypatch.setenv("RIPPLE_JUDGE_MAX_MODEL_CALLS", "20")
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value=model_result()):
        assert client.post("/rpc", json={"operation": "intent.parse",
            "payload": {"text": "For all orders lower approval threshold to JPY 500000"}}).status_code == 200
    monkeypatch.setenv("RIPPLE_JUDGE_MAX_COMMANDS", "11")
    assert client.post("/rpc", json={"operation": "session.reset"}).status_code == 429


def test_favicon_is_an_empty_success_not_a_404(browser):
    client, _, _ = browser
    assert client.get("/favicon.ico").status_code == 204
