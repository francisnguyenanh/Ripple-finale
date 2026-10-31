"""Personal free text on the immutable ledger (REL-06, 2026-10-31; review fix the same day): envelopes and erasure by key shredding.

The ledger tables never update or delete a row (`v2_reject_immutable_change`), so a person's own words that live inside them (a question to the
request assistant, a chat turn, the text of a change request, a review note) cannot be removed by deleting the row. Instead, when the deployment
turns the envelope on (`RIPPLE_TEXT_ENVELOPE=1` with a deployment key in `RIPPLE_DATA_KEY`), each such text is written as an envelope:

    env1:<key_id>:<sha256 of the plaintext>:<base64(nonce || AES-256-GCM ciphertext)>

encrypted with the data subject's own key (one key per account per workspace, generated on first use under a per-subject advisory lock).
Two tables hold the key side:
  * `v2_subject_key_material` (mutable): the subject key wrapped by the deployment key, one row per key. The shred DELETEs it.
  * `v2_subject_keys` (append-only): the history (created / shredded rows, who, why, when) without any key material. It is the auditable
    record and the replayable tombstone list.

Two guarantees, stated separately:
  (a) logical erasure: once the shred has committed, every read that starts afterwards, in every process, opens the envelope as
      `[erased on <date>]` (there is no process cache; each open consults the key store in the reader's own transaction);
  (b) cryptographic erasure of the key material held by this database: after the shred no copy of the wrapped subject key exists in it, so the
      deployment key alone cannot recover the plaintext. It does NOT cover copies outside this database: a backup taken before the shred still
      holds the wrapped key, and a restore of such a backup followed by the key-history replay (`tools/erasure.py replay`) deletes it again.
      The deployment key itself is not rotated by a shred. Model outputs derived from the text (an answer that quotes a question, an
      understanding's `user_quote`) are not envelopes and are outside this scope (listed by `tools/erasure.py list` as derived copies).

Reading opens the envelope on the way out. An operational problem is reported as such, never as an erasure: a missing or wrong deployment key
raises (configuration error for the operator); an envelope whose key id has no history row, or whose blob does not decrypt under its key, opens
as `[unreadable: <reason>]` and is logged with the key id. Only a shredded key gives `[erased on <date>]`.

Off (the default) nothing changes: texts are written and read as plain strings, which is the behaviour of every recorded run. Rows written as
plain text before the envelope was turned on stay readable (no `env1:` prefix) and are listed by `tools/erasure.py` as not erasable by key.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import logging
import os
import secrets
from typing import Optional

from psycopg2.extras import Json

log = logging.getLogger("ripple.text_envelope")

PREFIX = "env1:"
ERASED = "[erased on {date}]"
UNREADABLE = "[unreadable: {reason}]"
LOCATIONS = ("v2_request_messages.question", "v2_chat_turns.text", "v2_business_change_intents.request_text", "v2_audit.payload.text (review notes)")
#: stored places that may carry a derived copy of a subject's words (model outputs, quotes checked against the text); not envelopes, not erased by a shred
DERIVED_COPIES = ("v2_request_messages.result (assistant answer blocks may restate the question)", "v2_chat_turns.understanding (user_quote fields)",
                  "v2_business_change_intents.operations (user_quote fields)", "v2_jobs.execution_result of change.understand / request.assistant jobs")
_WRAP_AAD = b"ripple-subject-key"


def enabled() -> bool:
    return os.environ.get("RIPPLE_TEXT_ENVELOPE", "").strip() == "1"


def _deployment_key() -> bytes:
    raw = os.environ.get("RIPPLE_DATA_KEY", "").strip()
    if not raw:
        raise RuntimeError("RIPPLE_TEXT_ENVELOPE=1 needs RIPPLE_DATA_KEY (base64, 32 bytes) from the secret store")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RuntimeError("RIPPLE_DATA_KEY is not valid base64") from exc
    if len(key) != 32:
        raise RuntimeError("RIPPLE_DATA_KEY must decode to 32 bytes")
    return key


def _aead(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _wrap(subject_key: bytes) -> str:
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + _aead(_deployment_key()).encrypt(nonce, subject_key, _WRAP_AAD)).decode()


def _unwrap(wrapped: str) -> bytes:
    """The subject key; raises RuntimeError when the deployment key cannot open it (wrong key: a configuration error, not an erasure)."""
    raw = base64.b64decode(wrapped)
    try:
        return _aead(_deployment_key()).decrypt(raw[:12], raw[12:], _WRAP_AAD)
    except Exception as exc:
        raise RuntimeError("the deployment key (RIPPLE_DATA_KEY) does not open the stored subject key: wrong key version or corrupted material") from exc


def _subject_lock(cur, tenant: str, ws: str, subject: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"subject-key:{tenant}:{ws}:{subject}",))


def _live_key(cur, tenant: str, ws: str, subject: str, *, create: bool) -> Optional[tuple]:
    """-> (key_id, subject key bytes) for the subject's live key, (key_id, None) when the newest history row is a shred, None when the subject has
    no key and `create` is False. Creation happens under the subject's advisory lock so concurrent first writes make exactly one key."""
    cur.execute("SELECT key_id, wrapped_key FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (tenant, ws, subject))
    row = cur.fetchone()
    if row is not None:
        return row[0], _unwrap(row[1])
    cur.execute("""SELECT key_id, action FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s ORDER BY created_at DESC, id DESC LIMIT 1""", (tenant, ws, subject))
    hist = cur.fetchone()
    if hist is not None and hist[1] == "shredded":
        return hist[0], None
    if not create:
        return None
    _subject_lock(cur, tenant, ws, subject)                                                    # serialize first writes of this subject
    cur.execute("SELECT key_id, wrapped_key FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (tenant, ws, subject))
    row = cur.fetchone()
    if row is not None:                                                                        # another writer created it while we waited
        return row[0], _unwrap(row[1])
    key_id, sk = "sk-" + secrets.token_hex(8), secrets.token_bytes(32)
    cur.execute("INSERT INTO v2_subject_key_material (tenant_id, workspace_id, subject_id, key_id, wrapped_key) VALUES (%s,%s,%s,%s,%s)", (tenant, ws, subject, key_id, _wrap(sk)))
    cur.execute("""INSERT INTO v2_subject_keys (tenant_id, workspace_id, subject_id, key_id, wrapped_key, action, reason, actor_id)
                   VALUES (%s,%s,%s,%s,'','created','first text of this subject','system')""", (tenant, ws, subject, key_id))
    return key_id, sk


def seal(cur, tenant: str, ws: str, subject: str, text: str) -> str:
    """The stored form of `text` written by `subject`: the plain text when the envelope is off, else an envelope under the subject's key. A subject whose
    key was shredded cannot write new text (the account was erased); the caller gets a ValueError. Runs in the caller's transaction: a rollback drops the
    key rows with the text."""
    if not enabled() or text is None:
        return text
    key_id, sk = _live_key(cur, tenant, ws, subject, create=True)
    if sk is None:
        raise ValueError("subject_erased")
    nonce = secrets.token_bytes(12)
    data = text.encode("utf-8")
    ct = _aead(sk).encrypt(nonce, data, key_id.encode())
    return PREFIX + key_id + ":" + hashlib.sha256(data).hexdigest() + ":" + base64.b64encode(nonce + ct).decode()


def is_envelope(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def plaintext_sha256(value: str) -> Optional[str]:
    """The sha256 the envelope records of its plaintext (verifiable against a copy without opening it); None for a plain string."""
    if not is_envelope(value):
        return None
    parts = value.split(":", 3)
    return parts[2] if len(parts) == 4 else None


def open_text(cur, tenant: str, ws: str, value):
    """The readable form of a stored value: a plain string as it is; an envelope opened with its key (read from the key store in this transaction, never
    from a process cache); `[erased on <date>]` once the key is shredded; `[unreadable: <reason>]` for a key id without history or a blob that does not
    decrypt (logged); a RuntimeError when the deployment key is missing or wrong."""
    if not is_envelope(value):
        return value
    try:
        _, key_id, _sha, blob = value.split(":", 3)
    except ValueError:
        log.warning("text_envelope: malformed envelope ignored (tenant=%s ws=%s)", tenant, ws)
        return UNREADABLE.format(reason="malformed_envelope")
    cur.execute("SELECT wrapped_key FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (tenant, ws, key_id))
    row = cur.fetchone()
    if row is None:
        cur.execute("""SELECT action, shredded_at FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s ORDER BY created_at DESC, id DESC LIMIT 1""", (tenant, ws, key_id))
        hist = cur.fetchone()
        if hist is not None and hist[0] == "shredded":
            return ERASED.format(date=(hist[1].date().isoformat() if hist[1] else "unknown"))
        reason = "key_material_missing" if hist is not None else "key_unknown"                   # history without material (restore without replay) / no history at all
        log.warning("text_envelope: %s for key %s (tenant=%s ws=%s)", reason, key_id, tenant, ws)
        return UNREADABLE.format(reason=reason)
    sk = _unwrap(row[0])                                                                        # raises on a wrong deployment key
    raw = base64.b64decode(blob)
    try:
        return _aead(sk).decrypt(raw[:12], raw[12:], key_id.encode()).decode("utf-8")
    except Exception:
        log.warning("text_envelope: blob does not decrypt under key %s (tenant=%s ws=%s)", key_id, tenant, ws)
        return UNREADABLE.format(reason="decrypt_failed")


def shred(cur, tenant: str, ws: str, subject: str, *, actor_id: str, reason: str) -> dict:
    """Erase a subject: delete the wrapped key material and append one history row marking the key shredded, in the caller's transaction
    (effective for every process once it commits). Idempotent per subject; audited by the caller; never touches a ledger row."""
    _subject_lock(cur, tenant, ws, subject)
    cur.execute("SELECT key_id FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (tenant, ws, subject))
    live = cur.fetchone()
    cur.execute("""SELECT key_id, action, shredded_at FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s ORDER BY created_at DESC, id DESC LIMIT 1""", (tenant, ws, subject))
    hist = cur.fetchone()
    if live is None and hist is None:
        return {"subject": subject, "state": "no_key", "note": "no enveloped text of this subject exists; plain rows written before the envelope stay as they are"}
    if live is None and hist[1] == "shredded":
        return {"subject": subject, "state": "already_shredded", "shredded_at": hist[2].isoformat() if hist[2] else None}
    key_id = live[0] if live is not None else hist[0]
    now = datetime.datetime.now(datetime.timezone.utc)
    cur.execute("DELETE FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s", (tenant, ws, subject))
    cur.execute("""INSERT INTO v2_subject_keys (tenant_id, workspace_id, subject_id, key_id, wrapped_key, action, reason, actor_id, shredded_at)
                   VALUES (%s,%s,%s,%s,'','shredded',%s,%s,%s)""", (tenant, ws, subject, key_id, reason, actor_id, now))
    return {"subject": subject, "state": "shredded", "key_id": key_id, "shredded_at": now.isoformat(), "material_deleted": cur.rowcount >= 0}


def forget_cache() -> None:
    """Kept for callers of the first version; there is no process cache any more."""
    return None


def audit_payload_text_seal(cur, tenant: str, ws: str, subject: str, payload: dict, field: str = "text") -> dict:
    """Seal one free-text field of an audit payload (review notes) under the writer's key; the rest of the payload stays plain for the reader rules."""
    if field in payload and isinstance(payload[field], str):
        return {**payload, field: seal(cur, tenant, ws, subject, payload[field])}
    return payload


def audit_payload_text_open(cur, tenant: str, ws: str, payload, field: str = "text"):
    if isinstance(payload, dict) and is_envelope(payload.get(field)):
        return {**payload, field: open_text(cur, tenant, ws, payload[field])}
    return payload


__all__ = ["enabled", "seal", "open_text", "shred", "is_envelope", "plaintext_sha256", "forget_cache", "audit_payload_text_seal", "audit_payload_text_open",
           "LOCATIONS", "DERIVED_COPIES", "ERASED", "UNREADABLE", "Json"]
