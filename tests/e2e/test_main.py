# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_ready_endpoint():
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_rpc_endpoint_requires_post():
    response = client.get("/rpc")
    assert response.status_code == 405

def test_rpc_endpoint_handles_unknown_operation():
    response = client.post("/rpc", json={"operation": "unknown.op", "session_id": "SESSION-1234"})
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_operation"
    assert response.json()["ok"] is False

def test_rpc_endpoint_creates_session():
    response = client.post("/rpc", json={"operation": "session.create"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "session_id" in response.json()["data"]
    
def test_rpc_observability_included_in_response():
    response = client.post("/rpc", json={"operation": "session.create", "request_id": "req-123"})
    assert response.status_code == 200
    assert response.json()["request_id"] == "req-123"
    assert response.json()["operation"] == "session.create"
