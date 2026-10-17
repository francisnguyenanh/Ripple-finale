# -*- coding: utf-8 -*-
"""A command is not successful until its durable commit is.

`app/main.py` mutated the session, then persisted; a
persistence exception was logged as a warning and the response still
reported the pre-persist `reply.ok`. A judge could be told an approval
succeeded that will not survive a restart.

Mutation #12 (SS17): swallow the persistence exception and return `ok` ->
this suite must go red.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


class _FailingBackend:
    """A stand-in multi-instance backend whose commit always fails."""

    def persist_session(self, session, *, expected_version=None):
        raise RuntimeError("Cloud SQL is unreachable")


@pytest.fixture
def failing_backend():
    original = main.registry.backend
    main.registry.backend = _FailingBackend()
    yield
    main.registry.backend = original


def _new_session():
    created = client.post("/rpc", json={"operation": "session.create"})
    return created.json()["data"]["session_id"]


def test_a_persistence_failure_never_returns_ok(failing_backend):
    main.registry.backend = None
    session_id = _new_session()
    main.registry.backend = _FailingBackend()
    response = client.post("/rpc", json={
        "operation": "session.reset", "session_id": session_id,
        "request_id": "req-durable-1"})

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False, (
        "the durable commit failed but the response still claimed success; "
        "a judge acting on this response would believe a mutation "
        "survived that will vanish the moment this instance restarts")
    assert body["code"] == "persistence_failed"


def test_a_persistence_failure_discards_the_mutated_cache(failing_backend):
    main.registry.backend = None
    session_id = _new_session()
    main.registry.backend = _FailingBackend()
    client.post("/rpc", json={
        "operation": "session.reset", "session_id": session_id,
        "request_id": "req-durable-2"})

    assert session_id not in main.registry._sessions, (
        "the in-process cache still holds a mutation nobody outside this "
        "instance can see, and nothing will ever retry writing it")


def test_failed_response_is_not_cached_as_a_permanent_result(failing_backend):
    # Failure was not committed. There is no success result to deduplicate;
    # without a recoverable backend the next call correctly cannot find it.
    main.registry.backend = None
    session_id = _new_session()
    main.registry.backend = _FailingBackend()
    payload = {"operation": "session.reset", "session_id": session_id,
               "request_id": "req-durable-3"}
    assert client.post("/rpc", json=payload).status_code == 503
    main.registry.backend = None
    assert client.post("/rpc", json=payload).json()["code"] == "unknown_session"


def test_a_successful_write_still_persists_and_reports_ok():
    session_id = _new_session()
    response = client.post("/rpc", json={
        "operation": "session.reset", "session_id": session_id,
        "request_id": "req-durable-4"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
