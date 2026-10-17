"""On-demand wake-up of the batch worker: no poller while idle, a job stored in the database is never lost when the signal fails, duplicate signals start no second claim, a job that arrives as the worker
exits is not stranded. Real database (disposable), fake Cloud Run `post`; no provider is called."""
from __future__ import annotations

import json
import time

import pytest

from infra.worker import job_worker, wake
from tests.pitch_brief.conftest import create_job


@pytest.fixture
def w(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_WAKE_JOB", "projects/p/locations/l/jobs/ripple-worker")
    wake._ready = False                                                   # the disposable database may have been recreated
    with env.store._tx() as cur:
        wake._ensure(cur)
        cur.execute("DELETE FROM v2_outbox")                                      # the disposable database is shared: the wake-up looks at all workspaces
        cur.execute("DELETE FROM v2_worker_beats")
        cur.execute("DELETE FROM v2_worker_reservations")
        cur.execute("UPDATE v2_worker_wake SET last_kick_at=NULL, last_ok=NULL, last_error=NULL, kicks=0, failures=0, worker_seconds=0")
    return env


def queue(env, job_id="job-wake-1"):
    with env.store._tx() as cur:
        cur.execute("""INSERT INTO v2_outbox (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,'JOB_QUEUED',%s)""",
                    (env.tenant, env.ws, json.dumps({"job_id": job_id, "kind": "change.draft"})))


class Named:
    """The run call of Cloud Run answers with the execution it started; this fake names them exec-1, exec-2 ..."""
    def __init__(self):
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return f"exec-{len(self.calls)}"


class Post:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, name):
        self.calls.append(name)
        if len(self.calls) <= self.fail:
            raise RuntimeError("403 run.jobs.run denied")


def test_nothing_is_started_without_work_or_when_disabled_or_while_a_worker_is_alive(w, monkeypatch):
    post = Post()
    assert wake.kick(w.store, post=post)["reason"] == "no_work" and not post.calls
    queue(w)
    monkeypatch.delenv("RIPPLE_WAKE_JOB")
    assert wake.kick(w.store, post=post)["reason"] == "disabled" and not post.calls
    monkeypatch.setenv("RIPPLE_WAKE_JOB", "projects/p/locations/l/jobs/ripple-worker")
    wake.beat(w.store, "worker-alive")
    assert wake.kick(w.store, post=post)["reason"] == "worker_alive" and not post.calls
    wake.clear_beat(w.store, "worker-alive")
    assert wake.kick(w.store, post=post) == {"kicked": True, "reason": "signal_sent"} and post.calls == ["projects/p/locations/l/jobs/ripple-worker"]


def test_a_burst_of_requests_sends_one_signal(w):
    queue(w)
    post = Post()
    got = [wake.kick(w.store, post=post)["reason"] for _ in range(5)]
    assert got == ["signal_sent", "recent_kick", "recent_kick", "recent_kick", "recent_kick"] and len(post.calls) == 1


def test_a_failed_signal_loses_nothing_and_the_next_request_retries_at_once(w):
    queue(w)
    post = Post(fail=1)
    r = wake.kick(w.store, post=post)
    assert r["reason"] == "signal_failed" and "denied" in r["error"]
    with w.store._tx() as cur:
        cur.execute(f"SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND {wake.CLAIMABLE}", (w.tenant,))
        assert cur.fetchone()[0] == 1                                            # the job is still stored and claimable
        cur.execute("SELECT last_ok, failures, last_kick_at FROM v2_worker_wake")
        assert cur.fetchone() == (False, 1, None)                                # recorded, and the debounce was released
    assert wake.kick(w.store, post=post)["kicked"] is True and len(post.calls) == 2


def test_two_workers_woken_for_one_job_claim_it_once(w):
    queue(w)
    a = w.store.claim_outbox(w.tenant, w.ws, "worker-a")
    b = w.store.claim_outbox(w.tenant, w.ws, "worker-b")
    assert a is not None and b is None                                           # the second (duplicate) wake-up finds nothing to claim: no second execution of the job


def test_a_job_that_arrives_while_the_worker_is_leaving_is_claimed_by_its_last_sweep(w):
    class Claims:
        def __init__(self):
            self.n, self.late_done = 0, False

        def __call__(self, store, t, ws, wid):
            self.n += 1
            with store._tx() as cur:
                cur.execute("SELECT count(*) FROM v2_worker_beats WHERE worker_id=%s", (wid,))
                beating = cur.fetchone()[0]
            if beating == 0 and not self.late_done:                             # the leaving worker's sweep: the heartbeat is already cleared (every normal pass beats first)
                self.late_done = True
                return type("C", (), {"kind": "JOB_QUEUED"})()
            return None
    t = [0.0]
    claim = Claims()
    summary = job_worker.run_bounded_worker(w.store, [(w.tenant, w.ws)], worker_id="worker-leaving", poll_seconds=1, idle_exit_seconds=2, claim_fn=claim,
                                            clock=lambda: t.__setitem__(0, t[0] + 1) or t[0], sleep=lambda s: None, heartbeat=True)
    assert summary["stopped"] == "idle" and summary["jobs"] == 1 and claim.late_done          # claimed by the final sweep, then the worker kept going before leaving for good
    with w.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_worker_beats WHERE worker_id='worker-leaving'")
        assert cur.fetchone()[0] == 0                                           # nothing alive is claimed after it left: the next write starts a new execution


def test_a_successful_write_through_the_web_app_starts_the_worker_in_the_background(w, monkeypatch):
    from fastapi.testclient import TestClient
    from infra import judge
    queue(w)
    post = Post()
    monkeypatch.setattr(wake, "run_cloud_run_job", post)
    c = w.req_client()
    c.post("/v2/auth/logout") if False else None
    r = c.post("/v2/change/coverage/ensure", json={"workspace_id": w.ws})        # any successful write
    assert r.status_code < 400, r.text
    assert post.calls == ["projects/p/locations/l/jobs/ripple-worker"]          # sent inside the request (Cloud Run throttles the CPU after the response)
    assert TestClient(judge.app, base_url="https://judge.test").get("/v2/work/inbox", params={"workspace_id": w.ws}).status_code == 401   # a read starts nothing and shows nothing
    n = len(post.calls)
    time.sleep(0.3)
    assert len(post.calls) == n


# ------------------------------------------------------------------------------------------------ reliability of the on-demand design
def test_a_job_queued_after_the_last_sweep_while_the_execution_is_still_ending_starts_a_new_execution(w):
    """The worker returned from its loop (heartbeat already cleared) but its execution has not finished yet: the new job must not wait for the next scheduler tick."""
    t = [0.0]
    job_worker.run_bounded_worker(w.store, [(w.tenant, w.ws)], worker_id="worker-ending", poll_seconds=1, idle_exit_seconds=2, claim_fn=lambda *a: None,
                                  clock=lambda: t.__setitem__(0, t[0] + 1) or t[0], sleep=lambda s: None, heartbeat=True)
    queue(w, "job-after-last-sweep")                                                # the process is still alive, but it will never look again
    post = Post()
    assert wake.kick(w.store, post=post) == {"kicked": True, "reason": "signal_sent"} and len(post.calls) == 1


def test_two_web_instances_asking_at_the_same_moment_send_one_signal(w):
    import threading
    queue(w)
    post = Post()
    out, barrier = [], threading.Barrier(2)

    def one():
        barrier.wait()
        out.append(wake.kick(w.store, post=post)["reason"])
    ts = [threading.Thread(target=one) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["recent_kick", "signal_sent"] and len(post.calls) == 1


def test_a_heartbeat_left_by_a_crashed_worker_does_not_block_the_wake_up_for_ever(w):
    queue(w)
    wake.beat(w.store, "worker-crashed")
    post = Post()
    assert wake.kick(w.store, post=post)["reason"] == "worker_alive"
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_beats SET beat_at = now() - interval '5 minutes' WHERE worker_id='worker-crashed'")      # it died: nobody refreshes the beat
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent" and len(post.calls) == 1


def test_a_child_job_created_while_a_job_runs_is_processed_by_the_same_execution(w):
    """The pipeline creates jobs from inside the worker: the loop keeps claiming until nothing is left, so nothing waits for a new execution."""
    queue(w, "parent")
    seen = []

    def claim(store, t, ws, wid):
        c = store.claim_outbox(t, ws, wid)
        if c is None:
            return None
        seen.append(c.payload["job_id"])
        if c.payload["job_id"] == "parent":
            queue(w, "child")                                                       # what a pipeline step does
        store.ack_outbox(t, ws, c.id, wid, c.fencing_token)
        return c
    clock = iter(range(1, 1000))
    summary = job_worker.run_bounded_worker(w.store, [(w.tenant, w.ws)], worker_id="worker-pipe", poll_seconds=1, idle_exit_seconds=3, claim_fn=claim,
                                            clock=lambda: next(clock), sleep=lambda s: None, heartbeat=True)
    assert seen == ["parent", "child"] and summary["jobs"] == 2


def test_the_scheduler_backstop_recovers_a_stored_job_when_the_signal_failed_and_nobody_acts_again(w, monkeypatch):
    """DB committed the job, the Run call failed (no more user action follows). The scheduler tick (authenticated) restarts it; with nothing to do the tick starts nothing."""
    from fastapi.testclient import TestClient
    from infra import judge, v2_wake_routes
    monkeypatch.setenv("RIPPLE_WAKE_SCHEDULER_SA", "scheduler@sa.example")
    monkeypatch.setenv("RIPPLE_WAKE_AUDIENCE", "https://web.example")
    monkeypatch.setattr(v2_wake_routes, "_verify", lambda tok, aud: ({"email": "scheduler@sa.example", "email_verified": True}
                                                                    if tok == "good" and aud == "https://web.example" else (_ for _ in ()).throw(ValueError("bad"))))
    post = Post(fail=1)
    monkeypatch.setattr(wake, "run_cloud_run_job", post)
    monkeypatch.setattr(wake, "kick_in_background", lambda *a, **k: None)           # the per-request hook is tested elsewhere; here only the scheduler path acts
    c = TestClient(judge.app, base_url="https://judge.test")
    assert c.post("/internal/wake").status_code == 401                              # no token
    assert c.post("/internal/wake", headers={"Authorization": "Bearer forged"}).status_code == 401
    monkeypatch.setattr(v2_wake_routes, "_verify", lambda tok, aud: {"email": "someone-else@other.example", "email_verified": True})
    assert c.post("/internal/wake", headers={"Authorization": "Bearer good"}).status_code == 401      # a valid token of another identity
    monkeypatch.setattr(v2_wake_routes, "_verify", lambda tok, aud: {"email": "scheduler@sa.example", "email_verified": True})
    h = {"Authorization": "Bearer good"}
    assert c.post("/internal/wake", headers=h).json()["reason"] == "no_work" and not post.calls         # nothing to do: no execution started
    queue(w)
    assert wake.kick(w.store)["reason"] == "signal_failed"                          # the original (user-triggered) signal failed
    r = c.post("/internal/wake", headers=h)
    assert r.json() == {"kicked": True, "reason": "signal_sent"} and len(post.calls) == 2   # the next scheduler tick recovers it with nobody acting
    assert c.post("/internal/wake", headers=h).json()["reason"] == "recent_kick" and len(post.calls) == 2


def test_no_execution_is_started_while_worker_claims_are_switched_off(w, monkeypatch):
    from infra import operator_flags as F
    monkeypatch.setenv("RIPPLE_OPERATOR_FLAGS", "1")
    monkeypatch.setenv("RIPPLE_V2_DSN", w.dsn)
    monkeypatch.setenv("RIPPLE_FLAG_CACHE_SECONDS", "0")
    F.reset_cache()
    F.set_flag("worker_claims", "off", "operator", "paused for the test")
    try:
        queue(w)
        post = Post()
        assert wake.kick(w.store, post=post)["reason"] == "claims_off" and not post.calls
        F.set_flag("worker_claims", "on", "operator", "resumed for the test")
        assert wake.kick(w.store, post=post)["reason"] == "signal_sent"
    finally:
        with w.store._tx() as cur:
            cur.execute("DELETE FROM v2_operator_flags")
        F.reset_cache()


def test_work_of_a_workspace_the_worker_does_not_serve_never_wakes_it(w, monkeypatch):
    """Cloud finding 2026-10-13: three orphan PENDING rows of old smoke workspaces started an empty execution at every scheduler tick. The wake-up counts only the worker's own workspaces."""
    with w.store._tx() as cur:
        cur.execute("SELECT tenant_id, workspace_id FROM v2_workspaces WHERE (tenant_id, workspace_id) <> (%s, %s) LIMIT 1", (w.tenant, w.ws))
        other = cur.fetchone()
        if other is None:
            pytest.skip("the disposable database holds no second workspace")
        cur.execute("INSERT INTO v2_outbox (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,'JOB_QUEUED',%s)", (other[0], other[1], json.dumps({"job_id": "orphan", "kind": "simulation"})))
    monkeypatch.setenv("RIPPLE_WAKE_WORKSPACES", f"{w.tenant}:{w.ws}")
    post = Post()
    assert wake.kick(w.store, post=post)["reason"] == "no_work" and not post.calls
    queue(w)
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent" and len(post.calls) == 1
    monkeypatch.delenv("RIPPLE_WAKE_WORKSPACES")


def test_the_on_demand_execution_budget_stops_new_work_with_a_clear_message(env, w, monkeypatch):
    """Infrastructure self-limit: after RIPPLE_WAKE_MAX_KICKS executions the wake-up starts no more and the web stops accepting jobs (so none waits with nobody to run it)."""
    queue(w)
    post = Post()
    monkeypatch.setenv("RIPPLE_WAKE_MAX_KICKS", "1")
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent"                 # kick number 1 uses the whole budget
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET last_kick_at = NULL")                  # the debounce is not what stops the next one
    assert wake.kick_budget_exhausted(w.store) and wake.kick(w.store, post=post)["reason"] == "kick_budget_exhausted" and len(post.calls) == 1
    jid, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "case": "route", "edits": []})
    assert r.status_code == 409 and r.json()["code"] == "capacity_reached" and "Nothing was started" in r.json()["error"]
    monkeypatch.delenv("RIPPLE_WAKE_MAX_KICKS")
    assert not wake.kick_budget_exhausted(w.store)                                  # unset = no cap


def test_worker_seconds_are_reserved_at_every_start_corrected_at_exit_and_bound_the_spend(env, w, monkeypatch):
    """Infrastructure bound: billed worker time <= counted seconds. A kick reserves the task timeout; the worker's exit replaces it by the real time; a 4xx failure refunds; the cap refuses new work."""
    monkeypatch.setenv("RIPPLE_WAKE_TASK_SECONDS", "480")
    monkeypatch.setenv("RIPPLE_WAKE_MAX_SECONDS", "1000")
    queue(w)

    def secs():
        with w.store._tx() as cur:
            cur.execute("SELECT worker_seconds FROM v2_worker_wake")
            return cur.fetchone()[0]
    assert wake.kick(w.store, post=Named())["reason"] == "signal_sent" and secs() == 480                 # reserved at the start
    wake.record_worker_exit(w.store, 90, "exec-1", sleep=lambda s: None)
    assert secs() == 90                                                                                     # replaced by what it really ran
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET last_kick_at = NULL")
    import urllib.error

    def forbidden(name):
        raise urllib.error.HTTPError("u", 403, "denied", {}, None)
    assert wake.kick(w.store, post=forbidden)["reason"] == "signal_failed" and secs() == 90                  # a 4xx means nothing started: refunded
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET last_kick_at = NULL")
    assert wake.kick(w.store, post=lambda n: (_ for _ in ()).throw(TimeoutError("timed out")))["reason"] == "signal_failed" and secs() == 570   # a timeout may have started one: kept
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET last_kick_at = NULL, worker_seconds = 1000")
    assert wake.kick_budget_exhausted(w.store) and wake.kick(w.store, post=Post())["reason"] == "kick_budget_exhausted"
    jid, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "case": "route", "edits": []})
    assert r.status_code == 409 and r.json()["code"] == "capacity_reached"



def _secs(w):
    with w.store._tx() as cur:
        cur.execute("SELECT worker_seconds FROM v2_worker_wake")
        return cur.fetchone()[0]


def _rows(w):
    with w.store._tx() as cur:
        cur.execute("SELECT execution_name FROM v2_worker_reservations ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def _next_kick(w):
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET last_kick_at = NULL")


def _exit(w, secs, name):
    return wake.record_worker_exit(w.store, secs, name, sleep=lambda s: None)


@pytest.fixture
def secs480(monkeypatch):
    monkeypatch.setenv("RIPPLE_WAKE_TASK_SECONDS", "480")


def test_an_execution_started_by_hand_changes_nothing_with_an_empty_table_and_with_a_reservation_waiting(w, secs480):
    """The live finding: counter 61, a manual execution exits, the counter fell to 0. Also with a reservation of another kick present and not yet settled."""
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET worker_seconds = 61, kicks = 1")
    assert _exit(w, 3, "manual-exec") is False and _exit(w, 3, None) is False and _secs(w) == 61          # empty table
    queue(w)
    assert wake.kick(w.store, post=Named())["reason"] == "signal_sent" and _secs(w) == 541 and _rows(w) == ["exec-1"]
    assert _exit(w, 3, "manual-exec") is False and _secs(w) == 541 and _rows(w) == ["exec-1"]            # the waiting reservation is not theirs
    assert _exit(w, 40, "exec-1") is True and _secs(w) == 61 + 40 and _rows(w) == []                      # earlier spend kept, only its own reservation replaced


def test_two_kicks_whose_executions_exit_in_the_opposite_order_each_settle_only_their_own(w, secs480):
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET worker_seconds = 61")
    queue(w)
    post = Named()
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent"
    _next_kick(w)
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent" and _secs(w) == 61 + 960 and _rows(w) == ["exec-1", "exec-2"]
    assert _exit(w, 10, "exec-2") is True and _secs(w) == 61 + 480 + 10        # the later one finished first
    assert _exit(w, 20, "exec-1") is True and _secs(w) == 61 + 10 + 20
    assert _exit(w, 20, "exec-1") is False and _exit(w, 10, "exec-2") is False and _secs(w) == 91         # repeated exits are no-ops


def test_a_lost_execution_keeps_its_seconds_counted_and_nobody_else_can_release_them(w, secs480):
    queue(w)
    post = Named()
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent"      # exec-1 never exits (killed)
    _next_kick(w)
    assert wake.kick(w.store, post=post)["reason"] == "signal_sent" and _secs(w) == 960
    assert _exit(w, 5, "exec-2") is True and _secs(w) == 480 + 5
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_reservations SET reserved_at = now() - interval '3 days'")      # however old the row, only exec-1 may settle it
    assert _exit(w, 1, "manual-exec") is False and _secs(w) == 485 and _rows(w) == ["exec-1"]


def test_an_answer_without_an_execution_name_leaves_the_reservation_counted_and_unclaimable(w, secs480):
    queue(w)
    assert wake.kick(w.store, post=Post())["reason"] == "signal_sent" and _rows(w) == [None] and _secs(w) == 480
    assert _exit(w, 1, "any-exec") is False and _secs(w) == 480          # over-counting is the safe direction; nothing is released by guesswork


def test_a_4xx_refund_returns_only_that_kicks_seconds_and_a_5xx_or_timeout_never_refunds(w, secs480):
    import urllib.error
    queue(w)
    assert wake.kick(w.store, post=Named())["reason"] == "signal_sent"      # exec-1 is running with its reservation
    _next_kick(w)

    def forbidden(name):
        raise urllib.error.HTTPError("u", 403, "denied", {}, None)
    assert wake.kick(w.store, post=forbidden)["reason"] == "signal_failed" and _secs(w) == 480 and _rows(w) == ["exec-1"]     # exec-1's seconds untouched
    _next_kick(w)

    def unavailable(name):
        raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)
    assert wake.kick(w.store, post=unavailable)["reason"] == "signal_failed" and _secs(w) == 960 and _rows(w) == ["exec-1", None]
    assert _exit(w, 2, "exec-1") is True and _secs(w) == 482                # the 503 kick may have started one: its 480 stays counted


def test_concurrent_exits_lose_no_part_of_the_counter(w, secs480):
    import threading
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET worker_seconds = 61")
    queue(w)
    post = Named()
    for _ in range(6):
        assert wake.kick(w.store, post=post)["reason"] == "signal_sent"
        _next_kick(w)
    assert _secs(w) == 61 + 6 * 480
    errors = []

    def finish(i):
        try:
            for _ in range(3):
                _exit(w, 10 * (i + 1), f"exec-{i + 1}")                 # each execution exits three times (repeated exit)
        except Exception as exc:                                         # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=finish, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and _secs(w) == 61 + sum(10 * (i + 1) for i in range(6)) and _rows(w) == []


def test_a_database_error_never_opens_a_path_around_the_limit(w, secs480):
    queue(w)
    with w.store._tx() as cur:
        cur.execute("UPDATE v2_worker_wake SET worker_seconds = 61")
        cur.execute("DROP TABLE v2_worker_reservations")
    try:
        wake._ready = True                                                # the table is missing and the code believes it exists
        post = Named()
        assert wake.kick(w.store, post=post)["reason"] == "db_error" and post.calls == []               # no reservation -> no execution is started
        with pytest.raises(Exception):
            _exit(w, 5, "exec-1")                                          # the worker swallows this; a reservation (if any) stays counted
        assert _secs(w) == 61
    finally:
        wake._ready = False
        with w.store._tx() as cur:
            wake._ensure(cur)


def test_the_run_call_returns_the_short_execution_name_and_tolerates_an_unreadable_answer():
    class Resp:
        def __init__(self, body, status=200):
            self.body, self.status = body, status

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener_for(answer):
        def opener(req, timeout=2.0):
            return Resp(b'{"access_token": "t"}') if "metadata.google.internal" in req.full_url else Resp(answer)
        return opener
    full = b'{"name": "projects/p/locations/l/operations/o", "metadata": {"name": "projects/p/locations/l/jobs/ripple-v2-worker-pitch/executions/ripple-v2-worker-pitch-abc12"}}'
    assert wake.run_cloud_run_job("projects/p/locations/l/jobs/j", opener=opener_for(full)) == "ripple-v2-worker-pitch-abc12"
    assert wake.run_cloud_run_job("projects/p/locations/l/jobs/j", opener=opener_for(b"{}")) is None
    assert wake.run_cloud_run_job("projects/p/locations/l/jobs/j", opener=opener_for(b"not json")) is None
