"""REL-05 (audit 2026-10-30): a privileged business commit re-checks the actor's `knowledge.confirm` inside its own transaction, so a confirmer revoked
after the route resolved the request's authority cannot publish knowledge or commit a PENDING_HUMAN snapshot. Real stores on PostgreSQL; the
mid-transaction race uses the execute hook of `test_ai2_t02_atomicity.py`."""
from __future__ import annotations

import threading
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters import v2_business
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store, StaleAuthorityError

from .test_ai2_t02_atomicity import DSN, MemoryBlobs, _exec, install_execute_hook


@pytest.fixture
def setup():
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    nonce = uuid4().hex
    tenant, project, workspace = f"t-{nonce}", f"p-{nonce}", f"w-{nonce}"
    admin, confirmer = f"admin-{nonce}", f"confirmer-{nonce}"
    storage = PostgresV2Store(DSN, MemoryBlobs())
    storage.migrate()
    storage.create_workspace(tenant, project, workspace)
    digest = storage.stage_blob(b"seed artifact")
    storage.publish_source(tenant, workspace, "rev-0", {"artifact": digest}, expected_version=0, operation="source.import.commit", idempotency_key=nonce)
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, project, workspace, admin, f"cred-{nonce}-admin-distinct")
    identities.create_account(confirmer, f"cred-{nonce}-confirmer-distinct")
    identities.grant_membership(tenant, project, workspace, confirmer, ("knowledge_owner",))
    return locals()


def _head(storage, tenant, workspace):
    return storage.head(tenant, workspace)


def _commit_pending(storage, tenant, workspace, actor):
    head = _head(storage, tenant, workspace)
    return v2_business.commit_snapshot(storage, tenant, workspace, snapshot_id="snap-" + uuid4().hex, actor_id=actor, lifecycle_state="PENDING_HUMAN",
                                       rules=[], expected_version=head.version, expected_business_revision=0, source_revision_id=head.source_revision_id,
                                       km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch, idempotency_key="k-" + uuid4().hex)


def test_confirmer_with_authority_commits_and_publishes(setup):
    storage, tenant, workspace, confirmer = setup["storage"], setup["tenant"], setup["workspace"], setup["confirmer"]
    assert _commit_pending(storage, tenant, workspace, confirmer).version >= 0
    head = _head(storage, tenant, workspace)
    r = storage.publish_knowledge(tenant, workspace, "km-" + uuid4().hex, head.source_revision_id, head.decision_epoch, expected_version=head.version,
                                  idempotency_key="pub-" + uuid4().hex, actor_id=confirmer)
    assert r.version == head.version + 1


def test_revoked_confirmer_is_refused_inside_the_transaction(setup):
    storage, identities = setup["storage"], setup["identities"]
    tenant, project, workspace, confirmer = setup["tenant"], setup["project"], setup["workspace"], setup["confirmer"]
    identities.revoke_membership(tenant, project, workspace, confirmer)
    with pytest.raises(StaleAuthorityError):
        _commit_pending(storage, tenant, workspace, confirmer)
    head = _head(storage, tenant, workspace)
    with pytest.raises(StaleAuthorityError):
        storage.publish_knowledge(tenant, workspace, "km-" + uuid4().hex, head.source_revision_id, head.decision_epoch, expected_version=head.version,
                                  idempotency_key="pub-" + uuid4().hex, actor_id=confirmer)
    assert _head(storage, tenant, workspace).version == head.version, "nothing committed"
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
        assert cur.fetchone()[0] == 0


def test_revoke_that_lands_after_the_route_check_still_blocks_the_publish(setup, monkeypatch):
    """The race the finding names: the route already checked the permission; the revoke commits while the publish transaction is open (after its
    first statement, before the recheck locks the membership row). The recheck inside the transaction refuses."""
    storage, identities = setup["storage"], setup["identities"]
    tenant, project, workspace, confirmer = setup["tenant"], setup["project"], setup["workspace"], setup["confirmer"]
    head = _head(storage, tenant, workspace)
    reached, revoked = threading.Event(), threading.Event()

    def hook(real_cursor, query, vars):
        if isinstance(query, str) and "FROM v2_accounts WHERE account_id" in query and not reached.is_set():
            reached.set()                                # the transaction is open and about to recheck: the revoke lands now
            revoked.wait(timeout=10)
        return _exec(real_cursor, query, vars)

    install_execute_hook(monkeypatch, hook)
    out = {}
    store_t = PostgresV2Store(DSN, MemoryBlobs())

    def run():
        try:
            store_t.publish_knowledge(tenant, workspace, "km-" + uuid4().hex, head.source_revision_id, head.decision_epoch, expected_version=head.version,
                                      idempotency_key="pub-" + uuid4().hex, actor_id=confirmer)
            out["r"] = "published"
        except StaleAuthorityError as exc:
            out["r"], out["e"] = "blocked", str(exc)

    t = threading.Thread(target=run)
    t.start()
    assert reached.wait(10)
    identities.revoke_membership(tenant, project, workspace, confirmer)
    revoked.set()
    t.join(15)
    assert not t.is_alive() and out.get("r") == "blocked", out
    assert _head(storage, tenant, workspace).version == head.version
