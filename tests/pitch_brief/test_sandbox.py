"""J5: sandbox lease (server-enforced writes), job limit, bounded pump, no approval bypass, spend reserve, status.
Real PostgreSQL + real routes + stub provider. Off by default (ordinary tests elsewhere prove the no-op)."""
from __future__ import annotations

import json

import pytest

from tests.ai10ui.fixtures import pw_browser  # noqa: F401
from tests.pitch_brief.chain import APPROVAL, build, cand_id
from tests.pitch_brief.conftest import create_job, job_body, sandbox_config


@pytest.fixture
def sb(env, monkeypatch):
    ch = build(env, monkeypatch)

    def configure(**over):
        monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev, **over))
    configure()
    ch.configure = configure
    return ch


def _lease(client, env, action, participants=()):
    return client.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": action,
                                                  "participants": list(participants)})


def _rel_job(client, env, ids):
    return create_job(client, env, "relation", {"candidate_ids": ids})


def test_writes_need_the_lease_reads_do_not_and_other_sessions_are_blocked(env, sb):
    admin, req = sb.admin, env.req_client()
    ids = [cand_id(sb, APPROVAL, "standard_manager_approval_threshold")]
    # reads are open
    assert admin.get("/v2/knowledge/graph", params={"workspace_id": env.ws}).status_code == 200
    # no lease: write refused server-side with a reason the UI can show
    _, r = _rel_job(admin, env, ids)
    assert r.status_code == 409 and r.json()["code"] == "sandbox_lease_required"
    r = _lease(admin, env, "acquire", participants=[env.req])
    assert r.status_code == 200 and r.json()["i_can_write"] is True
    assert _rel_job(admin, env, ids)[1].status_code == 200
    assert _rel_job(req, env, ids)[1].status_code == 200            # a named participant may write
    # a stranger account (member, not participant) is read-only while the lease is active and cannot take it
    from tests.pitch_brief.test_ui_flow import _account
    acct, cred = _account(env, "requester")
    from infra import judge
    from fastapi.testclient import TestClient
    other = TestClient(judge.app, base_url="https://judge.test")
    other.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert other.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    r = _rel_job(other, env, ids)[1]
    assert r.status_code == 409 and r.json()["held_by_other"] is True
    assert _lease(other, env, "acquire").status_code == 409
    assert other.get("/v2/sandbox/status", params={"workspace_id": env.ws}).json()["i_can_write"] is False
    # only the holder releases; then the other session can take it
    assert _lease(other, env, "release").status_code == 403
    assert _lease(admin, env, "release").status_code == 200
    assert _rel_job(admin, env, ids)[1].status_code == 409          # released: nobody writes
    assert _lease(other, env, "acquire").status_code == 200


def test_unknown_participant_is_refused_and_other_workspaces_unaffected(env, sb):
    r = _lease(sb.admin, env, "acquire", participants=["nobody-here"])
    assert r.status_code == 400
    # a workspace that is not configured is not a sandbox (404 on the sandbox routes, ordinary rules elsewhere)
    r = sb.admin.get("/v2/sandbox/status", params={"workspace_id": "ws-other"})
    assert r.status_code in (403, 404)


def test_job_limit_per_lease(env, sb):
    sb.configure(max_jobs_per_lease=2)
    ids = [cand_id(sb, APPROVAL, "standard_manager_approval_threshold")]
    assert _lease(sb.admin, env, "acquire").status_code == 200
    assert _rel_job(sb.admin, env, ids)[1].status_code == 200
    assert _rel_job(sb.admin, env, ids)[1].status_code == 200
    r = _rel_job(sb.admin, env, ids)[1]
    assert r.status_code == 429 and r.json()["code"] == "sandbox_job_limit"


def test_pump_is_bounded_needs_lease_and_never_approves(env, sb):
    admin = sb.admin
    ids = [cand_id(sb, APPROVAL, "standard_manager_approval_threshold")]
    p = admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws})
    assert p.status_code == 409                                      # no lease, no pump
    sb.configure(pump_max_jobs=1)                                    # workspace concurrency is 2: two jobs, one per click
    assert _lease(admin, env, "acquire", participants=[env.req]).status_code == 200
    jobs = []
    for _ in range(2):
        jid, r = _rel_job(admin, env, ids)
        assert r.status_code == 200, r.text
        jobs.append(jid)
    st = admin.get("/v2/sandbox/status", params={"workspace_id": env.ws}).json()
    assert st["jobs"]["queued"] == 2 and st["spend"]["cap_usd"] is not None
    p1 = admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws}).json()
    assert p1["processed"] == 1 and p1["bounded_by"]["max_jobs"] == 1        # bounded per click
    p2 = admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws}).json()
    assert p2["processed"] == 1
    assert all(job_body(admin, env, j)["execution_status"] == "COMPLETED" for j in jobs)
    # a provider job waits for a distinct approver: the pump does not run or approve it
    ej, r = create_job(env.req_client(), env, "extraction", {"artifact_id": sb.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    calls_before = sb.stub.calls
    p3 = admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws}).json()
    assert p3["processed"] == 0 and sb.stub.calls == calls_before
    assert p3["waiting_for_approval"] == 1
    assert job_body(admin, env, ej)["execution_status"] == "QUEUED"
    # a DIFFERENT person approves; the next pump then runs it (one provider call, the approval stays the human's)
    assert admin.post(f"/v2/jobs/{ej}/approve", json={"workspace_id": env.ws}).status_code == 200
    import time
    p4 = {"processed": 0}
    for _ in range(20):                         # the held claim is deferred a few seconds (HOLD_SECONDS), then reclaimed
        p4 = admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws}).json()
        if p4["processed"]:
            break
        time.sleep(1)
    assert p4["processed"] == 1 and sb.stub.calls == calls_before + 1
    assert job_body(admin, env, ej)["execution_status"] == "COMPLETED"


def test_pump_refuses_below_budget_reserve_and_reports_busy(env, sb):
    sb.configure(min_remaining_usd=1000)
    assert _lease(sb.admin, env, "acquire").status_code == 200
    r = sb.admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws})
    assert r.status_code == 429 and "reserve" in r.json()["error"]
    sb.configure()
    from infra import v2_sandbox
    assert v2_sandbox._PUMP_LOCK.acquire(blocking=False)
    try:
        r = sb.admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws})
        assert r.status_code == 202 and r.json()["busy"] is True
    finally:
        v2_sandbox._PUMP_LOCK.release()


def test_status_shows_seed_and_reset_route_is_the_history_restore(env, sb):
    st = sb.admin.get("/v2/sandbox/status", params={"workspace_id": env.ws}).json()
    assert st["at_seed"] is True and st["seed_revision_id"] == sb.rev
    assert "restore" in st["reset"]["how"] and st["spend"]["note"].startswith("project-wide")


def test_browser_sandbox_page_lease_pump_and_read_only_other_session(env, sb, pw_browser):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect
    from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid
    from tests.ai10ui.harness import ServerThread
    server = ServerThread().start()
    try:
        def page_for(account, cred):
            p = pw_browser.new_context(base_url=server.url, viewport={"width": 1300, "height": 1000}).new_page()
            p.set_default_timeout(30000)
            open_app(p, server.url)
            login(p, account, cred)
            select_workspace(p, env.ws)
            goto_view(p, "Sandbox")
            return p
        admin = page_for(env.admin, f"cred-a-{env.nonce}")
        expect(tid(admin, "sandbox-lease-state")).to_have_attribute("data-state", "FREE")
        # legacy `/app` says "seed revision"; the new interface says "starting version" (same fact, plain words)
        from tests.ai10ui.contract import UI_BASE
        expect(tid(admin, "sandbox-at-seed")).to_contain_text("starting version" if UI_BASE.startswith("/app-next") else "seed revision")
        tid(admin, "sandbox-participants").fill(env.req)
        tid(admin, "sandbox-acquire").click()
        expect(tid(admin, "sandbox-lease-state")).to_have_attribute("data-state", "MINE")
        # the requester is a named participant: sees the lease as theirs; a job is queued, the admin pumps it from the page
        ids = [cand_id(sb, APPROVAL, "standard_manager_approval_threshold")]
        _, r = _rel_job(admin_api := sb.admin, env, ids)
        assert r.status_code == 200, r.text
        tid(admin, "sandbox-jobs").get_by_role("button", name="Refresh").click()
        expect(tid(admin, "sandbox-pump")).to_be_enabled()
        tid(admin, "sandbox-pump").click()
        expect(tid(admin, "sandbox-jobs")).to_contain_text("0 queued")
        # a different member who is not on the lease sees it read-only
        from tests.pitch_brief.test_ui_flow import _account
        acct, cred = _account(env, "requester")
        other = page_for(acct, cred)
        expect(tid(other, "sandbox-lease-state")).to_have_attribute("data-state", "OTHER")
        expect(tid(other, "sandbox-pump")).to_be_disabled()
        expect(tid(other, "sandbox-acquire")).to_have_count(0)
        tid(admin, "sandbox-release").click()
        expect(tid(admin, "sandbox-lease-state")).to_have_attribute("data-state", "FREE")
    finally:
        server.stop()
