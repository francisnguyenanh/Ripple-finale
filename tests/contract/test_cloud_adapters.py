# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json

import pytest

from infra.adapters.cloud_adapters import (
    BaselineIntegrityError,
    GCSBaselineStore,
    CloudSQLStateStore,
    StaleSessionVersion,
    VertexAIProvider,
)
from core.change_intelligence.storage import BaselineImmutableError

def test_gcs_baseline_store_requires_bucket():
    with pytest.raises(ValueError, match="Bucket name must be provided"):
        GCSBaselineStore(bucket_name="")

def test_gcs_baseline_store_invalid_path():
    with pytest.raises(ValueError, match="Invalid prefix path"):
        GCSBaselineStore(bucket_name="my-bucket", prefix="../path")

def test_gcs_baseline_store_missing_credentials(monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError
    def mock_storage_client(*args, **kwargs):
        raise DefaultCredentialsError("Mock missing creds")
    
    monkeypatch.setattr("google.cloud.storage.Client", mock_storage_client)
    
    with pytest.raises(RuntimeError, match="GCP Credentials missing"):
        GCSBaselineStore(bucket_name="my-bucket")

def test_gcs_baseline_store_is_immutable(monkeypatch):
    monkeypatch.setattr("google.cloud.storage.Client", lambda *args, **kwargs: None)
    store = GCSBaselineStore(bucket_name="my-bucket")
    with pytest.raises(BaselineImmutableError):
        store.write("path/to/file.txt", b"data")

def test_cloud_sql_requires_connection_string():
    with pytest.raises(ValueError, match="Connection string cannot be empty"):
        CloudSQLStateStore(connection_string="")

def test_vertex_ai_requires_config():
    with pytest.raises(ValueError, match="Vertex project cannot be empty"):
        VertexAIProvider(project="", location="us-central1", model="gemini")
        
    with pytest.raises(ValueError, match="Vertex location cannot be empty"):
        VertexAIProvider(project="my-project", location="", model="gemini")
        
    with pytest.raises(ValueError, match="Vertex model cannot be empty"):
        VertexAIProvider(project="my-project", location="us-central1", model="")

def test_vertex_ai_missing_credentials(monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError
    def mock_default(*args, **kwargs):
        raise DefaultCredentialsError("Mock missing creds")

    monkeypatch.setattr("google.auth.default", mock_default)

    with pytest.raises(RuntimeError, match="Vertex credentials missing"):
        VertexAIProvider(project="p", location="l", model="m")


# --- blob integrity on read -----------------------


class _FakeBlob:
    def __init__(self, content: bytes):
        self._content = content

    def exists(self):
        return True

    def download_as_text(self):
        return self._content.decode("utf-8")

    def download_as_bytes(self):
        return self._content


class _FakeBucket:
    def __init__(self, blobs: dict):
        self._blobs = blobs

    def blob(self, name):
        return self._blobs[name]


def _fake_gcs_client(manifest: dict, artifact_bytes: dict):
    manifest_blob = _FakeBlob(json.dumps({
        "baseline_id": "BASELINE-1", "artifacts": manifest,
    }).encode("utf-8"))
    blobs = {"MANIFEST.json": manifest_blob}
    for name, content in artifact_bytes.items():
        blobs[name] = _FakeBlob(content)
    bucket = _FakeBucket(blobs)

    class _FakeClient:
        def bucket(self, name):
            return bucket

    return _FakeClient()


def test_gcs_baseline_store_detects_a_tampered_blob(monkeypatch):
    monkeypatch.setattr("google.cloud.storage.Client", lambda *a, **k: None)
    store = GCSBaselineStore(bucket_name="my-bucket")

    correct = b"original bytes"
    tampered = b"somebody changed this"
    store.client = _fake_gcs_client(
        manifest={"routes.yaml": hashlib.sha256(correct).hexdigest()},
        artifact_bytes={"routes.yaml": tampered})

    with pytest.raises(BaselineIntegrityError):
        store.read("routes.yaml")


def test_gcs_baseline_store_reads_an_intact_blob(monkeypatch):
    monkeypatch.setattr("google.cloud.storage.Client", lambda *a, **k: None)
    store = GCSBaselineStore(bucket_name="my-bucket")

    content = b"original bytes"
    store.client = _fake_gcs_client(
        manifest={"routes.yaml": hashlib.sha256(content).hexdigest()},
        artifact_bytes={"routes.yaml": content})

    assert store.read("routes.yaml") == content


# --- session id and version are CAS-guarded ------


def test_stale_session_version_is_importable_and_is_an_exception():
    assert issubclass(StaleSessionVersion, Exception)
