"""AH-03: revocation racing a privileged business publication (the Final commit of an approved change), with two real connections on the throwaway PostgreSQL.

Linearization point: the publishing transaction's `_recheck_actor_authority`, which takes `FOR UPDATE` on the account row, then the membership row, then (after it)
the workspace head. `revoke_membership` deletes the membership row, then updates the workspace row. Both writers therefore meet on the membership row FIRST:
  * revocation first -> the publication waits on the membership row, then finds it gone and is refused; no revision is written;
  * publication first -> the revocation waits until the publication commits; the publication stands, later reads and actions of that person are refused.
Before AH-03 the Final adapters locked the workspace head BEFORE the membership row (the reverse order of the revocation): the two transactions deadlocked and the
database aborted one of them. No model is called."""
from __future__ import annotations

import threading
import time

import psycopg2
import pytest

from infra.adapters import v2_business, v2_business_commit
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store, StaleAuthorityError, StaleVersion, StoreConflict
from tests.final_loop.test_chg05_commit import approved
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)


def _revision(q):
    return v2_business.business_view(q.env.store, q.env.tenant, q.env.ws)["business_head"]["business_revision"]


def _commit_in_thread(q, cid, binding, out):
    def run():
        store = PostgresV2Store(q.env.dsn, q.env.blobs)                                              # its own connections
        try:
            out["result"] = ("ok", v2_business_commit.commit_change(store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding))
        except (StaleAuthorityError, StaleVersion, StoreConflict, BusinessKnowledgeRefused) as exc:
            out["result"] = ("refused", type(exc).__name__)
        except psycopg2.Error as exc:                                                                 # a deadlock would land here
            out["result"] = ("db_error", type(exc).__name__)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_revocation_that_wins_the_membership_row_refuses_the_publication(qa_env):
    q = qa_env
    cid, _approver, binding = approved(q)
    before = _revision(q)
    revoker = psycopg2.connect(q.env.dsn)
    try:
        cur = revoker.cursor()
        cur.execute("""DELETE FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s RETURNING roles""", (q.env.tenant, q.env.ws, q.env.admin))
        assert cur.fetchone() is not None                                                            # the revocation holds the membership row now
        out: dict = {}
        t = _commit_in_thread(q, cid, binding, out)
        time.sleep(1.5)
        assert t.is_alive() and "result" not in out                                                  # the publication waits on the membership row
        cur.execute("""UPDATE v2_workspaces SET policy_version=(policy_version::bigint + 1)::text WHERE tenant_id=%s AND workspace_id=%s""", (q.env.tenant, q.env.ws))
        revoker.commit()                                                                             # the revocation's second lock (workspace) did not deadlock
    finally:
        revoker.close()
    t.join(30)
    assert out.get("result") == ("refused", "StaleAuthorityError"), out
    assert _revision(q) == before                                                                    # no authorized effect


def test_publication_that_wins_commits_and_the_revocation_then_stops_later_access(qa_env, monkeypatch):
    q = qa_env
    cid, _approver, binding = approved(q)
    before = _revision(q)
    original = PostgresV2Store._recheck_actor_authority
    checked, revoke_done = threading.Event(), threading.Event()
    state = {}

    def slow_recheck(self, cur, tenant_id, workspace_id, actor_id, permission):
        original(self, cur, tenant_id, workspace_id, actor_id, permission)
        if actor_id == q.env.admin and not checked.is_set():
            checked.set()
            time.sleep(1.5)                                                                          # hold the locks while the revocation tries to run
            state["revoke_finished_inside"] = revoke_done.is_set()

    monkeypatch.setattr(PostgresV2Store, "_recheck_actor_authority", slow_recheck)
    out: dict = {}
    t = _commit_in_thread(q, cid, binding, out)
    assert checked.wait(20)

    def revoke():
        PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, q.env.admin)
        revoke_done.set()
    r = threading.Thread(target=revoke, daemon=True)
    r.start()
    t.join(30)
    r.join(30)
    assert out.get("result", ("",))[0] == "ok", out
    assert state["revoke_finished_inside"] is False                                                  # the revocation waited for the publication
    assert revoke_done.is_set() and _revision(q) == before + 1
    monkeypatch.setattr(PostgresV2Store, "_recheck_actor_authority", original)
    later = q.admin.get(f"/v2/business/changes/{cid}/journey", params={"workspace_id": q.env.ws})  # the revoked person can no longer read it
    assert later.status_code in (401, 403, 404), later.status_code
    with pytest.raises(StaleAuthorityError):                                                         # every privileged write's gate refuses that person now
        with q.env.store._tx() as cur:
            q.env.store._recheck_actor_authority(cur, q.env.tenant, q.env.ws, q.env.admin, "knowledge.confirm")
