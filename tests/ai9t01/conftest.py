"""Fixtures for the history browser and restore preview.

This docstring lists the interface the tests bind to. Every name extends an
existing pattern.

History and preview contract
----------------------------
* `GET  /v2/history/{workspace_id}[?limit=N]`            (`project.read`, current ACL)
    200 `{workspace_id, head{version, source_revision_id, knowledge_revision_id,
    decision_epoch}, revisions[{revision_id, sequence:int, manifest_hash,
    parent_revision_id, restored_from, trigger, created_at, is_current,
    artifacts[{artifact_id, blob_hash}]}] (oldest -> newest, `sequence` = 1-based
    ancestry ordinal, DISTINCT from the content `manifest_hash`),
    knowledge_revisions[{revision_id, source_revision_id, decision_epoch}],
    decisions[{decision_id, epoch}], timeline[{kind, ref, at}] (kinds include
    source_revision / knowledge_revision / decision), omitted{revisions:int}}`.
* `GET  /v2/history/{workspace_id}/diff?from_revision_id=&to_revision_id=`
    (`project.read`) 200 `{from_revision_id, to_revision_id, added[], removed[],
    changed[{artifact_id, before_hash, after_hash}], unchanged_count, omitted{}}`;
    404 unknown / other-workspace / other-tenant revision (uniform, no leak).
* `POST /v2/restore/preview` body `{workspace_id, target_revision_id}`
    (`source.restore`; read-only: NO durable write of any kind). 200 DTO carrying
    the reviewed `RecoveryProposal` fields (`core.change_intelligence.v2.schema`):
    `{proposal_id, workspace_id, noop:bool, current{source_revision_id, version,
    knowledge_revision_id, decision_epoch, manifest_hash}, target{source_revision_id,
    manifest_hash, sequence}, restored_from, delta{added[], removed[], changed[
    {artifact_id,before_hash,after_hash}], unchanged_count}, intervening_edits[
    {source_revision_id, artifact_ids[]}], affected_decision_ids[],
    stale_knowledge_revision_ids[], open_changes[], planned_verification[{
    artifact_id, check}], loss_warning, truncated:bool, omitted{delta,
    intervening_edits, open_changes}, expected{version, source_revision_id,
    decision_epoch, knowledge_revision_id}, preview_digest}`. `noop:true`
    (target == current) => 200, empty delta, `preview_digest: null`. 400
    malformed; 401 no session; 403 permission / revoked membership; 404 unknown /
    other-workspace / other-tenant target; 409 `{error:"restore_target_unavailable",
    reasons:[...]}` for a missing or checksum-invalid blob of ANY artifact of the
    target manifest (no digest, no delta, no fallback to another revision).
* `preview_digest` = canonical hash over (workspace, current tuple, target id +
  manifest, FULL untruncated delta / intervening edits / affected decisions /
  stale KM / open changes / planned verification). Deterministic; changes when
  head, decision epoch or target changes.
* Config (env, tighten-only): `RIPPLE_RESTORE_PREVIEW_MAX_ITEMS` (int >= 1,
  default 200) caps every LISTED collection; a cap that hides items sets
  `truncated=true` and `omitted.<collection>` (count of hidden items).
* Registry: the three GET/POST routes above are added to
  `infra/adapters/v2_policy.py::ROUTE_PERMISSIONS` (`project.read` /
  `project.read` / `source.restore`).

Fixtures build real durable history through the REAL store primitives
(`publish_source`, `record_decision`, `publish_knowledge`) - history browsing
and preview do not depend on how a revision was produced. The real apply chain
is used where provenance matters (`test_h1_open_changes...`).
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest  # noqa: F401

from tests.ai8t04.conftest import *  # noqa: F401,F403  (fixtures: setup, owner + T03/T04 helpers)
from tests.ai8t04.conftest import (  # noqa: F401
    DSN, applied_change, build_second_change, drain_outbox, head_tuple, login, patch_authority_kind,
)

MAX_ITEMS_ENV = "RIPPLE_RESTORE_PREVIEW_MAX_ITEMS"

A_TEXT = "refund_window_days: 14\n"
B_TEXT = "refund_window_days: 30\n"
C_TEXT = "refund_window_days: 45\n"
SHARED_TEXT = "shared_limit: 1\n"


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_account(setup, roles, prefix="acct", workspace=None):
    """A distinct human account with `roles` in `workspace` (default: the
    setup workspace), logged in. Returns {account, credential, client}."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    nonce = uuid4().hex
    account, credential = f"{prefix}-{nonce}", f"cred-{prefix}-{nonce}"
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.create_account(account, credential)
    identities.grant_membership(setup["tenant"], setup["project"],
                                workspace or setup["workspace"], account, tuple(roles))
    client = setup["browser"]()
    login(client, account, credential)
    return {"account": account, "credential": credential, "client": client}


def restore_operator(setup, prefix="restore-op", workspace=None):
    return make_account(setup, ("restore_operator",), prefix, workspace)


def revoke(setup, account, workspace=None):
    from infra.adapters.v2_identity import PostgresIdentityStore
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.revoke_membership(setup["tenant"], setup["project"],
                                 workspace or setup["workspace"], account["account"])


def publish_rev(setup, files: dict, *, tenant=None, workspace=None, revision_id=None):
    """Publish one WHOLE-snapshot source revision {artifact_id: text}."""
    store = setup["store"]
    tenant, workspace = tenant or setup["tenant"], workspace or setup["workspace"]
    revision_id = revision_id or f"rev-{uuid4().hex}"
    manifest = {aid: store.stage_blob(text.encode("utf-8")) for aid, text in files.items()}
    store.publish_source(tenant, workspace, revision_id, manifest,
                         expected_version=store.head(tenant, workspace).version,
                         operation="source.import.commit", idempotency_key=uuid4().hex)
    return revision_id


def build_history(setup, *, with_decision_and_km=False):
    """A -> B -> C (multi-artifact: a shared, unchanged artifact `s.yaml`; C
    also ADDS `c.yaml`, so a restore of A must REMOVE it)."""
    a = publish_rev(setup, {"a.yaml": A_TEXT, "s.yaml": SHARED_TEXT})
    b = publish_rev(setup, {"a.yaml": B_TEXT, "s.yaml": SHARED_TEXT})
    c = publish_rev(setup, {"a.yaml": C_TEXT, "s.yaml": SHARED_TEXT, "c.yaml": "extra: true\n"})
    hist = {"a": a, "b": b, "c": c, "workspace": setup["workspace"]}
    if with_decision_and_km:
        store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
        store.record_decision(t, ws, "dec-h-1", setup["admin"],
                              {"action": "CONFIRM", "artifact_id": "a.yaml",
                               "note": "AI9 history fixture decision"},
                              expected_version=store.head(t, ws).version,
                              idempotency_key=uuid4().hex)
        head = store.head(t, ws)
        store.publish_knowledge(t, ws, "km-h-1", head.source_revision_id, head.decision_epoch,
                                expected_version=head.version, idempotency_key=uuid4().hex)
        hist.update(decision_id="dec-h-1", km_id="km-h-1")
    drain_outbox(setup["store"], setup["tenant"], setup["workspace"])
    return hist


def other_workspace(setup, *, cross_tenant=False, files=None):
    """A second workspace holding its own revision; returns
    {tenant, project, workspace, revision_id}."""
    nonce = uuid4().hex[:10]
    tenant = f"tenant-x-{nonce}" if cross_tenant else setup["tenant"]
    project = f"project-x-{nonce}" if cross_tenant else setup["project"]
    workspace = f"workspace-x-{nonce}"
    setup["store"].create_workspace(tenant, project, workspace)
    rev = publish_rev(setup, files or {"a.yaml": "foreign_secret_marker: 4242\n"},
                      tenant=tenant, workspace=workspace)
    return {"tenant": tenant, "project": project, "workspace": workspace, "revision_id": rev}


# ---------------------------------------------------------------------- API helpers

def get_history(client, workspace, **params):
    return client.get(f"/v2/history/{workspace}", params=params or None)


def get_diff(client, workspace, frm, to):
    return client.get(f"/v2/history/{workspace}/diff",
                      params={"from_revision_id": frm, "to_revision_id": to})


def preview(client, workspace, target, **extra):
    body = {"workspace_id": workspace, "target_revision_id": target}
    body.update(extra)
    return client.post("/v2/restore/preview", json=body)


def preview_ok(client, workspace, target, **extra):
    resp = preview(client, workspace, target, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------- durable state

def durable_snapshot(setup, workspace=None):
    """Content-hash of EVERY `v2_*` table row scoped to (tenant, workspace)
    (discovered from information_schema, so a table added by AI9 is covered
    automatically) plus the blob port contents. A read-only request must leave
    this byte-for-byte identical (H1)."""
    from psycopg2 import sql
    store, tenant = setup["store"], setup["tenant"]
    workspace = workspace or setup["workspace"]
    snap = {}
    with store._tx() as cur:
        cur.execute("""SELECT table_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name LIKE 'v2\\_%%'
              AND column_name = 'tenant_id'
            INTERSECT
            SELECT table_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND column_name = 'workspace_id'
            ORDER BY 1""")
        for (table,) in cur.fetchall():
            cur.execute(sql.SQL("SELECT count(*), coalesce(md5(string_agg(t::text, '|' "
                                "ORDER BY t::text)), '') FROM {} t WHERE tenant_id=%s "
                                "AND workspace_id=%s").format(sql.Identifier(table)),
                        (tenant, workspace))
            snap[table] = cur.fetchone()
    snap["__blobs__"] = dict(store.blobs.data)
    return snap


def assert_no_durable_change(before, after):
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert not changed, f"read-only request changed durable state: {changed}"


def revision_view(client, workspace, revision_id):
    resp = client.get(f"/v2/sources/revisions/{revision_id}", params={"workspace_id": workspace})
    assert resp.status_code == 200, resp.text
    return resp.json()


def rev_entry(history_body, revision_id):
    rows = [r for r in history_body["revisions"] if r["revision_id"] == revision_id]
    assert len(rows) == 1, (revision_id, history_body["revisions"])
    return rows[0]


def ids_of(items, key="artifact_id"):
    return sorted(i[key] for i in items)


# ---------------------------------------------------------------------- hermetic seams

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def ai9_hermetic(monkeypatch, request):
    """(1) Route the blob seam `infra.v2_source_routes._blob_port` (used by
    restore preview/approve and the source routes) to the test store's
    in-memory blobs: un-patched, `RIPPLE_V2_BLOB_BUCKET=test-bucket` (set by the
    shared `setup` fixture) makes it build a REAL GCS client and issue a live
    request with the developer's credentials. (2) Belt and braces: any
    socket connect to a non-loopback host raises, so no AI9 test can reach a
    real network or use credentials even if a future seam is missed."""
    import socket
    if "setup" in request.fixturenames:
        import infra.v2_source_routes as src
        blobs = request.getfixturevalue("setup")["store"].blobs
        monkeypatch.setattr(src, "_blob_port", lambda: blobs)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    real_connect = socket.socket.connect

    def _guard(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in _LOOPBACK:
            raise RuntimeError(f"AI9 tests must not open a network connection to {host!r}")
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", _guard)
    yield
