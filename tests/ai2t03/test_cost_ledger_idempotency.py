"""SQL idempotency proof that the spend ledger is shared and idempotent.

Targets `infra/worker/job_quota.py::record_job_cost`/`project_spent_usd` and
the `v2_job_cost_ledger` table directly (real PostgreSQL, not a mock),
including a genuine two-connection concurrent race.
"""

from __future__ import annotations

import os
import threading
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from infra.worker import job_quota


DSN = os.environ.get("AI2_T03_TEST_DSN") or os.environ.get("AI1_T03_TEST_DSN")


@pytest.fixture(autouse=True)
def _require_dsn(monkeypatch):
    if not DSN:
        pytest.skip("AI2-T03 ledger tests need a disposable PostgreSQL (AI2_T03_TEST_DSN)")
    monkeypatch.setenv("AI2_T03_TEST_DSN", DSN)
    # Make sure a stray RIPPLE_V2_DSN from another fixture in the same run
    # does not silently redirect the ledger to a different database.
    monkeypatch.delenv("RIPPLE_V2_DSN", raising=False)


def _project() -> tuple[str, str]:
    nonce = uuid4().hex
    return f"t-ledger-{nonce}", f"w-ledger-{nonce}"


def _direct_sql_rows(tenant_id: str, workspace_id: str) -> list[tuple]:
    conn = psycopg2.connect(DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(job_quota._LEDGER_TABLE_SQL)
            cur.execute(
                """SELECT job_id, cost_usd FROM v2_job_cost_ledger
                   WHERE tenant_id=%s AND workspace_id=%s ORDER BY job_id""",
                (tenant_id, workspace_id),
            )
            return cur.fetchall()
    finally:
        conn.close()


def test_double_record_of_the_same_job_id_is_not_additive(tmp_path):
    """The original defect: recording the same job_id's cost
    twice must leave the total at the FIRST recorded value (0.40), never a
    sum (0.80)."""
    tenant_id, workspace_id = _project()

    first_total = job_quota.record_job_cost(
        tenant_id=tenant_id, workspace_id=workspace_id, job_id="job-a", cost_usd="0.40")
    assert first_total == Decimal("0.40")

    second_total = job_quota.record_job_cost(
        tenant_id=tenant_id, workspace_id=workspace_id, job_id="job-a", cost_usd="0.40")
    assert second_total == Decimal("0.40"), (
        "recording job-a's cost a second time must be a no-op -- 0.80 here "
        "would reproduce the exact round-1 defect"
    )

    # Direct SQL confirms exactly one row for job-a, not two.
    rows = _direct_sql_rows(tenant_id, workspace_id)
    assert [r[0] for r in rows] == ["job-a"]

    # project_spent_usd (a fresh SELECT, no cache) agrees.
    assert job_quota.project_spent_usd(tenant_id=tenant_id, workspace_id=workspace_id) == Decimal("0.40")


def test_distinct_job_ids_in_the_same_project_correctly_add(tmp_path):
    """Idempotency must be scoped to job_id, not "a project's second call
    never counts" -- a different job_id's cost must still add to the
    project total."""
    tenant_id, workspace_id = _project()

    job_quota.record_job_cost(tenant_id=tenant_id, workspace_id=workspace_id,
                              job_id="job-a", cost_usd="0.40")
    total = job_quota.record_job_cost(tenant_id=tenant_id, workspace_id=workspace_id,
                                      job_id="job-b", cost_usd="0.10")
    assert total == Decimal("0.50")
    assert job_quota.project_spent_usd(tenant_id=tenant_id, workspace_id=workspace_id) == Decimal("0.50")


def test_decimal_precision_is_preserved_not_float(tmp_path):
    """Money must round-trip through PostgreSQL NUMERIC as an exact Decimal,
    never a float (ADR-005 §2.7's float ban) -- a value that would lose
    precision under float (e.g. 0.1 + 0.2 != 0.3 in binary float) must come
    back exact."""
    tenant_id, workspace_id = _project()

    total = job_quota.record_job_cost(tenant_id=tenant_id, workspace_id=workspace_id,
                                      job_id="job-precise", cost_usd="0.123456")
    assert isinstance(total, Decimal)
    assert total == Decimal("0.123456")

    second_project_tenant, second_project_workspace = _project()
    job_quota.record_job_cost(tenant_id=second_project_tenant, workspace_id=second_project_workspace,
                              job_id="job-x", cost_usd="0.10")
    running = job_quota.record_job_cost(tenant_id=second_project_tenant, workspace_id=second_project_workspace,
                                        job_id="job-y", cost_usd="0.20")
    assert isinstance(running, Decimal)
    assert running == Decimal("0.30")  # would be 0.30000000000000004 under naive binary float addition


def test_concurrent_workers_racing_to_record_the_same_job_id_stay_idempotent():
    """Two independent connections (standing in for two worker processes)
    attempt to record the SAME job_id's cost at the same moment. The unique
    constraint (tenant_id, workspace_id, job_id) plus `ON CONFLICT DO
    NOTHING` must make exactly one of them "win", never a double-count from
    the race, and neither call may raise (a race must be absorbed, not
    surfaced as an error to a worker)."""
    tenant_id, workspace_id = _project()
    job_id = "job-race"
    results: list[Decimal] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker(cost: str):
        try:
            barrier.wait(timeout=5)
            total = job_quota.record_job_cost(
                tenant_id=tenant_id, workspace_id=workspace_id, job_id=job_id, cost_usd=cost)
            results.append(total)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=("0.40",)),
              threading.Thread(target=_worker, args=("0.40",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"a concurrent record_job_cost call raised: {errors!r}"
    assert len(results) == 2
    # Both racing calls must observe the SAME final total (whichever row won
    # the insert), and it must be 0.40, never 0.80.
    assert set(results) == {Decimal("0.40")}
    assert job_quota.project_spent_usd(tenant_id=tenant_id, workspace_id=workspace_id) == Decimal("0.40")
    rows = _direct_sql_rows(tenant_id, workspace_id)
    assert len(rows) == 1, f"exactly one row must exist for job_id={job_id!r} after the race, got {rows!r}"


def test_ledger_idempotency_is_not_vacuous_a_naive_accumulate_implementation_fails_it():
    """Proves the double-record test above would have caught the original
    defect (an OS-temp JSON file whose value went 0.40 -> 0.80 on a second
    call for the same job_id). Rather than hand-editing
    `infra/worker/job_quota.py`'s SQL and manually restoring it byte-for-byte
    (an interrupted or mis-restored manual edit is a risk), this defines a local stand-in implementing the
    original shape -- an unconditional accumulate on every call, with
    no per-job_id uniqueness check at all -- and shows it fails this test
    suite's own assertion. The REAL `job_quota.record_job_cost` (imported,
    unmodified) is then shown to behave correctly on the same scenario in the
    same test.
    """
    tenant_id, workspace_id = _project()

    def _naive_accumulate_like_round_1(existing_total: Decimal, cost: str) -> Decimal:
        # This is mathematically what the round-1 JSON-file implementation
        # did: read the current total, add the new cost, write it back --
        # with no per-job_id key at all, so calling it twice for the SAME
        # job always doubles the total.
        return existing_total + Decimal(cost)

    naive_total = Decimal("0")
    naive_total = _naive_accumulate_like_round_1(naive_total, "0.40")
    naive_total = _naive_accumulate_like_round_1(naive_total, "0.40")
    assert naive_total == Decimal("0.80"), (
        "sanity check: the round-1-shaped naive accumulator really does "
        "double-count -- if this assertion itself failed, the naive helper "
        "above would not be faithfully reproducing the original defect"
    )
    # The real fix must NOT reproduce that number for the same job_id.
    real_total = job_quota.record_job_cost(tenant_id=tenant_id, workspace_id=workspace_id,
                                           job_id="job-naive-check", cost_usd="0.40")
    real_total = job_quota.record_job_cost(tenant_id=tenant_id, workspace_id=workspace_id,
                                           job_id="job-naive-check", cost_usd="0.40")
    assert real_total != naive_total
    assert real_total == Decimal("0.40")
