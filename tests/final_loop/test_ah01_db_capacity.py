"""AH-01 on the throwaway PostgreSQL: the bounded pool contains failures without leaking slots or inventing commits, and a provider call through the REAL worker
holds no caller-owned connection (with a pool of ONE, another transaction completes while the provider is blocked). The provider is a synthetic stub."""
from __future__ import annotations

import threading
import time

import psycopg2
import pytest

from infra import db_pool
from infra.worker import explain
from tests.final_loop.test_request_assistant_jobs import answer, ask, fact_id, ra_env, read, thread  # noqa: F401  (ra_env is a fixture)
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.pitch_brief.conftest import DSN, drain


@pytest.fixture
def pool_of(monkeypatch):
    def make(n, wait="3"):
        monkeypatch.setenv("RIPPLE_DB_POOL_MAX", str(n))
        monkeypatch.setenv("RIPPLE_DB_POOL_WAIT", wait)
        db_pool.reset()
    yield make
    db_pool.reset()


def _db_or_skip():
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("needs the disposable PostgreSQL (PITCH_BRIEF_TEST_DSN)")


def test_pool_contains_exceptions_dirty_returns_and_failed_close_without_leaking_or_committing(pool_of):
    _db_or_skip()
    pool_of(1)
    with db_pool.connection(DSN) as c, c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS ah01_probe (v int)")
        cur.execute("DELETE FROM ah01_probe")

    class Boom(BaseException):
        pass

    with pytest.raises(Boom):                                   # a BaseException inside an open transaction
        with db_pool.connection(DSN) as c:
            cur = c.cursor()
            cur.execute("INSERT INTO ah01_probe VALUES (1)")
            raise Boom()
    with pytest.raises(ValueError):                             # an ordinary exception, same shape
        with db_pool.connection(DSN) as c:
            c.cursor().execute("INSERT INTO ah01_probe VALUES (2)")
            raise ValueError("x")
    with db_pool.connection(DSN) as c:                          # a connection returned dirty without an exception: discarded, never committed
        c.cursor().execute("INSERT INTO ah01_probe VALUES (3)")

    class BadClose:
        closed = False

        class info:                                             # noqa: N801
            transaction_status = 2                              # INTRANS: not idle

        def close(self):
            raise RuntimeError("close failed")

    pool = db_pool._pool_for(DSN, 1)
    pool.slots.acquire()
    with pool.guard:
        pool.in_use += 1
    pool.release(BadClose())                                    # a failed close still frees the slot
    s = db_pool.stats(DSN)
    assert s["in_use"] == 0 and s["idle"] <= 1
    with db_pool.connection(DSN) as c, c, c.cursor() as cur:   # the one slot is usable again and nothing above was committed by the pool
        cur.execute("SELECT count(*) FROM ah01_probe")
        assert cur.fetchone()[0] == 0
        cur.execute("DROP TABLE ah01_probe")


def test_saturated_pool_refuses_in_bounded_time_and_recovers(pool_of):
    _db_or_skip()
    pool_of(1, wait="0.5")
    holder = db_pool._pool_for(DSN, 1).acquire()
    t0 = time.monotonic()
    with pytest.raises(db_pool.PoolBusy) as busy:
        with db_pool.connection(DSN):
            pass
    assert time.monotonic() - t0 < 2 and "password" not in str(busy.value) and "host=" not in str(busy.value)
    assert db_pool.stats(DSN)["timeouts"] == 1 and db_pool.busy_counts()["timeouts"] >= 1              # AH-01.6 counters, no DSN
    db_pool._pool_for(DSN, 1).release(holder)
    with db_pool.connection(DSN) as c, c, c.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_a_blocked_provider_holds_no_connection_with_a_pool_of_one(ra_env, pool_of):
    q = ra_env
    cid, approver = prepared(q)
    tid = thread(q, approver, cid)["thread_id"]
    entered, release = threading.Event(), threading.Event()

    def fn(prompt):
        entered.set()
        release.wait(20)
        return answer(blocks=[("The current stage is shown in the facts.", [fact_id(prompt, "state")], "fact")])

    from tests.final_loop.test_qry03_qa import Model
    model = Model(fn)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    assert ask(q, approver, tid, "Why is it waiting?", "k-ah01-1", 0).status_code == 200
    pool_of(1, wait="3")                                        # from here on, ONE connection for the whole process
    worker = threading.Thread(target=drain, args=(q.env,), daemon=True)
    worker.start()
    assert entered.wait(20), "the provider was not reached"
    t0 = time.monotonic()
    with db_pool.connection(DSN) as c, c, c.cursor() as cur:   # an independent transaction while the provider call is blocked
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s", (q.env.tenant,))
        assert cur.fetchone()[0] >= 1
    assert time.monotonic() - t0 < 3
    assert db_pool.stats(DSN)["in_use"] == 0                    # nothing else held the one slot during the provider wait
    release.set()
    worker.join(30)
    assert not worker.is_alive()
    st = read(q, approver, tid).json()
    assert st["messages"][-1]["state"] != "pending"             # the job finished through the one-slot pool (no nested-borrow deadlock)
