"""FB-N1 / FS-I1 (local build): a confirmed change waiting for review -> one durable intent -> recipients resolved from the CURRENT memberships and the review view -> inbox items
for everyone who may decide (never the requester/confirmer under the distinct-approver policy) -> one delivery per registered token through a MOCK transport with bounded
attempts. Nothing here proves browser delivery. What this proves: idempotent intents/items/deliveries across repeated passes, generic payload with an opaque key (no amounts,
rule text, job ids), invalid tokens retired, transport failure retried then failed without blocking the inbox, a decided change makes the intent stale and the item resolved,
read receipts are idempotent and decide nothing, tokens are never returned.
"""
from __future__ import annotations

import json

import psycopg2

from infra.adapters import v2_notifications as nt
from tests.final_loop.test_chg01_change_request import ACTOR_REQ, OP_ACTOR
from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_chg04_review import decide, login, prepared, review
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401

DEVICE_A = "synthetic-registration-a-" + "a" * 12       # synthetic device registrations (not credentials)
DEVICE_B = "synthetic-registration-b-" + "b" * 12


def inbox(q, client):
    return client.get("/v2/notifications/inbox", params={"workspace_id": q.env.ws}).json()


def test_a_pending_review_reaches_every_eligible_approver_once_with_a_generic_payload_and_resolves_when_decided(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    second = login(q, f"approver2-{q.env.nonce}", ("change_approver",), cred="c2")
    assert approver.post("/v2/notifications/tokens", json={"workspace_id": q.env.ws, "token": DEVICE_A, "platform": "web"}).status_code == 200
    assert approver.post("/v2/notifications/tokens", json={"workspace_id": q.env.ws, "token": DEVICE_B, "platform": "web"}).status_code == 200
    assert approver.post("/v2/notifications/tokens", json={"workspace_id": q.env.ws, "token": "short", "platform": "web"}).status_code == 409
    transport = nt.MockTransport()
    transport.invalid.add(DEVICE_B)
    out = nt.dispatch_once(q.env.store, transport, q.env.tenant, q.env.ws)
    intents = [i for i in out["intents"] if i["status"] in ("SENT", "PENDING")]
    assert len(out["derived"]) >= 1 and len(intents) == 1 and intents[0]["recipients"] == 3 and intents[0]["deliveries"] == 1 and out["invalid_tokens"] == 1
    assert len(transport.sent) == 1 and transport.sent[0]["token_digest"] == nt._digest(DEVICE_A)
    payload = transport.sent[0]["payload"]
    assert set(payload) == {"title", "body", "key", "schema"} and payload["key"].startswith("inb-") and cid not in json.dumps(payload) and "JPY" not in json.dumps(payload) and "1,000,000" not in json.dumps(payload)
    again = nt.dispatch_once(q.env.store, transport, q.env.tenant, q.env.ws)
    assert again["sent"] == 0 and again["derived"] == [] and len(transport.sent) == 1                                      # idempotent: nothing twice
    box = inbox(q, approver)
    assert box["unread"] == 1 and box["items"][0]["state"] == "open" and box["items"][0]["route"].endswith(f"/changes/{cid}/review") and box["items"][0]["item_id"] == payload["key"]
    assert inbox(q, second)["unread"] == 1 and inbox(q, q.req)["items"] == []                                              # the requester is never a recipient of their own review
    assert inbox(q, q.admin)["unread"] == 1                                                                                # the admin may decide too and is not the requester
    item = box["items"][0]["item_id"]
    r1 = approver.post(f"/v2/notifications/inbox/{item}/read", json={"workspace_id": q.env.ws})
    r2 = approver.post(f"/v2/notifications/inbox/{item}/read", json={"workspace_id": q.env.ws})
    assert r1.status_code == 200 and r2.status_code == 200 and r1.json()["read_at"] == r2.json()["read_at"]
    assert second.post(f"/v2/notifications/inbox/{item}/read", json={"workspace_id": q.env.ws}).status_code == 404        # another person's item
    assert inbox(q, approver)["unread"] == 0 and inbox(q, second)["unread"] == 1
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_change_decisions WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 0                                                                                       # reading decided nothing
        cur.execute("SELECT invalid_at IS NOT NULL FROM v2_notification_tokens WHERE tenant_id=%s AND workspace_id=%s AND token_digest=%s", (q.env.tenant, q.env.ws, nt._digest(DEVICE_B)))
        assert cur.fetchone()[0] is True
    binding = review(q, approver, cid).json()["binding"] if "binding" in review(q, approver, cid).json() else review(q, approver, cid).json()["draft"]["binding"]
    assert decide(q, approver, cid, binding).status_code == 200
    assert inbox(q, second)["items"][0]["state"] == "resolved" and inbox(q, second)["unread"] == 0                        # decided elsewhere: resolved, not pending
    assert json.dumps(inbox(q, approver)) .find(DEVICE_A) == -1
    assert approver.post("/v2/notifications/tokens/revoke", json={"workspace_id": q.env.ws, "all": True}).json()["revoked"] == 2                 # both registered tokens, the retired one included


def test_a_transport_failure_is_retried_then_failed_while_the_inbox_still_works_and_a_decided_change_makes_a_late_intent_stale(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    assert approver.post("/v2/notifications/tokens", json={"workspace_id": q.env.ws, "token": DEVICE_A, "platform": "web"}).status_code == 200
    transport = nt.MockTransport()
    transport.fail = True
    for _ in range(nt.MAX_ATTEMPTS):
        out = nt.dispatch_once(q.env.store, transport, q.env.tenant, q.env.ws)
    assert out["failed"] == 1 and out["intents"][0]["status"] == "SENT" and inbox(q, approver)["unread"] == 1               # bounded attempts; the inbox is the full path
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, attempts FROM v2_notification_deliveries WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        assert cur.fetchall() == [("FAILED", nt.MAX_ATTEMPTS)]
    view = review(q, approver, cid).json()
    binding = view["binding"] if "binding" in view else view["draft"]["binding"]
    assert decide(q, approver, cid, binding).status_code == 200
    cid2, _ = confirmed(q, ACTOR_REQ, [OP_ACTOR], client=q.req)                                                             # a second confirmed change, decided before any dispatch
    view2 = review(q, approver, cid2).json()
    assert decide(q, approver, cid2, view2["binding"], acknowledged_flags=True).status_code == 200                           # a high-risk change: the flag is acknowledged by the approver
    transport.fail = False
    out = nt.dispatch_once(q.env.store, transport, q.env.tenant, q.env.ws)
    assert out["stale"] >= 1 and out["sent"] == 0                                                                           # nothing is sent for a change nobody can still decide
    assert q.admin.post("/v2/notifications/dispatch", json={"workspace_id": q.env.ws}).status_code == 503                     # transport off by default
    assert q.req.post("/v2/notifications/dispatch", json={"workspace_id": q.env.ws}).status_code == 403
