"""Two API instances sharing a CAS backend fixture; no claim of live Postgres."""
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api.router import Api
from app.api.sessions import SessionRegistry
from core.change_intelligence.session import InMemoryBaseline
from infra.adapters.cloud_adapters import StaleSessionVersion
from reference_system.reset.baseline import build_baseline


class Backend:
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.lose_reply = False
        self.writes = 0

    def persist_session(self, session, *, expected_version=None):
        if self.fail:
            raise RuntimeError("database unavailable")
        current = self.rows.get(session.session_id)
        version = current.durable_version if current else 0
        if expected_version is not None and expected_version != version:
            raise StaleSessionVersion("concurrent write")
        stored = copy.deepcopy(session)
        stored.orchestrator = None
        stored.durable_version = version + 1
        self.rows[session.session_id] = stored
        self.writes += 1
        if self.lose_reply:
            self.lose_reply = False
            raise RuntimeError("connection lost after commit")
        return version + 1

    def fetch_session(self, session_id, baseline):
        return copy.deepcopy(self.rows.get(session_id))


@pytest.fixture
def durable(monkeypatch):
    backend = Backend()
    baseline = InMemoryBaseline.from_artifacts(build_baseline(Path(__file__).resolve().parents[2] / "reference_system")["artifacts"])
    registry = SessionRegistry(baseline, backend=backend)
    monkeypatch.setattr(main, "registry", registry)
    monkeypatch.setattr(main, "api", Api(sessions=registry))
    return TestClient(main.app), registry, backend


def create(client):
    return client.post("/rpc", json={"operation": "session.create"}).json()["data"]["session_id"]


def command(sid, key="request-one", value=300000):
    return dict(operation="scenario.external_edit", session_id=sid, request_id=key,
                actor="judge", timestamp="2026-09-20T14:00:00Z",
                payload=dict(artifact="data/approval_matrix.xlsx", locator="Thresholds!C2", value=value))


def test_retry_after_restart_returns_committed_result_without_second_write(durable):
    client, registry, backend = durable
    sid = create(client)
    first = client.post("/rpc", json=command(sid))
    assert first.status_code == 200, first.text
    writes = backend.writes
    registry._sessions.clear()
    second = client.post("/rpc", json=command(sid))
    assert second.json() == first.json()
    assert backend.writes == writes
    assert len(backend.rows[sid].work["handled_event"]) == 1


def test_failed_commit_can_be_retried_with_same_key(durable):
    client, registry, backend = durable
    sid = create(client)
    backend.fail = True
    assert client.post("/rpc", json=command(sid)).status_code == 503
    assert sid not in registry._sessions
    backend.fail = False
    assert client.post("/rpc", json=command(sid)).status_code == 200
    assert len(backend.rows[sid].work["handled_event"]) == 1


def test_lost_commit_acknowledgement_is_safe_to_retry(durable):
    client, registry, backend = durable
    sid = create(client)
    backend.lose_reply = True
    assert client.post("/rpc", json=command(sid)).status_code == 503
    writes = backend.writes
    assert client.post("/rpc", json=command(sid)).status_code == 200
    assert backend.writes == writes


def test_request_key_is_bound_to_session_and_content(durable):
    client, registry, backend = durable
    a, b = create(client), create(client)
    assert client.post("/rpc", json=command(a)).status_code == 200
    assert client.post("/rpc", json=command(a, value=400000)).status_code == 409
    assert client.post("/rpc", json=command(b)).status_code == 200
    assert len(backend.rows[b].work["handled_event"]) == 1


def test_reads_refresh_authoritative_state_without_writing(durable):
    client, registry, backend = durable
    sid = create(client)
    other = backend.fetch_session(sid, registry.baseline)
    other.overlay.write("workflow/approval_routes.yaml", b"changed by another instance")
    backend.persist_session(other, expected_version=other.durable_version)
    writes = backend.writes
    reply = client.post("/rpc", json=dict(operation="state.observe", session_id=sid)).json()
    assert reply["data"]["changed_in_session"] == ["workflow/approval_routes.yaml"]
    assert backend.writes == writes


def test_stale_instance_cannot_overwrite_first_instance(durable):
    client, registry, backend = durable
    sid = create(client)
    a = backend.fetch_session(sid, registry.baseline)
    b = backend.fetch_session(sid, registry.baseline)
    a.put("sentinel", "a", "first")
    backend.persist_session(a, expected_version=a.durable_version)
    b.put("sentinel", "b", "second")
    with pytest.raises(StaleSessionVersion):
        backend.persist_session(b, expected_version=b.durable_version)
    assert backend.rows[sid].work["sentinel"] == {"a": "first"}
