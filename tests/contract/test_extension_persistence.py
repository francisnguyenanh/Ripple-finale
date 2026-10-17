"""Exercise the production SQL/CAS and blob codec with controlled transports."""
import copy
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from app.api.sessions import JudgeSession
from core.change_intelligence.lineage import VerifiedLineage
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from infra.adapters.cloud_adapters import CloudSQLSessionBackend, StaleSessionVersion, BaselineIntegrityError


class Cursor:
    def __init__(self, version=4):
        self.version = version
        self.calls = []
        self.rows = {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if "INSERT INTO state_store" in sql:
            for key in ("meta", "work", "lineage", "overlay"):
                if f"'{key}'" in sql:
                    self.rows[key] = json.loads(args[1])
    def fetchone(self): return (self.version,)
    def fetchall(self): return list(self.rows.items())


class Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.rolled_back = False
        self.committed = False
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return self.cur
    def rollback(self): self.rolled_back = True
    def commit(self): self.committed = True


class Bucket:
    def __init__(self): self.data = {}
    def blob(self, name):
        bucket = self
        class Blob:
            def upload_from_string(self, raw): bucket.data[name] = raw
            def download_as_bytes(self): return bucket.data[name]
        return Blob()


def backend(cur):
    store = CloudSQLSessionBackend.__new__(CloudSQLSessionBackend)
    connection = Connection(cur)
    store._get_conn = lambda: connection
    store.blob_bucket = Bucket()
    return store, connection


def session():
    baseline = InMemoryBaseline.from_artifacts({"workflow/test.yaml": b"value: 1"})
    overlay = InMemoryOverlay(baseline, "SESSION-contract")
    return JudgeSession("SESSION-contract", overlay,
        VerifiedLineage(session_id="SESSION-contract", baseline_id=baseline.baseline_id), baseline.baseline_id)


def test_production_cas_rejects_stale_version_before_writing():
    cur = Cursor(version=4)
    store, connection = backend(cur)
    with pytest.raises(StaleSessionVersion):
        store.persist_session(session(), expected_version=3)
    assert connection.rolled_back and not connection.committed
    assert not cur.rows
    assert any("FOR UPDATE" in sql for sql, _ in cur.calls)


def test_production_blob_manifest_roundtrip_and_parent_are_preserved():
    cur = Cursor(version=4)
    store, connection = backend(cur)
    original = session()
    before = original.snapshot.snapshot_id
    original.overlay.write("workflow/test.yaml", b"value: 2")
    assert store.persist_session(original, expected_version=4) == 5
    assert connection.committed
    assert "blobs" not in cur.rows["overlay"]
    assert cur.rows["overlay"]["blob_hashes"]
    cur.calls.clear()
    restored = store.fetch_session(original.session_id, original.overlay.baseline)
    assert "FOR SHARE" in cur.calls[0][0]
    assert restored.snapshot.parent_snapshot_id == before
    assert restored.snapshot.snapshot_id == original.snapshot.snapshot_id
    assert restored.overlay.read("workflow/test.yaml") == b"value: 2"
    assert restored.work == original.work
    assert restored.orchestrator is None
    key = next(iter(store.blob_bucket.data))
    store.blob_bucket.data[key] = b"corrupted"
    with pytest.raises(BaselineIntegrityError):
        store.fetch_session(original.session_id, original.overlay.baseline)


def test_production_session_ids_do_not_reuse_historical_sequence():
    store, _ = backend(Cursor())
    ids = {store.mint_session_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(s.removeprefix("SESSION-")) == 32 for s in ids)
