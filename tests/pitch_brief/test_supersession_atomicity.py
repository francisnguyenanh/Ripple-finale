"""Claim 3, the race window closed: "the draft behind this work was revised" is decided INSIDE the transaction that writes (publish, approve decision, job insert),
on the same cursor, under the same workspace-row lock that every job creation also takes. Controlled interleavings, no sleeps to hope for a race: one side holds the
workspace lock in an open transaction, the other side is observed BLOCKED on that lock (pg_stat_activity), then the first side commits.
Real store, real executor, real Postgres, STUB provider.
  * a revision commits BEFORE the apply's publish transaction gets the lock -> nothing is published, the apply fails `draft_superseded`   -> test 1
  * an apply publishes FIRST (the revise is blocked behind it) -> the publish stays as history, the revise is created afterwards         -> test 2
  * two revisions of the same draft at once -> exactly one is created, the other is refused                                            -> test 3
  * an approve decision cannot be recorded after the revision committed (and leaves no row)                                            -> test 4
In tests 1, 3, 4 nothing half-done is left: no revision, no decision row, no success state."""
from __future__ import annotations

import threading
import time

import pytest

from infra import v2_lineage
from infra.worker import apply as apply_mod
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import _grant, _to_approval, chain_to_proposal


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _waiting_on_a_lock(env, timeout=20.0) -> bool:
    """Event-driven: true as soon as some backend is observed waiting for a row lock (bounded wait, not a guess about timing)."""
    import psycopg2
    end = time.time() + timeout
    conn = psycopg2.connect(env.dsn)
    conn.autocommit = True
    n = 0
    try:
        with conn.cursor() as cur:
            while time.time() < end:
                n += 1
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND pid <> pg_backend_pid()")
                got = cur.fetchone()[0]
                _waiting_on_a_lock.last = (n, got)
                if got > 0:
                    return True
                time.sleep(0.05)
    finally:
        conn.close()
    return False


_OPEN: list = []


@pytest.fixture(autouse=True)
def _release_held_locks():
    """A failing assert must never leave a transaction holding the workspace lock (it would hang the whole run): roll back whatever is still open."""
    yield
    for h in _OPEN:
        h.rollback()
    _OPEN.clear()


class HeldRevision:
    """An open transaction that holds the workspace-row lock and has inserted a revision of `draft` but not committed it."""

    def __init__(self, env, intent, draft, job_id="draft-rev-held"):
        self.env, self.job_id = env, job_id
        self._cm = env.store._tx()
        self.cur = self._cm.__enter__()
        self.open = True
        _OPEN.append(self)
        head = env.store._lock_head(self.cur, env.tenant, env.ws)
        env.store._create_job_core(self.cur, env.tenant, env.ws, job_id, "change.draft", {"intent_id": intent, "revises_draft_job_id": draft},
                                   head=head, expected_version=head.version, idempotency_key=f"k-{job_id}", requested_by=env.req)

    def commit(self):
        self.open = False
        self._cm.__exit__(None, None, None)

    def rollback(self):
        if self.open:
            self.open = False
            try:
                self._cm.__exit__(RuntimeError, RuntimeError("test cleanup"), None)
            except RuntimeError:
                pass


def _revisions(env) -> int:
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, env.ws))
        return cur.fetchone()[0]


def _approved_and_queued_apply(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c)
    approver = _grant(env, "change_approver")
    ok = approver.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "ok", "expected_approval_digest": appr["approval_digest"]})
    assert ok.status_code == 200, ok.text
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 200, r.text
    return c, aj, appr, apj


def test_a_revision_that_commits_before_the_publish_transaction_gets_the_lock_publishes_nothing(env, ch, monkeypatch):
    c, aj, appr, apj = _approved_and_queued_apply(env, ch, monkeypatch)
    monkeypatch.setattr(apply_mod, "_assert_draft_current", lambda ctx, pid: None)       # prove the IN-TRANSACTION check alone: the early pre-check is switched off
    head0 = env.store.head(env.tenant, env.ws).source_revision_id
    revs0 = _revisions(env)
    entered, proceed = threading.Event(), threading.Event()
    real = env.store.publish_source

    def spy(*a, **kw):
        entered.set()                                                                    # the apply is past every check outside the transaction and about to open its publish transaction
        assert proceed.wait(30)
        return real(*a, **kw)
    monkeypatch.setattr(env.store, "publish_source", spy)
    t = threading.Thread(target=drain, args=(env,), daemon=True)
    t.start()
    assert entered.wait(20), "the apply never reached its publish transaction"
    held = HeldRevision(env, c.intent, c.draft)                                          # the revision is written, not yet committed; the workspace lock is held
    proceed.set()                                                                        # the apply now opens its publish transaction and must wait for that lock
    assert _waiting_on_a_lock(env), "the publish transaction was not observed waiting on the workspace lock"
    held.commit()                                                                         # now the revision commits; the publish transaction proceeds
    t.join(60)
    assert not t.is_alive()
    body = job_body(ch.admin, env, apj)
    assert body["execution_status"] == "FAILED" and "draft_superseded" in str(body), body
    assert env.store.head(env.tenant, env.ws).source_revision_id == head0 and _revisions(env) == revs0     # nothing published, no half revision
    assert v2_lineage.superseding_draft(env.store, env.tenant, env.ws, c.draft) == held.job_id
    assert job_body(ch.admin, env, aj)["execution_status"] == "COMPLETED"                                  # the earlier approval bundle is history, untouched


def test_an_apply_that_publishes_first_stays_as_history_and_the_revise_is_created_after_it(env, ch, monkeypatch):
    c, aj, appr, apj = _approved_and_queued_apply(env, ch, monkeypatch)
    real = env.store.publish_source
    result: dict = {}

    def revise():
        from infra.adapters.v2_postgres import StaleVersion
        payload = {"intent_id": c.intent, "revises_draft_job_id": c.draft}
        try:
            for attempt in (1, 2):                                                          # a create that races a publish is refused as stale (CAS on the head version); the caller re-reads and retries
                try:
                    result["job"] = env.store.create_job(
                        env.tenant, env.ws, "draft-rev-late", "change.draft", payload,
                        expected_version=env.store.head(env.tenant, env.ws).version, idempotency_key="k-late", requested_by=env.req,
                        locked_check=v2_lineage.job_guard("change.draft", payload, env.tenant, env.ws))
                    result["attempts"] = attempt
                    break
                except StaleVersion:
                    result["stale_first"] = True
        except Exception as exc:                                                          # noqa: BLE001
            result["error"] = exc
    thread = threading.Thread(target=revise, daemon=True)

    def hook():                                                                           # runs INSIDE the publish transaction, after its checks, before its commit
        thread.start()
        seen = _waiting_on_a_lock(env)
        if not seen:
            import psycopg2
            with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT pid, state, wait_event_type, wait_event, replace(left(query, 60), chr(10), chr(32)) FROM pg_stat_activity WHERE datname=current_database() AND pid <> pg_backend_pid()")
                rows = cur.fetchall()
            import json as _json
            open("dbg_act.json", "w", encoding="utf-8").write(_json.dumps(rows, default=str))
            raise RuntimeError("REVISE-NOT-BLOCKED result=" + repr({k: (type(v).__name__, str(v)[:200]) for k, v in result.items()}) + " ACT=" + repr(rows) + " alive=" + str(thread.is_alive()) + " last=" + str(getattr(_waiting_on_a_lock, "last", None)))

    def wrapped(*a, **kw):
        return real(*a, before_commit=hook, **kw)
    monkeypatch.setattr(env.store, "publish_source", wrapped)
    drain(env)
    thread.join(60)
    assert not thread.is_alive()
    assert "error" not in result and "job" in result, result
    body = job_body(ch.admin, env, apj)
    assert body["execution_status"] == "COMPLETED" and body["execution_result"]["published"] is True, {k: v for k, v in body.items() if "fail" in k or "error" in k or "reason" in k or k == "execution_status"}       # the commit that came first is kept
    head = env.store.head(env.tenant, env.ws)
    assert head.source_revision_id == body["execution_result"]["published_revision_id"]
    assert v2_lineage.superseding_draft(env.store, env.tenant, env.ws, c.draft) == "draft-rev-late"             # and the revision exists afterwards: the applied work is history


def test_two_revisions_of_the_same_draft_at_once_create_exactly_one(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    held = HeldRevision(env, c.intent, c.draft, job_id="draft-rev-first")
    out: dict = {}

    def second():
        payload = {"intent_id": c.intent, "revises_draft_job_id": c.draft}
        try:
            env.store.create_job(env.tenant, env.ws, "draft-rev-second", "change.draft", payload, expected_version=env.store.head(env.tenant, env.ws).version,
                                 idempotency_key="k-second", requested_by=env.req, locked_check=v2_lineage.job_guard("change.draft", payload, env.tenant, env.ws))
            out["created"] = True
        except v2_lineage.DraftSuperseded as exc:
            out["refused"] = str(exc)
    t = threading.Thread(target=second, daemon=True)
    t.start()
    assert _waiting_on_a_lock(env)
    held.commit()
    t.join(60)
    assert out.get("refused") and "already revised" in out["refused"] and "created" not in out, out
    with env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='change.draft' AND payload->>'revises_draft_job_id'=%s", (env.tenant, env.ws, c.draft))
        assert [r[0] for r in cur.fetchall()] == ["draft-rev-first"]


def test_an_approve_decision_is_not_recorded_after_the_revision_committed(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c)
    held = HeldRevision(env, c.intent, c.draft)
    out: dict = {}

    def decide():
        try:
            env.store.record_change_decision(
                env.tenant, env.ws, aj, actor_id=op_account(env), action="approve", reason="ok", approval_digest=appr["approval_digest"], self_approval=False,
                high_risk=False, idempotency_key=None, payload_hash=None, locked_check=v2_lineage.proposal_guard(env.tenant, env.ws, c.proposal))
            out["recorded"] = True
        except v2_lineage.DraftSuperseded as exc:
            out["refused"] = str(exc)
    t = threading.Thread(target=decide, daemon=True)
    t.start()
    assert _waiting_on_a_lock(env)
    held.commit()
    t.join(60)
    assert out.get("refused") and "recorded" not in out, out
    assert env.store.change_decisions(env.tenant, env.ws, aj) == []                          # no decision row, nothing to undo


def op_account(env):
    return env.admin
