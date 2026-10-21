"""RUX-06 / RQA-05 browser journey for the intent-flow family on the React build (/app-next/): real server (uvicorn thread), real Chromium, real routes and store on the
throwaway PostgreSQL. A requester's confirmed request with no AI draft yet: the task frame names the step ("Ask for the AI draft of the change") for the requester, the
whole-request map marks it as their step, My work lists it and opens it; a judge who may prepare changes sees that the requester is asked (eligible, not assigned); opening
the request assistant and its thread starts no model call and no job. Not model quality."""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg2
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_request_task_view import ch  # noqa: E402,F401  (fixture)
from tests.pitch_brief.chain import S1, intent  # noqa: E402


def _shot(page, name):
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"rux_{name}.png"), full_page=False)


def _member(env, roles):
    from infra.adapters.v2_identity import PostgresIdentityStore
    ids = PostgresIdentityStore(env.dsn)
    acct, cred = f"jb-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, tuple(roles))
    return acct, cred


def _assistant_jobs(env) -> int:
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='request.qa'", (env.tenant, env.ws))
        return cur.fetchone()[0]


def test_intent_request_task_frame_map_my_work_and_no_call_on_open(env, ch, pw_browser, monkeypatch):
    calls = []
    monkeypatch.setattr(explain, "_client_for_model", lambda m: calls.append(m) or (_ for _ in ()).throw(AssertionError("no model call expected")))
    iid = intent(env, env.req_client(), S1)["intent_id"]                                       # stated and confirmed by the requester, no draft yet
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash="overview")
        page.set_viewport_size({"width": 1440, "height": 900})
        row = page.locator(f'[data-testid="work-todo"][data-section="mine"][data-proposal="{iid}"]')
        expect(row).to_be_visible(timeout=WAIT)
        expect(row).to_contain_text("Ask for the AI draft of the change")
        _shot(page, "1_my_work_requester")
        row.get_by_test_id("work-open-todo").click()
        frame = page.get_by_test_id("task-frame")
        expect(frame).to_have_attribute("data-stage", "ASK_DRAFT", timeout=WAIT)
        expect(frame.locator('[data-testid="task-my-obligation"][data-code="ask_draft"]')).to_contain_text("Ask for the AI draft of the change")
        expect(frame.get_by_test_id("next-action")).to_have_attribute("data-who", "You")                  # the requester keeps the instruction, inside the task frame
        expect(page.locator('[data-testid="journey"] li[data-step="draft"]')).to_have_attribute("data-mine", "true")
        frame.scroll_into_view_if_needed()
        _shot(page, "2_task_frame_requester")
        frame.get_by_test_id("task-mode-map").click()
        expect(frame.locator('[data-testid="task-map-step"][data-step="draft"]').get_by_test_id("task-map-yours")).to_be_visible()
        draft = frame.locator('[data-testid="task-map-step"][data-step="draft"]')                                 # D4: current but not started: never a "running" badge
        expect(draft).to_have_attribute("data-phase", "not_started")
        expect(draft.get_by_test_id("status-badge")).to_have_text("Not started")
        frame.get_by_test_id("assistant-launch").click()
        panel = frame.get_by_test_id("request-assistant")
        expect(panel.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)                 # EUI-02: no Start step
        expect(panel.locator('[data-testid="assistant-suggestion"][data-id="my_step"]')).to_be_visible()   # the requester is asked: a question about their own step
        assert calls == [] and _assistant_jobs(env) == 0                                       # opening the panel: no model call, no job
        assert not page._errors, page._errors
        page.context.close()
        jacct, jcred = _member(env, ("judge",))
        jp = sign_in(pw_browser, server, env.ws, jacct, jcred, page_hash=f"changes?intent={iid}")
        jf = jp.get_by_test_id("task-frame")
        expect(jf).to_be_visible(timeout=WAIT)
        expect(jf.get_by_test_id("task-not-yours")).to_contain_text("the requester")
        expect(jf.get_by_test_id("task-eligible-not-asked")).to_contain_text("requester is asked")
        jf.get_by_test_id("assistant-launch").click()                                                    # may-but-not-asked: never a "your step" suggestion
        expect(jf.locator('[data-testid="assistant-suggestion"][data-id="eligible"]')).to_be_visible(timeout=WAIT)
        assert jf.locator('[data-testid="assistant-suggestion"][data-id="my_step"]').count() == 0
        jf.get_by_test_id("assistant-launch").click()
        # review finding (gallery R3): the journey and the next-step text agree with the task frame; permission is not "your action"
        draft_row = jp.locator('[data-testid="journey"] li[data-step="draft"]')
        expect(draft_row).to_have_attribute("data-mine", "false")
        expect(draft_row.get_by_test_id("journey-state")).not_to_have_text("Your action")
        nxt = jf.get_by_test_id("next-action")
        expect(nxt).to_have_attribute("data-inline", "true")
        expect(nxt).to_have_attribute("data-who", "The requester")
        expect(nxt.get_by_test_id("check-now")).to_be_visible()                                         # waiting: only the checking controls, no repeated sentence
        assert jp.locator('[data-testid="next-action"]').count() == 1                                  # one responsibility summary: no second banner
        _shot(jp, "3_task_frame_judge_eligible")
        assert jp.locator(f'[data-testid="work-todo"][data-proposal="{iid}"]').count() == 0
        assert not jp._errors, jp._errors
        jp.context.close()
    finally:
        server.stop()
