"""Cancel / retry / repeated cancel and the operator reconcile keep the cost ledger honest: an incurred charge is never dropped, an unused reservation is released, nothing is deleted."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_postgres import StoreConflict
from tests.ai2t03.test_worker_terminal_ack_and_budget_round4 import DSN, project  # noqa: F401


def _create(store, tenant, ws, job_id, reserve="0.40"):
    head = store.head(tenant, ws)
    store.create_job(tenant, ws, job_id, "change", {}, expected_version=head.version, idempotency_key=f"c-{job_id}", budget_reservation=(Decimal(reserve), Decimal("50.00")))


def _row(tenant, ws, job_id):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT entry_state, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, ws, job_id))
        return cur.fetchone()


def test_cancel_after_a_failed_attempt_and_a_retry_keeps_the_charge_and_releases_the_rest(project):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    job = f"cx-retry-{uuid4().hex}"
    _create(store, t, w, job)
    store.finish_execution(t, w, job, "FAILED", reason="provider_timeout", attempt={"attempt_key": "a1", "outcome": "FAILED", "request_context": {}, "cost_usd": Decimal("0.04")})
    store.retry_job(t, w, job, budget_reservation=(Decimal("0.10"), Decimal("50.00")))
    assert _row(t, w, job)[0] == "RESERVED"
    store.request_cancel(t, w, job, "cancelled while waiting for its retry")
    assert _row(t, w, job) == ("SETTLED", Decimal("0.04"))            # the charge that happened stays, the retry reservation is gone


def test_cancel_while_running_settles_when_the_worker_finishes_and_keeps_the_usage(project):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    job = f"cx-run-{uuid4().hex}"
    _create(store, t, w, job)
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("UPDATE v2_jobs SET execution_status='RUNNING' WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (t, w, job))
    store.request_cancel(t, w, job, "cancel during the attempt")        # only a checkpoint is set: the attempt is in flight
    assert _row(t, w, job)[0] == "RESERVED"
    store.finish_execution(t, w, job, "CANCELLED", reason="cancelled during the attempt", attempt={"attempt_key": "a1", "outcome": "CANCELLED", "request_context": {}, "cost_usd": Decimal("0.03")})
    assert _row(t, w, job) == ("SETTLED", Decimal("0.03"))             # the provider usage already incurred is charged


def test_a_repeated_cancel_is_refused_and_changes_nothing(project):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    job = f"cx-twice-{uuid4().hex}"
    _create(store, t, w, job)
    store.request_cancel(t, w, job, "first")
    before = _row(t, w, job)
    with pytest.raises(StoreConflict):
        store.request_cancel(t, w, job, "second")
    assert _row(t, w, job) == before == ("SETTLED", Decimal("0"))


def test_reconcile_settles_only_terminal_jobs_keeps_charges_is_audited_and_idempotent(project):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    stuck0, stuck_c, live = (f"cx-{k}-{uuid4().hex}" for k in ("stuck0", "stuckc", "live"))
    for j in (stuck0, stuck_c, live):
        _create(store, t, w, j)
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:               # the legacy defect: a terminal job whose reservation was never released
        cur.execute("UPDATE v2_jobs SET status='CANCELLED', execution_status='CANCELLED' WHERE tenant_id=%s AND workspace_id=%s AND job_id=ANY(%s)", (t, w, [stuck0, stuck_c]))
        cur.execute("""INSERT INTO v2_job_attempts (tenant_id, workspace_id, job_id, attempt_key, outcome, cost_usd, request_context) VALUES (%s,%s,%s,'a1','FAILED',0.05,'{}')""", (t, w, stuck_c))
    dry = store.reconcile_terminal_reservations(t, w, actor_id="operator")
    assert {r["job_id"] for r in dry} == {stuck0, stuck_c} and _row(t, w, stuck0)[0] == "RESERVED"   # a dry run changes nothing; the QUEUED job is not listed
    done = store.reconcile_terminal_reservations(t, w, actor_id="operator", approval_ref="user message 2026-10-10", apply=True)
    assert len(done) == 2
    assert _row(t, w, stuck0) == ("SETTLED", Decimal("0")) and _row(t, w, stuck_c) == ("SETTLED", Decimal("0.05")) and _row(t, w, live)[0] == "RESERVED"
    assert store.reconcile_terminal_reservations(t, w, actor_id="operator", apply=True) == []        # idempotent
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='ledger.reconcile_reservations'", (t, w))
        assert cur.fetchone()[0] == 1                                                                # one audit row, only for the run that changed something
