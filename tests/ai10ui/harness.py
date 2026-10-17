"""Hermetic infrastructure for the AI10 browser journeys (no network, no GCS, no live AI).

* Isolated Postgres database `ai10_tester` on the shared docker instance (port 55434); the
  DSN env vars the upstream fixtures read are pointed at it BEFORE they are imported.
* Blob seam = in-memory content-addressed port (`tests/ai9t01/conftest.py::ai9_hermetic`).
* AI seam = deterministic provider (`infra.worker.extraction._client_for_model`), the same seam
  every AI4..AI9 gate test uses; `install_text_provider` answers by the SOURCE TEXT it is shown
  and can be switched to `outage` or delayed by a test.
* The real `infra.judge.app` is served by uvicorn in a thread of THIS process (so the seams
  above apply) on 127.0.0.1:<free port>; a worker thread drives the production dispatcher
  `job_worker.claim_and_admit_one` (pausable) so UI-created jobs really run.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import psycopg2

DB_NAME = os.environ.get("AI10_TEST_DB", "ai10_tester")
_ADMIN = "host=127.0.0.1 port=55434 dbname=postgres user=postgres password=localtest"
DSN = os.environ.get("AI10_TEST_DSN",
                     f"host=127.0.0.1 port=55434 dbname={DB_NAME} user=postgres password=localtest")


def _ensure_database() -> None:
    try:
        conn = psycopg2.connect(_ADMIN, connect_timeout=3)
    except psycopg2.OperationalError:
        return  # fixtures skip with a clear message
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        conn.close()


if "AI10_TEST_DSN" not in os.environ:
    _ensure_database()
# The upstream conftests read these at import time.
for _name in ("AI4_T01_TEST_DSN", "AI2_T03_TEST_DSN", "AI2_T04_TEST_DSN", "AI1_T03_TEST_DSN"):
    os.environ[_name] = DSN

LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    """`infra.judge.app` behind uvicorn, in a thread, on 127.0.0.1. Origin check uses the
    request base URL (RIPPLE_JUDGE_ORIGIN must be UNSET so the TestClient and the browser
    both pass); the browser origin is exactly `self.url`."""

    def __init__(self):
        import uvicorn
        from infra import judge
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(judge.app, host="127.0.0.1", port=self.port, log_level="warning",
                                lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class UiWorker:
    """Pausable background worker over the PRODUCTION dispatcher. Unrelated (non-job) outbox
    events are acked like `tests.ai8t03.conftest.pump` does, except the apply
    SOURCE_PUBLISHED event which only the production dispatcher may consume."""

    def __init__(self, setup):
        self.setup = setup
        self.errors: list = []
        self.claims: list = []
        self._stop = threading.Event()
        self._go = threading.Event()
        self._go.set()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def pause(self):
        self._go.clear()
        time.sleep(0.4)  # let an in-flight claim finish

    def resume(self):
        self._go.set()

    def stop(self):
        self._stop.set()
        self._go.set()
        if self.thread.is_alive():
            self.thread.join(timeout=15)

    def _run(self):
        from uuid import uuid4
        from infra.worker import job_worker
        store, t, ws = self.setup["store"], self.setup["tenant"], self.setup["workspace"]
        while not self._stop.is_set():
            if not self._go.is_set():
                time.sleep(0.05)
                continue
            worker = f"ui-worker-{uuid4().hex[:6]}"
            try:
                claim = job_worker.claim_and_admit_one(store, t, ws, worker, lease_seconds=5)
            except Exception as exc:  # noqa: BLE001 - surfaced to the test
                self.errors.append(f"{type(exc).__name__}: {exc}")
                time.sleep(0.3)
                continue
            if claim is None:
                time.sleep(0.15)
                continue
            payload = claim.payload if isinstance(claim.payload, dict) else {}
            self.claims.append((claim.kind, payload.get("kind")))
            if claim.kind == "JOB_QUEUED":
                continue
            if claim.kind == "SOURCE_PUBLISHED" and payload.get("kind") == "apply":
                continue
            try:
                if store.outbox_status(t, ws, claim.id) == "CLAIMED":
                    store.ack_outbox(t, ws, claim.id, worker, claim.fencing_token)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"ack {type(exc).__name__}: {exc}")


def install_text_provider(monkeypatch, rules):
    """Deterministic extraction provider answering by the source text shown to it.
    rules = [(needle, [candidate dict, ...])]. Returns a holder with `.calls`, `.mode`
    ("ok" | "outage" | "malformed"), `.delay` (seconds) and `.gate` (threading.Event a test may
    clear to hold every provider call until it sets it again)."""
    from tests.ai4t02.conftest import _response
    from infra.worker import job_worker  # noqa: F401  (import first: circular import)
    from infra.worker import extraction

    holder = SimpleNamespace(calls=[], mode="ok", delay=0.0, gate=threading.Event())
    holder.gate.set()

    def factory(model):
        def generate_content(**kwargs):
            holder.calls.append(kwargs)
            holder.gate.wait(30)
            if holder.delay:
                time.sleep(holder.delay)
            if holder.mode == "outage":
                raise TimeoutError("provider outage (test injection)")
            blob = json.dumps(kwargs, default=str)
            if holder.mode == "malformed":
                return SimpleNamespace(text="not json {", model_version="test-model",
                                       candidates=[SimpleNamespace(finish_reason="STOP")],
                                       usage_metadata=SimpleNamespace(
                                           prompt_token_count=40, candidates_token_count=30,
                                           thoughts_token_count=None, total_token_count=70))
            for needle, candidates in rules:
                if needle in blob:
                    return _response(candidates)
            return _response([])
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content),
                               calls=holder.calls)

    monkeypatch.setattr(extraction, "_client_for_model", factory)
    return holder


# ------------------------------------------------------------------------- SQL observation

def q(setup, sql, args=()):
    """Read rows (dict-like) from the isolated database through the production store's tx."""
    with setup["store"]._tx() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def scoped(setup):
    return (setup["tenant"], setup["workspace"])


def count_rows(setup, table, where="", args=()):
    sql = f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND workspace_id=%s {where}"
    return q(setup, sql, scoped(setup) + tuple(args))[0][0]


def durable_snapshot(setup):
    """Content hash of EVERY workspace-scoped `v2_*` table (discovered from information_schema):
    the 'DB unchanged' proof for denied raw-API mutations."""
    import hashlib
    tables = [r[0] for r in q(setup, """SELECT table_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name LIKE 'v2\\_%%'
          AND column_name='workspace_id' AND table_name NOT IN ('v2_audit')
        GROUP BY table_name ORDER BY table_name""")]
    out = {}
    for table in tables:
        rows = q(setup, f"SELECT * FROM {table} WHERE workspace_id=%s ORDER BY 1", (setup["workspace"],))
        out[table] = hashlib.sha256(repr([tuple(map(str, r)) for r in rows]).encode()).hexdigest()
    return out
