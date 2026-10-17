"""Opt-in production CAS probe against a disposable PostgreSQL database.

Set RIPPLE_TEST_POSTGRES_DSN, then run python -m tests.live_extension_postgres.
This creates the production schema and isolated test sessions in that database.
SQL is real; the GCS transport is an in-memory fixture, explicitly not live GCS.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.api.sessions import JudgeSession
from core.change_intelligence.lineage import VerifiedLineage
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from infra.adapters.cloud_adapters import CloudSQLSessionBackend, StaleSessionVersion
from tests.contract.test_extension_persistence import Bucket


def main():
    dsn = os.environ.get("RIPPLE_TEST_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("Set RIPPLE_TEST_POSTGRES_DSN to a disposable test database; no probe was run.")
    bucket = Bucket()
    first = CloudSQLSessionBackend(dsn, blob_bucket=bucket)
    second = CloudSQLSessionBackend(dsn, blob_bucket=bucket)
    baseline = InMemoryBaseline.from_artifacts({"workflow/probe.yaml": b"value: 1"})
    sid = "EXT-PROBE-" + uuid4().hex
    original = JudgeSession(sid, InMemoryOverlay(baseline, sid),
        VerifiedLineage(session_id=sid, baseline_id=baseline.baseline_id), baseline.baseline_id)
    try:
        first.persist_session(original, expected_version=0)
        a, b = first.fetch_session(sid, baseline), second.fetch_session(sid, baseline)
        def write(store, session, value):
            session.overlay.write("workflow/probe.yaml", f"value: {value}".encode())
            try:
                store.persist_session(session, expected_version=session.durable_version)
                return "committed"
            except StaleSessionVersion:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, first, a, 2), pool.submit(write, second, b, 3)]
            assert sorted(f.result() for f in futures) == ["committed", "stale"]
        restored = second.fetch_session(sid, baseline)
        assert restored.durable_version == 2
        assert restored.snapshot.parent_snapshot_id == original.snapshot.snapshot_id
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: first.mint_session_id(), range(100)))
        assert len(set(ids)) == 100
        print("PASS: real PostgreSQL CAS race, parent/blob round-trip, 100 unique concurrent ids")
    finally:
        with first._get_conn() as connection:
            with connection.cursor() as cursor:
                for table in ("state_store", "events_store", "session_versions"):
                    cursor.execute(f"DELETE FROM {table} WHERE session_id = %s", (sid,))
            connection.commit()


if __name__ == "__main__":
    main()
