"""Server-side storage of request conversations (free text -> clarification -> understanding -> confirmed intent).

Everything is scoped by (tenant, workspace) and by account: the creator and accounts that may confirm knowledge can read a
conversation (the confirmer needs it to act on a handed-over link); nobody else. A conversation keeps the person's exact words,
the AI's answers and every VERSION of the structured understanding; an earlier version is never rewritten. No evidence quotes are
stored here. Written through a store object's `_tx()` so it shares the store's connection handling; nothing here calls a model.
"""

from __future__ import annotations

import json
import uuid

from psycopg2.extras import Json

MAX_TURN_TEXT = 4000
MAX_TURNS = 60


class ChatError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def new_id() -> str:
    return "chat-" + uuid.uuid4().hex[:16]


def create(store, tenant: str, ws: str, account: str, text: str, head, *, purpose: str = "change_request", context: dict | None = None) -> str:
    cid = new_id()
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_chat_conversations (tenant_id, workspace_id, conversation_id, created_by,
            pinned_source_revision_id, pinned_decision_epoch, purpose, context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant, ws, cid, account, head.source_revision_id, head.decision_epoch, purpose, Json(context) if context else None))
        cur.execute("""INSERT INTO v2_chat_turns (tenant_id, workspace_id, conversation_id, seq, role, text, created_by)
            VALUES (%s,%s,%s,1,'user',%s,%s)""", (tenant, ws, cid, text[:MAX_TURN_TEXT], account))
    return cid


def add_turn(store, tenant, ws, cid, role, text, account=None, *, understanding=None, version=None, job_id=None) -> int:
    with store._tx() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"chat:{tenant}:{ws}:{cid}",))
        cur.execute("SELECT status, count(*) FROM v2_chat_conversations c LEFT JOIN v2_chat_turns t USING (tenant_id, workspace_id, conversation_id) "
                    "WHERE c.tenant_id=%s AND c.workspace_id=%s AND c.conversation_id=%s GROUP BY status", (tenant, ws, cid))
        row = cur.fetchone()
        if row is None:
            raise ChatError("not_found", "conversation not found", 404)
        if row[1] >= MAX_TURNS:
            raise ChatError("too_long", f"a conversation is limited to {MAX_TURNS} messages; start a new request", 409)
        cur.execute("SELECT coalesce(max(seq),0)+1 FROM v2_chat_turns WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s",
                    (tenant, ws, cid))
        seq = cur.fetchone()[0]
        cur.execute("""INSERT INTO v2_chat_turns (tenant_id, workspace_id, conversation_id, seq, role, text, understanding,
            understanding_version, job_id, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING""",
                    (tenant, ws, cid, seq, role, text[:MAX_TURN_TEXT], Json(understanding) if understanding is not None else None,
                     version, job_id, account))
        return seq


def next_version(store, tenant, ws, cid) -> int:
    with store._tx() as cur:
        cur.execute("SELECT coalesce(max(understanding_version),0)+1 FROM v2_chat_turns WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s",
                    (tenant, ws, cid))
        return cur.fetchone()[0]


def get(store, tenant, ws, cid) -> dict | None:
    with store._tx() as cur:
        cur.execute("""SELECT created_by, created_at, status, ai_allowance, ai_used, ai_approved_by, ai_approved_at,
                pinned_source_revision_id, pinned_decision_epoch, intent_id, ai_expires_at, ai_revoked_at, ai_revoked_by, previous_intent_id, purpose, context, outcome FROM v2_chat_conversations
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s""", (tenant, ws, cid))
        c = cur.fetchone()
        if c is None:
            return None
        cur.execute("""SELECT seq, role, text, understanding, understanding_version, job_id, created_by, created_at FROM v2_chat_turns
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s ORDER BY seq""", (tenant, ws, cid))
        turns = [{"seq": r[0], "role": r[1], "text": r[2], "understanding": _obj(r[3]), "version": r[4], "job_id": r[5],
                  "by": r[6], "at": r[7].isoformat() if r[7] else None} for r in cur.fetchall()]
    latest = next((t for t in reversed(turns) if t["understanding"] is not None), None)
    return {"conversation_id": cid, "created_by": c[0], "created_at": c[1].isoformat(), "status": c[2], "ai_allowance": c[3],
            "ai_used": c[4], "ai_approved_by": c[5], "ai_approved_at": c[6].isoformat() if c[6] else None,
            "pinned_source_revision_id": c[7], "pinned_decision_epoch": c[8], "intent_id": c[9], "ai_expires_at": c[10].isoformat() if c[10] else None, "ai_revoked_at": c[11].isoformat() if c[11] else None,
            "ai_revoked_by": c[12], "previous_intent_id": c[13], "purpose": c[14], "context": _obj(c[15]), "outcome": _obj(c[16]), "turns": turns,
            "understanding": latest["understanding"] if latest else None, "version": latest["version"] if latest else None}


def list_for(store, tenant, ws, account, *, all_visible: bool) -> list[dict]:
    with store._tx() as cur:
        cur.execute("""SELECT c.conversation_id, c.created_by, c.created_at, c.status,
                (SELECT text FROM v2_chat_turns t WHERE t.tenant_id=c.tenant_id AND t.workspace_id=c.workspace_id
                   AND t.conversation_id=c.conversation_id ORDER BY seq LIMIT 1)
            FROM v2_chat_conversations c WHERE c.tenant_id=%s AND c.workspace_id=%s AND (%s OR c.created_by=%s)
            ORDER BY c.created_at DESC LIMIT 30""", (tenant, ws, all_visible, account))
        return [{"conversation_id": r[0], "created_by": r[1], "created_at": r[2].isoformat(), "status": r[3], "first_text": (r[4] or "")[:160]}
                for r in cur.fetchall()]


def grant_allowance(store, tenant, ws, cid, approver, turns, cap, minutes=60) -> dict:
    with store._tx() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"chat:{tenant}:{ws}:{cid}",))
        cur.execute("SELECT ai_allowance, ai_used FROM v2_chat_conversations WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s FOR UPDATE",
                    (tenant, ws, cid))
        row = cur.fetchone()
        if row is None:
            raise ChatError("not_found", "conversation not found", 404)
        new_total = min(cap, row[0] + turns)
        cur.execute("""UPDATE v2_chat_conversations SET ai_allowance=%s, ai_approved_by=%s, ai_approved_at=now(),
            ai_expires_at=now() + make_interval(mins => %s), ai_revoked_at=NULL, ai_revoked_by=NULL
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s""", (new_total, approver, minutes, tenant, ws, cid))
        return {"ai_allowance": new_total, "ai_used": row[1]}


def claim_ai_turn(store, tenant, ws, cid) -> bool:
    """Atomically use one pre-approved AI turn; False when none is left (the person's text is still stored)."""
    with store._tx() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"chat:{tenant}:{ws}:{cid}",))
        cur.execute("""UPDATE v2_chat_conversations SET ai_used = ai_used + 1
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s AND ai_used < ai_allowance AND status='OPEN'
              AND ai_revoked_at IS NULL AND (ai_expires_at IS NULL OR ai_expires_at > now())""",
                    (tenant, ws, cid))
        return cur.rowcount == 1


def count_first_job(store, tenant, ws, cid) -> None:
    """The first AI reply is approved as a job (counts as one used turn)."""
    with store._tx() as cur:
        cur.execute("""UPDATE v2_chat_conversations SET ai_used = GREATEST(ai_used, 1), ai_allowance = GREATEST(ai_allowance, 1)
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s""", (tenant, ws, cid))


def set_status(store, tenant, ws, cid, status, intent_id=None) -> None:
    with store._tx() as cur:
        cur.execute("UPDATE v2_chat_conversations SET status=%s, "
                    "previous_intent_id = CASE WHEN %s IS NOT NULL AND intent_id IS NOT NULL AND intent_id <> %s THEN intent_id ELSE previous_intent_id END, "
                    "intent_id=coalesce(%s, intent_id) WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s",
                    (status, intent_id, intent_id, intent_id, tenant, ws, cid))


def set_outcome(store, tenant, ws, cid, outcome: dict) -> None:
    """The confirmed, structured result of a non-request purpose (what the person agreed to; nothing has been executed)."""
    with store._tx() as cur:
        cur.execute("UPDATE v2_chat_conversations SET outcome=%s WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s",
                    (Json(outcome), tenant, ws, cid))


def release_ai_turn(store, tenant, ws, cid) -> None:
    """Give back a claimed turn when the job could not be created (nothing was reserved or called)."""
    with store._tx() as cur:
        cur.execute("UPDATE v2_chat_conversations SET ai_used = GREATEST(ai_used - 1, 0) "
                    "WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s", (tenant, ws, cid))


def fill_turn(store, tenant, ws, cid, seq, text, understanding, version) -> None:
    """Complete a pending assistant turn once; a filled turn is never rewritten."""
    with store._tx() as cur:
        cur.execute("""UPDATE v2_chat_turns SET text=%s, understanding=%s, understanding_version=%s
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s AND seq=%s AND role='assistant' AND text=''""",
                    (text[:MAX_TURN_TEXT], Json(understanding) if understanding is not None else None, version, tenant, ws, cid, seq))


def revoke(store, tenant, ws, cid, by) -> None:
    """Withdraw the unused part of the AI permission now (replies already produced stay; nothing is refunded or deleted)."""
    with store._tx() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"chat:{tenant}:{ws}:{cid}",))
        cur.execute("""UPDATE v2_chat_conversations SET ai_allowance = ai_used, ai_revoked_at = now(), ai_revoked_by = %s
            WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s""", (by, tenant, ws, cid))


def audit(store, tenant, ws, event, actor, payload) -> None:
    """Minimal audit row: reason CODES and ids only, never the person's text."""
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                    (tenant, ws, event, actor, Json(payload)))
