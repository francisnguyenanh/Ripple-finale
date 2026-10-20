"""A booked cost adjustment (usage the provider charged but no attempt recorded, priced fail-closed) is part of THE ledger every reservation sums, so
every live entry point is held to the allowance net of it. Disposable PostgreSQL; no provider. Owner request 2026-10-18 (finding F6)."""
from __future__ import annotations

import threading
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_postgres import PostgresV2Store, StoreConflict
from infra.worker.budget_config import BudgetExceeded
from tests.ai2t03.test_worker_terminal_ack_and_budget_round4 import DSN, MemoryBlobs, project  # noqa: F401  (project is a fixture)


def _reserve(store, tenant, workspace, estimate, cap="1.00"):
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, "job-" + uuid4().hex, "change", {}, expected_version=head.version, idempotency_key=uuid4().hex,
                     budget_reservation=(Decimal(estimate), Decimal(cap)))


def _spent(tenant, workspace):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FILTER (WHERE job_id LIKE 'adjustment:%%') FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s",
                    (tenant, workspace))
        return cur.fetchone()


def test_the_adjustment_is_summed_by_every_reservation_so_the_direct_job_path_cannot_pass_the_cap(project):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    got = store.record_cost_adjustment(t, w, "ADJ-20261018-T1", "0.50", reason="three discarded calls priced at the ceiling")
    assert got == {"created": True, "cost_usd": "0.50", "job_id": "adjustment:ADJ-20261018-T1"}
    with pytest.raises(BudgetExceeded):
        _reserve(store, t, w, "0.60")                                                      # 0.50 adjusted + 0.60 > 1.00: refused, no phantom row
    assert _spent(t, w) == (Decimal("0.50"), 1)
    _reserve(store, t, w, "0.40")                                                          # 0.90 <= 1.00
    assert _spent(t, w)[0] == Decimal("0.90")


def test_the_same_adjustment_is_booked_once_and_never_rewritten_after_a_restart(project):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    store.record_cost_adjustment(t, w, "ADJ-20261018-T2", "0.11376", reason="F6")
    restarted = PostgresV2Store(DSN, MemoryBlobs())                                        # a new process: the adjustment is durable, not in memory
    assert restarted.record_cost_adjustment(t, w, "ADJ-20261018-T2", "0.11376", reason="F6")["created"] is False
    with pytest.raises(StoreConflict):
        restarted.record_cost_adjustment(t, w, "ADJ-20261018-T2", "0.05", reason="smaller")    # history is never lowered
    assert _spent(t, w) == (Decimal("0.11376"), 1)
    for bad in (("ADJ-x", "0.1"), ("ADJ-20261018-T3", "0"), ("ADJ-20261018-T3", "-1"), ("ADJ-20261018-T3", "NaN")):
        with pytest.raises(ValueError):
            store.record_cost_adjustment(t, w, bad[0], bad[1], reason="r")


def _race(n, fn):
    barrier, out = threading.Barrier(n), []

    def run():
        barrier.wait(timeout=5)
        out.append(fn())
    threads = [threading.Thread(target=run) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
        assert not th.is_alive()
    return out


def test_racing_bookings_of_one_adjustment_write_one_row_and_racing_reservations_after_it_admit_exactly_one(project):
    """An adjustment is a cost already incurred: it is booked even if it passes the cap (it is never refused); what it changes is every later admission."""
    t, w = project["tenant"], project["workspace"]
    created = _race(2, lambda: PostgresV2Store(DSN, MemoryBlobs()).record_cost_adjustment(t, w, "ADJ-20261018-T4", "0.30", reason="race")["created"])
    assert sorted(created) == [False, True] and _spent(t, w) == (Decimal("0.30"), 1)

    def reserve():
        try:
            _reserve(PostgresV2Store(DSN, MemoryBlobs()), t, w, "0.40")
            return "admitted"
        except BudgetExceeded:
            return "refused"
        except Exception as exc:                                                            # a head-version race is also a refusal, never an overspend
            return type(exc).__name__
    outcomes = _race(2, reserve)
    spent, adj_rows = _spent(t, w)
    assert outcomes.count("admitted") <= 1 and adj_rows == 1 and spent == Decimal("0.30") + Decimal("0.40") * outcomes.count("admitted") and spent <= Decimal("1.00")
    if outcomes.count("admitted") == 0:                                                     # both lost the head race: a sequential retry still admits exactly one
        _reserve(PostgresV2Store(DSN, MemoryBlobs()), t, w, "0.40")
    with pytest.raises(BudgetExceeded):
        _reserve(PostgresV2Store(DSN, MemoryBlobs()), t, w, "0.40")                        # 0.70 + 0.40 > 1.00
    assert _spent(t, w) == (Decimal("0.70"), 1)
    big = PostgresV2Store(DSN, MemoryBlobs()).record_cost_adjustment(t, w, "ADJ-20261018-T5", "0.50", reason="sunk cost past the cap")
    assert big["created"] is True and _spent(t, w)[0] == Decimal("1.20")                   # booked, never hidden; nothing more is admitted
    with pytest.raises(BudgetExceeded):
        _reserve(PostgresV2Store(DSN, MemoryBlobs()), t, w, "0.01")
