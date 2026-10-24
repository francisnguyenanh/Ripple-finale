"""AH-01: synchronous DB units run off the event loop with bounded admission; overload is a bounded, explained 503 (no DSN); liveness needs no thread.
No database here: the DB unit is replaced by a blocking callable (the real-DB pool proofs are in tests/final_loop/test_ah01_db_capacity.py)."""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from infra import db_offload
from infra.db_pool import PoolBusy


def _run(coro):
    return asyncio.run(coro)


def test_a_blocked_db_unit_does_not_stop_the_event_loop(monkeypatch):
    monkeypatch.setenv("RIPPLE_DB_WORK_SLOTS", "2")
    gate = threading.Event()

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while not gate.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        unit = asyncio.create_task(db_offload.run_db(lambda: gate.wait(5) and "done"))
        await asyncio.sleep(0.3)
        progressed = ticks
        gate.set()
        assert await unit == "done"
        await t
        return progressed

    assert _run(main()) >= 10                      # the loop kept running while the synchronous unit was blocked


def test_admission_is_bounded_busy_after_the_wait_and_recovers(monkeypatch):
    monkeypatch.setenv("RIPPLE_DB_WORK_SLOTS", "1")
    monkeypatch.setenv("RIPPLE_DB_POOL_WAIT", "0.2")
    gate = threading.Event()

    async def main():
        first = asyncio.create_task(db_offload.run_db(gate.wait, 5))
        await asyncio.sleep(0.05)
        with pytest.raises(db_offload.DbWorkBusy) as busy:
            await db_offload.run_db(lambda: "never")
        assert isinstance(busy.value, PoolBusy) and "dsn" not in str(busy.value).lower()
        gate.set()
        await first
        assert await db_offload.run_db(lambda: "after") == "after"      # capacity recovered
        assert db_offload.stats()["in_use"] == 0

    _run(main())


def test_a_cancelled_request_keeps_its_slot_until_the_thread_really_ends(monkeypatch):
    monkeypatch.setenv("RIPPLE_DB_WORK_SLOTS", "1")
    monkeypatch.setenv("RIPPLE_DB_POOL_WAIT", "0.2")
    gate, ended = threading.Event(), threading.Event()

    def unit():
        gate.wait(5)
        ended.set()

    async def main():
        task = asyncio.create_task(db_offload.run_db(unit))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert db_offload.stats()["in_use"] == 1 and not ended.is_set()     # not released while the synchronous unit still runs
        with pytest.raises(db_offload.DbWorkBusy):
            await db_offload.run_db(lambda: "no")
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):                                    # the thread ends on its own time; the slot follows it
            if ended.is_set() and db_offload.stats()["in_use"] == 0:
                break
            await asyncio.sleep(0.01)
        assert ended.is_set() and db_offload.stats()["in_use"] == 0
        assert await db_offload.run_db(lambda: "again") == "again"

    _run(main())


def test_failures_inside_the_unit_release_the_slot_and_keep_the_original_error(monkeypatch):
    monkeypatch.setenv("RIPPLE_DB_WORK_SLOTS", "1")

    class Boom(BaseException):
        pass

    def bad():
        raise Boom("original")

    async def main():
        with pytest.raises(Boom, match="original"):
            await db_offload.run_db(bad)
        with pytest.raises(ValueError, match="plain"):
            await db_offload.run_db(lambda: (_ for _ in ()).throw(ValueError("plain")))
        assert db_offload.stats()["in_use"] == 0 and await db_offload.run_db(lambda: 1) == 1

    _run(main())


@pytest.fixture
def app_client(monkeypatch):
    from infra import judge
    return judge.app


def test_assistant_post_handler_is_offloaded_and_liveness_answers_meanwhile(app_client, monkeypatch):
    from infra import v2_request_assistant_routes as routes
    gate, entered = threading.Event(), threading.Event()

    def blocking_run(request, workspace_id, action, **kw):
        entered.set()
        gate.wait(5)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    monkeypatch.setattr(routes, "_run", blocking_run)

    async def main():
        transport = httpx.ASGITransport(app=app_client)
        async with httpx.AsyncClient(transport=transport, base_url="https://judge.test") as c:
            post = asyncio.create_task(c.post("/v2/request-assistant/threads/t1/messages", json={"workspace_id": "w"}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            t0 = time.monotonic()
            health = await c.get("/health")
            elapsed = time.monotonic() - t0
            gate.set()
            r = await post
            return health, elapsed, r

    health, elapsed, r = _run(main())
    assert health.status_code == 200 and elapsed < 1.0
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_pool_busy_is_a_bounded_503_with_retry_after_and_no_dsn(app_client, monkeypatch, caplog):
    from infra import v2_request_assistant_routes as routes

    def busy(*a, **kw):
        raise PoolBusy("no database connection free after 15s (pool of 4) host=10.0.0.1 password=secret dbname=prod")

    monkeypatch.setattr(routes, "_run", busy)

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client), base_url="https://judge.test") as c:
            return await c.post("/v2/request-assistant/threads/t1/messages", json={"workspace_id": "w"})

    r = _run(main())
    assert r.status_code == 503 and r.headers.get("retry-after") == "2" and r.json()["code"] == "service_busy"
    assert "password" not in r.text and "host=" not in r.text and "pool of" not in r.text
    logged = [rec.getMessage() for rec in caplog.records if rec.name == "ripple.capacity"]
    assert len(logged) == 1 and logged[0].startswith("service_busy kind=pool") and "timeouts" in logged[0]                # counters only
    assert "password" not in logged[0] and "host=" not in logged[0] and "t1" not in logged[0]
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_pool_busy_inside_the_identity_lookup_is_the_same_bounded_503(app_client, monkeypatch):
    from infra import v2_auth

    class BusyIdentity:
        def resolve_context(self, token):
            raise PoolBusy("no database connection free after 15s (pool of 4) host=10.0.0.1 password=secret")

    monkeypatch.setattr(v2_auth, "_store", lambda: BusyIdentity())

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client), base_url="https://judge.test", cookies={"ripple_v2": "x"}) as c:
            return await c.get("/v2/auth/me")

    r = _run(main())
    assert r.status_code == 503 and r.headers.get("retry-after") == "2" and r.json()["code"] == "service_busy"
    assert "password" not in r.text and "host=" not in r.text and r.headers.get("x-request-id")


def test_a_converted_sibling_route_runs_off_the_loop_and_keeps_its_body_parse(app_client, monkeypatch):
    """AH-01 siblings: an interview POST (one of the 109 routes moved to `db_route`) blocks in its synchronous unit; liveness still answers; the body reached it."""
    from infra import v2_interview_routes as routes
    gate, entered, seen = threading.Event(), threading.Event(), {}

    def blocking_shell(request, workspace_id, action, **kw):
        seen["workspace_id"], seen["thread"] = workspace_id, threading.current_thread().name
        entered.set()
        gate.wait(5)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    monkeypatch.setattr(routes, "_shell", blocking_shell)

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client), base_url="https://judge.test") as c:
            post = asyncio.create_task(c.post("/v2/business/interviews/s1/answer", json={"workspace_id": "ws-1", "text": "x"}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            t0 = time.monotonic()
            health = await c.get("/health")
            elapsed = time.monotonic() - t0
            gate.set()
            return health, elapsed, await post, threading.current_thread().name

    health, elapsed, r, loop_thread = _run(main())
    assert health.status_code == 200 and elapsed < 1.0
    assert r.status_code == 200 and seen["workspace_id"] == "ws-1" and seen["thread"] != loop_thread


def test_invalid_json_and_an_oversized_import_body_keep_their_previous_answers(monkeypatch):
    """The sync body readers behave like the async ones: invalid JSON -> {} (policy shell) / {} (bounded); a declared length above the limit -> None, unread."""
    from infra import v2_source_routes as src
    from infra.v2_policy_routes import _json_body_sync

    class R:
        def __init__(self, raw, length=None):
            self.headers = {"content-length": str(length if length is not None else len(raw or b""))}
            self._raw = raw
            self.state = type("S", (), {})()

        async def body(self):
            return self._raw

    async def read(req, cap):
        req.state.ripple_body = await db_offload._read_body(req, cap)
        return req

    bad = _run(read(R(b"{not json"), None))
    assert _json_body_sync(bad) == {} and src._bounded_json_sync(bad) == {}
    listy = _run(read(R(b"[1, 2]"), None))
    assert _json_body_sync(listy) == {} and src._bounded_json_sync(listy) == {}
    big = _run(read(R(b"", length=src.MAX_REQUEST_BYTES + 1), src.MAX_REQUEST_BYTES))
    assert src._bounded_json_sync(big) is None
    ok = _run(read(R(b'{"workspace_id": "w"}'), src.MAX_REQUEST_BYTES))
    assert src._bounded_json_sync(ok) == {"workspace_id": "w"} and db_offload.request_json(ok) == {"workspace_id": "w"}


def test_no_async_route_runs_synchronous_work_on_the_event_loop():
    """Regression guard over the source: an `async def` route may only await (the body, `run_db`, ...) — every other route is a sync `def` (thread pool) or a
    `db_route` function. Allowed exceptions: liveness and the legacy pitch surface, which do no SQL."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    allowed = {("infra/judge.py", "health"), ("infra/judge.py", "login"), ("infra/judge.py", "rpc"), ("app/main.py", "rpc_endpoint")}
    bad = []
    for path in sorted(list((root / "infra").rglob("*.py")) + list((root / "app").rglob("*.py"))):
        rel = str(path.relative_to(root)).replace("\\", "/")
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            routed = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post", "put", "patch", "delete") for d in fn.decorator_list)
            if not routed or (rel, fn.name) in allowed:
                continue
            awaited = [ast.unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Await)]
            if not any("run_db(" in a for a in awaited):
                bad.append(f"{rel}:{fn.lineno} {fn.name}")
    assert bad == [], bad


def test_readiness_reports_the_database_honestly_and_liveness_needs_none(app_client, monkeypatch):
    """AH-01: `/ready` checks the Final database off the loop: reachable -> 200 ok; unreachable -> 503 unavailable without DSN or driver text; not configured -> said so."""
    import psycopg2
    from infra import v2_policy_routes as pol

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            assert sql == "SELECT 1"

        def fetchone(self):
            return (1,)

    class Good:
        def _tx(self):
            return Cur()

    class Down:
        def _tx(self):
            raise psycopg2.OperationalError('connection to server at "10.0.0.9", port 5432 failed: password authentication failed for user "ripple"')

    async def get(path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client), base_url="https://judge.test") as c:
            return await c.get(path)

    monkeypatch.setattr(pol, "_v2_store", lambda: Good())
    r = _run(get("/ready"))
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": "ok"}
    monkeypatch.setattr(pol, "_v2_store", lambda: Down())
    r = _run(get("/ready"))
    assert r.status_code == 503 and r.json() == {"status": "unavailable", "database": "unavailable"} and "10.0.0.9" not in r.text and "password" not in r.text
    assert _run(get("/health")).status_code == 200                                         # liveness unaffected
    monkeypatch.setattr(pol, "_v2_store", lambda: None)
    assert _run(get("/ready")).json() == {"status": "ok", "database": "not_configured"}
