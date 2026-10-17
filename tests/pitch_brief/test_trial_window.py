"""The trial window: ONE end time in the database that the web (refuses new AI actions after it, with its own explained message), the worker (stops claiming at it, switches both switches OFF
when it exits) and every signed-in role (a banner with the time left and the switch states) all read. Independent of any agent, terminal or machine. Stub provider; no AI is called."""
from __future__ import annotations

import pytest

from infra import operator_flags as F
from infra import provider_gate
from infra.worker import job_worker
from tests.pitch_brief.chain import build
from tests.ai10ui.fixtures import pw_browser  # noqa: F401
from tests.pitch_brief.conftest import create_job


@pytest.fixture
def on(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_OPERATOR_FLAGS", "1")
    monkeypatch.setenv("RIPPLE_V2_DSN", env.dsn)
    monkeypatch.setenv("RIPPLE_FLAG_CACHE_SECONDS", "0")
    monkeypatch.delenv("RIPPLE_PROVIDER_MODE", raising=False)
    F.reset_cache()
    import psycopg2
    with psycopg2.connect(env.dsn) as cx, cx.cursor() as cur:
        F._ensure(cur)
        cur.execute("DELETE FROM v2_operator_flags")
        cur.execute("DELETE FROM v2_operator_window")
    yield
    with psycopg2.connect(env.dsn) as cx, cx.cursor() as cur:        # leave nothing behind: a recorded window would disarm the AI path of every later test that shares this database
        cur.execute("DROP TABLE IF EXISTS v2_operator_window")
        cur.execute("DELETE FROM v2_operator_flags")
    F._ready = False
    F.reset_cache()


def test_no_window_means_nothing_changes(on):
    assert F.window_status()["state"] == "none" and not F.window_over() and not provider_gate.is_disarmed()
    provider_gate.require_provider("change.draft")


def test_a_window_is_bounded_audited_and_counts_down(on):
    with pytest.raises(ValueError):
        F.set_window(86, "operator", "a window longer than the approved one")
    with pytest.raises(ValueError):
        F.set_window(30, "operator", "short")
    F.set_window(60, "operator", "trial window for the test")
    st = F.window_status()
    assert st["state"] == "open" and 3500 < st["seconds_left"] <= 3600 and not F.window_over()
    assert F.status()["window"]["state"] == "open"                                   # the switches payload carries it for every role


def test_after_the_end_new_ai_actions_are_refused_with_the_window_message_and_other_work_is_not(on):
    F.set_window(0, "operator", "the window ends now for the test")
    assert F.window_over() and F.window_status()["state"] == "ended" and provider_gate.is_disarmed()
    with pytest.raises(provider_gate.WindowEnded):
        provider_gate.require_provider("change.draft")
    provider_gate.require_provider("impact")                                          # deterministic work and reading are not an AI action
    body = provider_gate.refusal_for(provider_gate.WindowEnded("trial window ended"))
    assert body.status_code == 409 and b"window_ended" in body.body and b"Nothing was started and nothing was charged" in body.body


def test_the_worker_stops_claiming_at_the_window_end_and_never_runs_longer_than_configured(on):
    assert job_worker.runtime_until_window(5100) == 5100                              # no window: the configured bound as before
    F.set_window(10, "operator", "ten minute window for the test")
    assert 590 < job_worker.runtime_until_window(5100) <= 600                        # the window is shorter than the bound
    assert job_worker.runtime_until_window(120) == 120                               # the bound is shorter than the window
    F.set_window(0, "operator", "the window ends now for the test")
    assert job_worker.runtime_until_window(5100) == 0.0


def test_the_worker_switches_both_switches_off_itself_when_its_window_ends_and_only_then(on, monkeypatch):
    monkeypatch.setenv("RIPPLE_WORKER_CLOSE_ON_EXIT", "1")
    assert job_worker.close_switches_after_window({"stopped": "max_jobs"}) == []      # a different reason to stop: nothing is switched
    assert F.ai_on() and F.claims_on()
    assert job_worker.close_switches_after_window({"stopped": "max_runtime"}) == ["worker_claims", "ai_switch"]
    assert not F.ai_on() and not F.claims_on()
    hist = F.history(4)
    assert {h["key"] for h in hist[:2]} == {"ai_switch", "worker_claims"} and all(h["set_by"] == "worker" and "window ended" in h["reason"] for h in hist[:2])
    monkeypatch.delenv("RIPPLE_WORKER_CLOSE_ON_EXIT")
    F.set_flag("ai_switch", "on", "operator", "back on for the next test step")
    assert job_worker.close_switches_after_window({"stopped": "max_runtime"}) == [] and F.ai_on()       # not enabled: never switches by itself


def test_the_web_refuses_a_new_ai_job_after_the_window_with_the_explained_409(env, on, monkeypatch):
    ch = build(env, monkeypatch, finguide_scope={"category": "all"})
    from tests.pitch_brief.chain import S1, intent
    iid = intent(env, ch.admin, S1, confirm=True)["intent_id"]
    F.set_window(0, "operator", "the window ends now for the test")
    jid, r = create_job(env.req_client(), env, "change.draft", {"intent_id": iid, "extraction_job_ids": list(ch.jobs.values())})
    assert r.status_code == 409 and r.json()["code"] == "window_ended" and "no longer accepted" in r.json()["error"]
    with env.store._tx() as cur:                                                       # nothing was created, nothing is waiting
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, jid))
        assert cur.fetchone()[0] == 0
    flags = env.req_client().get("/v2/operator/flags", params={"workspace_id": env.ws}).json()
    assert flags["window"]["state"] == "ended" and flags["window"]["seconds_left"] == 0           # every role reads the same state


def test_every_role_sees_the_window_banner_with_the_time_left_and_then_the_ended_state(env, on, monkeypatch, pw_browser):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect
    from tests.ai10ui.contract import select_workspace, tech_url, tid, UI_BASE
    from tests.ai10ui.harness import ServerThread
    from tests.pitch_brief.test_ui_flow import _page
    F.set_window(30, "operator", "thirty minute window for the banner test")
    server = ServerThread().start()
    try:
        for who, cred in ((env.req, f"cred-r-{env.nonce}"), (env.admin, f"cred-a-{env.nonce}")):
            page = _page(pw_browser, server, (who, cred))
            select_workspace(page, env.ws)
            page.goto("about:blank")
            page.goto(tech_url(f"{server.url}{UI_BASE}#/ws/{env.ws}/overview"))
            banner = tid(page, "window-banner")
            expect(banner).to_have_attribute("data-state", "open", timeout=40000)
            expect(tid(page, "window-left")).to_contain_text("left")
            expect(tid(page, "window-switches")).to_contain_text("New AI starts: ON")
            F.set_window(0, "operator", "the window ends now for the banner test")
            F.set_flag("ai_switch", "off", "operator", "switched off for the banner test")
            page.goto("about:blank")
            page.goto(tech_url(f"{server.url}{UI_BASE}#/ws/{env.ws}/overview"))
            expect(tid(page, "window-banner")).to_have_attribute("data-state", "ended", timeout=40000)
            expect(tid(page, "window-banner")).to_contain_text("nothing is lost")
            expect(tid(page, "window-switches")).to_contain_text("New AI starts: OFF")
            assert not page._errors, page._errors
            F.set_window(30, "operator", "thirty minute window for the banner test")
            F.set_flag("ai_switch", "on", "operator", "switched on for the banner test")
    finally:
        server.stop()


def test_a_window_that_was_never_set_is_not_a_window_even_when_the_table_does_not_exist(on):
    import psycopg2
    with psycopg2.connect(F.os.environ["RIPPLE_V2_DSN"]) as cx, cx.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS v2_operator_window")
    F._ready = False
    F.reset_cache()
    assert F.window_status()["state"] == "none" and not F.window_over()               # no crash, and "never set" is not read as "open" or "ended"
    provider_gate.require_provider("change.draft")                                    # nothing is refused because of a window that does not exist
    F.set_window(30, "operator", "first window ever set, table created on first use")  # the application creates its table the way it creates the switches table
    assert F.window_status()["state"] == "open"


def test_a_window_expires_by_itself_with_nobody_acting(on):
    import time as _t
    F.set_window(0.02, "operator", "a window of about one second for the test")
    assert F.window_status()["state"] == "open"
    _t.sleep(1.5)                                                                     # no agent, no terminal, no switch change: only the clock
    assert F.window_status()["state"] == "ended" and F.window_over()
    with pytest.raises(provider_gate.WindowEnded):
        provider_gate.require_provider("intent.interpret")


# ------------------------------------------------------------------------------------------------ the evaluation (serving) window: an absolute deadline weeks away
def test_a_serving_window_has_an_absolute_end_far_away_and_bounds_the_worker_by_it(on):
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc) + timedelta(days=24)
    F.set_window_until(end, "operator", "evaluation deadline for the test")
    st = F.window_status()
    assert st["state"] == "open" and st["seconds_left"] > 23 * 86400 and not F.window_over()
    assert job_worker.runtime_until_window(1500) == 1500                           # the per-execution bound is shorter than the window
    with pytest.raises(ValueError):
        F.set_window_until(datetime.now(timezone.utc) + timedelta(days=120), "operator", "too far away for the test")
    with pytest.raises(ValueError):
        F.set_window_until(datetime.now(timezone.utc) - timedelta(minutes=1), "operator", "in the past for the test")
    with pytest.raises(ValueError):
        F.set_window_until(datetime.now(), "operator", "no time zone for the test")


def test_the_web_stops_accepting_jobs_before_the_worker_stops_claiming_and_says_why(env, on, monkeypatch):
    from datetime import datetime, timedelta, timezone
    ch = build(env, monkeypatch, finguide_scope={"category": "all"})                     # the fixture itself creates jobs: build it before the window closes
    monkeypatch.setenv("RIPPLE_WINDOW_CREATE_MARGIN_SECONDS", "300")
    F.set_window_until(datetime.now(timezone.utc) + timedelta(seconds=200), "operator", "ends in 200 s, margin 300 s")      # inside the margin: creation closed, claiming still open
    assert F.window_creation_closed() and not F.window_over() and job_worker.runtime_until_window(1500) <= 200
    jid, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "case": "route", "edits": []})
    assert r.status_code == 409 and r.json()["code"] == "window_ended" and "Nothing was started" in r.json()["error"]
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, jid))
        assert cur.fetchone()[0] == 0                                                   # nothing was created, nothing waits
    r2 = env.req_client().post("/v2/change/coverage/ensure", json={"workspace_id": env.ws})
    assert r2.status_code == 409
    F.set_window_until(datetime.now(timezone.utc) + timedelta(days=1), "operator", "extended by the operator for the test")      # an extension re-opens it
    assert not F.window_creation_closed()


def test_no_worker_execution_is_started_after_the_window_ended(on, env):
    from infra.worker import wake
    F.set_window(0, "operator", "ended for the wake test")
    with env.store._tx() as cur:
        wake._ensure(cur)
        cur.execute("DELETE FROM v2_outbox")
        cur.execute("INSERT INTO v2_outbox (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,'JOB_QUEUED','{\"job_id\":\"j\",\"kind\":\"simulation\"}')", (env.tenant, env.ws))
    assert wake.kick(env.store, post=lambda n: (_ for _ in ()).throw(AssertionError("must not start")), job_name="projects/p/locations/l/jobs/j")["reason"] == "window_ended"
