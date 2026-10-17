"""Approval notifications and inbox (extension FB-N1 / FS-I1, local build).

Intent: a confirmed change request (`v2_business_change_intents`, the durable committed event) that waits for a decision -> one durable notification intent (idempotent by
change id). Dispatch: recipients are resolved at dispatch time from the CURRENT memberships (everyone whose role may decide and for whom the review view says the decision is
open: the requester/confirmer is excluded by the distinct-approver policy, a decided or moved change makes the intent STALE); every recipient gets an inbox item (the full path
when push is refused or absent); every registered, valid token of a recipient gets one delivery through the transport port (`MockTransport` locally) with bounded attempts; the
payload is generic text plus an opaque key, never amounts, rule text or an approval token. A read receipt is idempotent and only a UX fact. Nothing here decides anything.
"""
from __future__ import annotations

import hashlib

from psycopg2.extras import Json

from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters import v2_business
from infra.adapters.v2_business import BusinessKnowledgeRefused, _as_object, read_snapshot
from infra.adapters.v2_policy import permissions_for
from infra.adapters.v2_postgres import TenantScopeError

SCHEMA = "ripple-notifications/1"
KIND_REVIEW = "review_pending"
MAX_ATTEMPTS = 3
PAYLOAD = {KIND_REVIEW: {"title": "A change is waiting for your review", "body": "Open Ripple to see the current request."}}


class TransportError(Exception):
    pass


class InvalidToken(TransportError):
    pass


class MockTransport:
    """Local transport: records sends; `fail` makes every send raise; tokens in `invalid` are reported as invalid (unregistered device)."""

    def __init__(self):
        self.sent: list = []
        self.fail = False
        self.invalid: set = set()

    def send(self, token: str, payload: dict) -> str:
        if self.fail:
            raise TransportError("transport down")
        if token in self.invalid:
            raise InvalidToken("unregistered")
        self.sent.append({"token_digest": _digest(token), "payload": dict(payload)})
        return f"msg-{len(self.sent)}"


def _digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _audit(cur, tenant_id, workspace_id, event, actor_id, payload):
    cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)", (tenant_id, workspace_id, event, actor_id, Json(payload)))


# ---- tokens ------------------------------------------------------------------------------------------------------------------------------------------

def register_token(store, tenant_id, workspace_id, *, account_id: str, token: str, platform: str) -> dict:
    if not isinstance(token, str) or not 16 <= len(token) <= 4096 or not isinstance(platform, str) or platform not in ("web", "android", "ios"):
        raise BusinessKnowledgeRefused("token_invalid", "token (16-4096 characters) and platform web|android|ios required")
    d = _digest(token)
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_notification_tokens (tenant_id, workspace_id, account_id, token_digest, token, platform) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, workspace_id, token_digest) DO UPDATE SET account_id=EXCLUDED.account_id, platform=EXCLUDED.platform, registered_at=now(), invalid_at=NULL, revoked_at=NULL""",
                    (tenant_id, workspace_id, account_id, d, token, platform))
        _audit(cur, tenant_id, workspace_id, "NOTIFICATION_TOKEN_REGISTERED", account_id, {"platform": platform, "token_digest": d[:23]})
    return {"registered": True, "token_digest": d[:23], "platform": platform, "statement": "The token is routing data: it is never shown again and grants nothing."}


def revoke_token(store, tenant_id, workspace_id, *, account_id: str, token: str | None = None, all_tokens: bool = False) -> dict:
    with store._tx() as cur:
        if all_tokens:
            cur.execute("UPDATE v2_notification_tokens SET revoked_at=now() WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s AND revoked_at IS NULL", (tenant_id, workspace_id, account_id))
        else:
            if not isinstance(token, str) or not token:
                raise BusinessKnowledgeRefused("token_invalid", "token required")
            cur.execute("UPDATE v2_notification_tokens SET revoked_at=now() WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s AND token_digest=%s AND revoked_at IS NULL", (tenant_id, workspace_id, account_id, _digest(token)))
        n = cur.rowcount
        _audit(cur, tenant_id, workspace_id, "NOTIFICATION_TOKEN_REVOKED", account_id, {"count": n, "all": all_tokens})
    return {"revoked": n}


def _tokens_of(cur, tenant_id, workspace_id, account_id) -> list:
    cur.execute("SELECT token_digest, token FROM v2_notification_tokens WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s AND invalid_at IS NULL AND revoked_at IS NULL ORDER BY registered_at",
                (tenant_id, workspace_id, account_id))
    return cur.fetchall()


# ---- intents and dispatch --------------------------------------------------------------------------------------------------------------------------------

def intent_id_for(change_id: str) -> str:
    return "nti-" + canonical_hash({"change": change_id, "kind": KIND_REVIEW})[:20]


def derive_intents(store, tenant_id, workspace_id) -> dict:
    """Every confirmed change request without an intent yet -> one PENDING intent (idempotent). Reads committed rows only."""
    created = []
    with store._tx() as cur:
        cur.execute("""SELECT i.intent_id, i.content_hash FROM v2_business_change_intents i LEFT JOIN v2_notification_intents n
            ON n.tenant_id=i.tenant_id AND n.workspace_id=i.workspace_id AND n.intent_id=%s || substr(md5(''), 1, 0) AND false
            WHERE i.tenant_id=%s AND i.workspace_id=%s ORDER BY i.created_at""", ("", tenant_id, workspace_id))
        rows = cur.fetchall()
        for change_id, content_hash in rows:
            nid = intent_id_for(change_id)
            cur.execute("""INSERT INTO v2_notification_intents (tenant_id, workspace_id, intent_id, kind, target_ref, binding_hash, status) VALUES (%s,%s,%s,%s,%s,%s,'PENDING')
                ON CONFLICT (tenant_id, workspace_id, intent_id) DO NOTHING""", (tenant_id, workspace_id, nid, KIND_REVIEW, change_id, content_hash))
            if cur.rowcount == 1:
                created.append(nid)
    return {"created": created}


def _candidates(cur, tenant_id, workspace_id) -> list:
    cur.execute("SELECT account_id, roles FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s ORDER BY account_id", (tenant_id, workspace_id))
    return [(a, tuple(r)) for a, r in cur.fetchall() if "change.approve" in permissions_for(tuple(r))]


def _actionable(store, tenant_id, workspace_id, change_id, account_id, roles) -> bool | None:
    """True when THIS person may decide the knowledge change now; False when not; None when the change is not (or no longer) reviewable at all."""
    from infra.adapters import v2_business_review
    try:
        view = v2_business_review.review_view(store, tenant_id, workspace_id, change_id, account_id=account_id, roles=roles)
    except (TenantScopeError, BusinessKnowledgeRefused):
        return None
    if view is None:
        return None
    k = view["can"]["knowledge_decision"]
    if view["committed"] is not None or view["knowledge_moved"] or (k is not None and k["action"] in ("approve", "reject")):
        return None
    return bool(view["can"]["approve_knowledge"])


def dispatch_once(store, transport, tenant_id, workspace_id, *, limit: int = 20) -> dict:
    """Resolve recipients at the committed state, write inbox items, send one delivery per valid token with bounded attempts. Idempotent: a repeated pass sends nothing twice."""
    derived = derive_intents(store, tenant_id, workspace_id)
    with read_snapshot(store) as cur:
        cur.execute("SELECT intent_id, kind, target_ref FROM v2_notification_intents WHERE tenant_id=%s AND workspace_id=%s AND status='PENDING' ORDER BY created_at LIMIT %s", (tenant_id, workspace_id, limit))
        pending = cur.fetchall()
        candidates = _candidates(cur, tenant_id, workspace_id)
    summary = {"derived": derived["created"], "intents": [], "sent": 0, "invalid_tokens": 0, "failed": 0, "stale": 0}
    for intent_id, kind, change_id in pending:
        verdicts = {a: _actionable(store, tenant_id, workspace_id, change_id, a, roles) for a, roles in candidates}
        if all(v is None for v in verdicts.values()):
            with store._tx() as cur:
                cur.execute("UPDATE v2_notification_intents SET status='STALE', resolved_at=now() WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND status='PENDING'", (tenant_id, workspace_id, intent_id))
                _audit(cur, tenant_id, workspace_id, "NOTIFICATION_INTENT_STALE", "server", {"intent_id": intent_id, "target_ref": change_id})
            summary["stale"] += 1
            summary["intents"].append({"intent_id": intent_id, "status": "STALE", "recipients": 0})
            continue
        recipients = sorted(a for a, v in verdicts.items() if v is True)
        entry = {"intent_id": intent_id, "status": "SENT", "recipients": len(recipients), "deliveries": 0}
        with store._tx() as cur:
            for a in recipients:
                cur.execute("""INSERT INTO v2_inbox_items (tenant_id, workspace_id, item_id, account_id, intent_id, kind, target_ref) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id, workspace_id, item_id) DO NOTHING""", (tenant_id, workspace_id, item_id_for(intent_id, a), a, intent_id, kind, change_id))
            cur.execute("UPDATE v2_notification_intents SET recipients=%s WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s", (Json(recipients), tenant_id, workspace_id, intent_id))
            tokens = [(a, d, t) for a in recipients for d, t in _tokens_of(cur, tenant_id, workspace_id, a)]
            for a, d, _ in tokens:
                cur.execute("""INSERT INTO v2_notification_deliveries (tenant_id, workspace_id, intent_id, token_digest, account_id, status) VALUES (%s,%s,%s,%s,%s,'PENDING')
                    ON CONFLICT DO NOTHING""", (tenant_id, workspace_id, intent_id, d, a))
        all_done = True
        for a, d, token in tokens:
            with store._tx() as cur:
                cur.execute("SELECT status, attempts FROM v2_notification_deliveries WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND token_digest=%s FOR UPDATE", (tenant_id, workspace_id, intent_id, d))
                st, attempts = cur.fetchone()
                if st != "PENDING":
                    continue
                payload = {**PAYLOAD.get(kind, PAYLOAD[KIND_REVIEW]), "key": item_id_for(intent_id, a), "schema": SCHEMA}
                try:
                    transport.send(token, payload)
                    cur.execute("UPDATE v2_notification_deliveries SET status='SENT', attempts=attempts+1, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND token_digest=%s", (tenant_id, workspace_id, intent_id, d))
                    summary["sent"] += 1
                    entry["deliveries"] += 1
                except InvalidToken:
                    cur.execute("UPDATE v2_notification_deliveries SET status='INVALID_TOKEN', attempts=attempts+1, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND token_digest=%s", (tenant_id, workspace_id, intent_id, d))
                    cur.execute("UPDATE v2_notification_tokens SET invalid_at=now() WHERE tenant_id=%s AND workspace_id=%s AND token_digest=%s", (tenant_id, workspace_id, d))
                    summary["invalid_tokens"] += 1
                except TransportError as exc:
                    attempts += 1
                    final = attempts >= MAX_ATTEMPTS
                    cur.execute("UPDATE v2_notification_deliveries SET status=%s, attempts=%s, last_error=%s, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND token_digest=%s",
                                ("FAILED" if final else "PENDING", attempts, str(exc)[:120], tenant_id, workspace_id, intent_id, d))
                    summary["failed"] += 1
                    if not final:
                        all_done = False
        with store._tx() as cur:
            if all_done:
                cur.execute("UPDATE v2_notification_intents SET status='SENT', resolved_at=now() WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND status='PENDING'", (tenant_id, workspace_id, intent_id))
            else:
                entry["status"] = "PENDING"
            _audit(cur, tenant_id, workspace_id, "NOTIFICATION_DISPATCHED", "server", {"intent_id": intent_id, "recipients": len(recipients), "deliveries": entry["deliveries"], "status": entry["status"]})
        summary["intents"].append(entry)
    return summary


def item_id_for(intent_id: str, account_id: str) -> str:
    return "inb-" + canonical_hash({"intent": intent_id, "account": account_id})[:20]


# ---- inbox ---------------------------------------------------------------------------------------------------------------------------------------------

def inbox(store, tenant_id, workspace_id, *, account_id: str, limit: int = 50) -> dict:
    with read_snapshot(store) as cur:
        cur.execute("""SELECT i.item_id, i.intent_id, i.kind, i.target_ref, i.created_at, i.read_at, n.status FROM v2_inbox_items i JOIN v2_notification_intents n
            ON n.tenant_id=i.tenant_id AND n.workspace_id=i.workspace_id AND n.intent_id=i.intent_id
            WHERE i.tenant_id=%s AND i.workspace_id=%s AND i.account_id=%s ORDER BY i.created_at DESC, i.item_id LIMIT %s""", (tenant_id, workspace_id, account_id, limit))
        rows = cur.fetchall()
    items = []
    for item_id, intent_id, kind, target_ref, created_at, read_at, status in rows:
        current = _actionable(store, tenant_id, workspace_id, target_ref, account_id, v2_business.current_roles(store, tenant_id, workspace_id, account_id)) if kind == KIND_REVIEW else None
        state = "open" if current is True else "resolved" if current is None else "not_yours"
        items.append({"item_id": item_id, "kind": kind, "target_ref": target_ref, "created_at": created_at.isoformat(), "read_at": read_at.isoformat() if read_at else None, "state": state,
                      "title": PAYLOAD.get(kind, PAYLOAD[KIND_REVIEW])["title"], "route": f"/v2/business/changes/{target_ref}/review"})
    unread = sum(1 for it in items if it["read_at"] is None and it["state"] == "open")
    return {"schema": SCHEMA, "items": items, "unread": unread, "truncated": len(rows) >= limit,
            "statement": "An inbox item points at the current request; opening it re-checks your permission. Reading is not approving; a resolved item stays as history."}


def mark_read(store, tenant_id, workspace_id, *, account_id: str, item_id: str) -> dict:
    with store._tx() as cur:
        cur.execute("UPDATE v2_inbox_items SET read_at=COALESCE(read_at, now()) WHERE tenant_id=%s AND workspace_id=%s AND item_id=%s AND account_id=%s RETURNING read_at", (tenant_id, workspace_id, item_id, account_id))
        row = cur.fetchone()
    if row is None:
        raise TenantScopeError("inbox item not found")
    return {"item_id": item_id, "read_at": row[0].isoformat(), "statement": "Marked as read for you only; nothing was decided."}
