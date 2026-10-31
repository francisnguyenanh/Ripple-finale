"""REL-06 review findings (2026-10-31), reproduced before the fix and kept as the regression guard afterwards.

1. After a shred, no recoverable copy of the subject key may remain in the database (cryptographic erasure of the key material): the history
   row of the key's creation must not carry a wrapped key that the deployment key can still unwrap.
2. A reader that opened the key before the shred (a running web or worker process with the key in memory) must not return plaintext on any
   read that starts after the shred committed, through the product's own callers.
3. The shred of one process must take effect in every other process (no process-local cache decides).
Also: two concurrent first writes of one subject create one key; a rolled-back seal leaves no key row; shred is idempotent; an operational
error (missing deployment key, undecryptable blob) is reported as such, never as an erasure."""
from __future__ import annotations

import base64
import secrets
import threading

import psycopg2
import pytest

from infra import text_envelope
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import ask, ra_env, settle, thread  # noqa: F401  (ra_env is a fixture)
from tools import erasure


def _on(q):
    q.monkeypatch.setenv("RIPPLE_TEXT_ENVELOPE", "1")
    q.monkeypatch.setenv("RIPPLE_DATA_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    text_envelope.forget_cache()


def _asked(q):
    """One sealed question of the approver through the product routes -> (thread id, asker id, raw stored value)."""
    cid, approver = prepared(q)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "My words under the envelope", "rel06-f-0001", 0).status_code == 200
    with q.env.store._tx() as cur:
        cur.execute("SELECT question, asked_by FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (q.env.tenant, q.env.ws, t["thread_id"]))
        raw, asker = cur.fetchone()
    assert raw.startswith("env1:")
    return t["thread_id"], approver, asker, raw


def _other_process_shred(dsn, tenant, ws_, subject):
    """Process B: a separate operating-system process running the operator tool (its own connection and commit); process A keeps whatever it cached."""
    import json
    import os
    import subprocess
    import sys
    env = dict(os.environ, RIPPLE_V2_DSN=dsn)
    out = subprocess.run([sys.executable, "-m", "tools.erasure", "shred", "--tenant", tenant, "--workspace", ws_, "--subject", subject,
                          "--reason", "erasure request from another process", "--apply"], capture_output=True, text=True, env=env, check=True)
    return json.loads(out.stdout)


def test_no_recoverable_key_material_remains_after_a_shred(ra_env):
    q = ra_env
    _on(q)
    _tid, _approver, asker, raw = _asked(q)
    key_id = raw.split(":", 3)[1]
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (q.env.tenant, q.env.ws, key_id))
        assert cur.fetchone()[0] == 1                                                     # the material lives in the mutable key store
    assert _other_process_shred(q.env.dsn, q.env.tenant, q.env.ws, asker)["state"] == "shredded"
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (q.env.tenant, q.env.ws, key_id))
        assert cur.fetchone()[0] == 0                                                     # finding 1: nothing the deployment key can unwrap
        cur.execute("SELECT wrapped_key FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (q.env.tenant, q.env.ws, key_id))
        assert all(r[0] == "" for r in cur.fetchall())                                   # the append-only history carries no key material at all
        cur.execute("SELECT count(*) FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s AND action='shredded'", (q.env.tenant, q.env.ws, key_id))
        assert cur.fetchone()[0] == 1


def test_a_process_that_cached_the_key_stops_returning_plaintext_once_another_process_shredded(ra_env):
    q = ra_env
    _on(q)
    tid, approver, asker, raw = _asked(q)
    assert settle(q, approver, tid)["messages"][0]["question"] == "My words under the envelope"     # process A has read (and may have cached) the key
    assert _other_process_shred(q.env.dsn, q.env.tenant, q.env.ws, asker)["state"] == "shredded"     # process B shreds and commits; nothing tells A
    got = settle(q, approver, tid)["messages"][0]["question"]                                       # a new read in A through the product route
    assert got.startswith("[erased on "), got                                                       # findings 2 and 3
    with q.env.store._tx() as cur:                                                                  # every other reader of the same value too
        assert text_envelope.open_text(cur, q.env.tenant, q.env.ws, raw).startswith("[erased on ")
    conn = psycopg2.connect(q.env.dsn)                                                              # the worker's own path (history for the model)
    try:
        from infra.worker import request_assistant as ra_worker
        with conn.cursor() as cur:
            cur.execute("SELECT question FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (q.env.tenant, q.env.ws, tid))
            assert text_envelope.open_text(cur, q.env.tenant, q.env.ws, cur.fetchone()[0]).startswith("[erased on ")
        assert ra_worker is not None
    finally:
        conn.close()


def test_concurrent_first_writes_create_one_key_and_a_rolled_back_seal_leaves_nothing(ra_env):
    q = ra_env
    _on(q)
    subject = f"acct-concurrent-{q.env.nonce}"
    results, errors = [], []

    def writer(i):
        conn = psycopg2.connect(q.env.dsn)
        try:
            with conn.cursor() as cur:
                results.append(text_envelope.seal(cur, q.env.tenant, q.env.ws, subject, f"text {i}"))
            conn.commit()
        except Exception as exc:                                                                    # noqa: BLE001
            errors.append(repr(exc))
        finally:
            conn.close()
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    key_ids = {r.split(":", 3)[1] for r in results}
    assert len(results) == 6 and len(key_ids) == 1                                                  # one key per subject, however many writers
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s AND action='created'", (q.env.tenant, q.env.ws, subject))
        assert cur.fetchone()[0] == 1
        for r in results:
            assert text_envelope.open_text(cur, q.env.tenant, q.env.ws, r).startswith("text ")
    # a seal whose transaction rolls back leaves no key row and no material
    other = f"acct-rolled-back-{q.env.nonce}"
    conn = psycopg2.connect(q.env.dsn)
    try:
        with conn.cursor() as cur:
            sealed = text_envelope.seal(cur, q.env.tenant, q.env.ws, other, "never committed")
            assert sealed.startswith("env1:")
        conn.rollback()
    finally:
        conn.close()
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (q.env.tenant, q.env.ws, other))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (q.env.tenant, q.env.ws, other))
        assert cur.fetchone()[0] == 0
    # shred is idempotent and a second concurrent shred is one row
    conn = psycopg2.connect(q.env.dsn)
    try:
        a = erasure.shred_subject(conn, q.env.tenant, q.env.ws, subject, actor_id="operator", reason="idempotency check", apply=True)
        b = erasure.shred_subject(conn, q.env.tenant, q.env.ws, subject, actor_id="operator", reason="idempotency check", apply=True)
        assert a["state"] == "shredded" and b["state"] == "already_shredded"
    finally:
        conn.close()
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s AND action='shredded'", (q.env.tenant, q.env.ws, subject))
        assert cur.fetchone()[0] == 1


def test_operational_errors_are_reported_as_such_not_as_erasure(ra_env):
    q = ra_env
    _on(q)
    subject = f"acct-errors-{q.env.nonce}"
    with q.env.store._tx() as cur:
        sealed = text_envelope.seal(cur, q.env.tenant, q.env.ws, subject, "readable while configured")
    # a blob that does not decrypt under its key: diagnosed, not "[erased]"
    head, blob = sealed.rsplit(":", 1)
    corrupt = head + ":" + base64.b64encode(b"\x00" * 40).decode()
    with q.env.store._tx() as cur:
        got = text_envelope.open_text(cur, q.env.tenant, q.env.ws, corrupt)
    assert got.startswith("[unreadable:") and "erased" not in got
    # an envelope whose key id is unknown to this database (restored without its key history): diagnosed
    unknown = sealed.replace(sealed.split(":", 3)[1], "sk-0000000000000000", 1)
    with q.env.store._tx() as cur:
        got = text_envelope.open_text(cur, q.env.tenant, q.env.ws, unknown)
    assert got.startswith("[unreadable:") and "key_unknown" in got
    # the deployment key missing or wrong is a configuration error raised to the operator, never a quiet marker
    q.monkeypatch.setenv("RIPPLE_DATA_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    text_envelope.forget_cache()
    with q.env.store._tx() as cur:
        with pytest.raises(RuntimeError, match="deployment key"):
            text_envelope.open_text(cur, q.env.tenant, q.env.ws, sealed)
    q.monkeypatch.delenv("RIPPLE_DATA_KEY")
    with q.env.store._tx() as cur:
        with pytest.raises(RuntimeError, match="RIPPLE_DATA_KEY"):
            text_envelope.open_text(cur, q.env.tenant, q.env.ws, sealed)
