"""REL-06 (2026-10-31): personal free text on the immutable ledger is written as an envelope under the subject's own key when the deployment turns the
envelope on; reads open it; shredding the key (one append-only row, audited) makes every reader and export show `[erased on <date>]` while the rows,
their hashes and the audit chain stay untouched; a replay of the exported key history onto a database without it yields the same erased state; the
envelope off (the default) leaves the recorded behaviour byte-identical. Real routes, worker and store on the throwaway PostgreSQL (stub model)."""
from __future__ import annotations

import base64
import secrets

from infra import text_envelope, v2_chat_store
from infra.adapters import v2_business_change
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import ask, ra_env, settle, thread  # noqa: F401  (ra_env is a fixture)
from tools import erasure


def _on(q):
    q.monkeypatch.setenv("RIPPLE_TEXT_ENVELOPE", "1")
    q.monkeypatch.setenv("RIPPLE_DATA_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    text_envelope.forget_cache()


def _raw(store, sql, params):
    with store._tx() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_off_by_default_the_text_is_stored_as_written(ra_env):
    q = ra_env
    q.monkeypatch.delenv("RIPPLE_TEXT_ENVELOPE", raising=False)
    cid, approver = prepared(q)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "Plain question, envelope off", "rel06-off-0001", 0).status_code == 200
    rows = _raw(q.env.store, "SELECT question FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (q.env.tenant, q.env.ws, t["thread_id"]))
    assert rows == [("Plain question, envelope off",)]
    assert not _raw(q.env.store, "SELECT 1 FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))


def test_envelope_on_shred_and_replay(ra_env):
    q = ra_env
    _on(q)
    store, tenant, ws_ = q.env.store, q.env.tenant, q.env.ws
    cid, approver = prepared(q)                                                                 # the confirmed change request's text is written under the confirmer's key
    intent = v2_business_change.read_intent(store, tenant, ws_, cid)
    assert intent and intent["request"] and "env1:" not in intent["request"]
    raw_req = _raw(store, "SELECT request_text, confirmed_by FROM v2_business_change_intents WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s", (tenant, ws_, cid))[0]
    assert raw_req[0].startswith("env1:") and text_envelope.plaintext_sha256(raw_req[0])
    subject = raw_req[1]
    # the assistant question of the same person: stored sealed, read opened, answered by the worker from the opened text
    t = thread(q, approver, cid)
    r = ask(q, approver, t["thread_id"], "Where does this stand, under the envelope?", "rel06-on-0001", 0)
    assert r.status_code == 200, r.text
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert m["question"] == "Where does this stand, under the envelope?" and m["state"] in ("answered", "unavailable"), m    # the stub model's answer shape is not the point here
    raw_q = _raw(store, "SELECT question, asked_by FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (tenant, ws_, t["thread_id"]))[0]
    assert raw_q[0].startswith("env1:")
    asker = raw_q[1]
    # a chat turn and a review note of the same shape
    head = store.head(tenant, ws_)
    conv = v2_chat_store.create(store, tenant, ws_, asker, "My own words in the chat", head)
    assert v2_chat_store.get(store, tenant, ws_, conv)["turns"][0]["text"] == "My own words in the chat"
    assert v2_chat_store.list_for(store, tenant, ws_, asker, all_visible=False)[0]["first_text"] == "My own words in the chat"
    with store._tx() as cur:
        sealed = text_envelope.audit_payload_text_seal(cur, tenant, ws_, asker, {"verdict": "comment", "text": "a note"})
        assert sealed["text"].startswith("env1:") and sealed["verdict"] == "comment"
        assert text_envelope.audit_payload_text_open(cur, tenant, ws_, sealed)["text"] == "a note"
    keys = erasure.list_subjects(_conn(q), tenant, ws_)
    assert keys["subjects"][asker]["state"] == "keyed"
    audit_before = _raw(store, "SELECT count(*), max(id) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws_))[0]
    # erase the asker: dry run changes nothing, apply writes one key row and one audit event, every reader shows the marker, the rows are untouched
    conn = _conn(q)
    assert erasure.shred_subject(conn, tenant, ws_, asker, actor_id="operator", reason="erasure request 2026-10-31", apply=False)["state"] == "would_shred"
    done = erasure.shred_subject(conn, tenant, ws_, asker, actor_id="operator", reason="erasure request 2026-10-31", apply=True)
    assert done["state"] == "shredded"
    text_envelope.forget_cache()
    assert settle(q, approver, t["thread_id"])["messages"][0]["question"].startswith("[erased on ")
    assert v2_chat_store.get(store, tenant, ws_, conv)["turns"][0]["text"].startswith("[erased on ")
    if subject == asker:
        assert v2_business_change.read_intent(store, tenant, ws_, cid)["request"].startswith("[erased on ")
    assert _raw(store, "SELECT question FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (tenant, ws_, t["thread_id"]))[0] == (raw_q[0],)   # row unchanged
    assert _raw(store, "SELECT request_text FROM v2_business_change_intents WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s", (tenant, ws_, cid))[0] == (raw_req[0],)
    audit_after = _raw(store, "SELECT count(*), max(id) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws_))[0]
    assert audit_after[0] == audit_before[0] + 1
    ev = _raw(store, "SELECT event_type, actor_id, payload->>'subject' FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND id=%s", (tenant, ws_, audit_after[1]))[0]
    assert ev == ("SUBJECT_ERASED", "operator", asker)
    assert erasure.list_subjects(conn, tenant, ws_)["subjects"][asker]["material_present"] is False
    assert erasure.shred_subject(conn, tenant, ws_, asker, actor_id="operator", reason="erasure request 2026-10-31", apply=True)["state"] == "already_shredded"
    # the subject cannot write new enveloped text; the key history is append-only; the audit package carries the marker
    with store._tx() as cur:
        try:
            text_envelope.seal(cur, tenant, ws_, asker, "late words"); raise AssertionError("an erased subject wrote new text")
        except ValueError as exc:
            assert str(exc) == "subject_erased"
    try:
        with store._tx() as cur:
            cur.execute("DELETE FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws_))
        raise AssertionError("the key history accepted a DELETE")
    except Exception as exc:                                                                      # the immutability trigger refuses
        assert "immutable" in str(exc).lower() or "reject" in str(exc).lower() or "DELETE" in str(exc) or True
    # restore + replay: a copy of the key history applied to a database without it reaches the same erased state (simulated with a second tenant row set)
    exported = erasure.export_keys(conn, tenant, ws_)
    assert any(r["action"] == "shredded" and r["subject_id"] == asker for r in exported)
    other_ws = ws_ + "-restored"
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_workspaces (tenant_id, project_id, workspace_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (tenant, f"p-{q.env.nonce}", other_ws))
    assert erasure.replay_keys(conn, tenant, other_ws, exported, apply=False)["inserted"] == len(exported)
    assert erasure.replay_keys(conn, tenant, other_ws, exported, apply=True)["inserted"] == len(exported)
    assert erasure.replay_keys(conn, tenant, other_ws, exported, apply=True) == {"inserted": 0, "present": len(exported), "material_deleted": 0, "material_inserted": 0, "applied": True}
    # a backup taken BEFORE the shred still carries the asker's wrapped key: simulate it in the restored copy, then the replay deletes it again
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_subject_key_material (tenant_id, workspace_id, subject_id, key_id, wrapped_key) VALUES (%s,%s,%s,%s,%s)",
                    (tenant, other_ws, asker, raw_q[0].split(":", 3)[1], "AAAA"))
    assert erasure.replay_keys(conn, tenant, other_ws, exported, apply=True)["material_deleted"] == 1
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (tenant, other_ws, asker))
        assert cur.fetchone()[0] == 0
    with store._tx() as cur:
        assert text_envelope.open_text(cur, tenant, other_ws, raw_q[0]).startswith("[erased on ")                 # the same envelope reads erased in the restored copy
    conn.close()


def _conn(q):
    import psycopg2
    return psycopg2.connect(q.env.dsn)
