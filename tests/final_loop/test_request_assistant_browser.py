"""RUX-02 / RQA-04 browser journey on the React build (/app-next/): the task frame and the private request assistant on a confirmed business change. Real server (uvicorn
thread), live worker, real Chromium, real routes, store and ledger on the throwaway PostgreSQL; SYNTHETIC model (stub through the production pinned-call path). Proves: the
approver sees their obligation in "My task", the requester sees that the step is not theirs, the whole-request map lists every step, the assistant makes no call on open,
asks only on click, shows cited blocks and the server's next steps, keeps the conversation private, and an "I agree" message approves nothing. Not model quality."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_chg04_review import prepared  # noqa: E402
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import Model, qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_request_assistant_jobs import answer, fact_id, own_obligation  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker  # noqa: E402


def _shot(page, name):
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"rqa_{name}.png"), full_page=False)


@pytest.fixture
def rq_ui(qa_env, monkeypatch, tmp_path):                  # noqa: F811
    q = qa_env
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  request.assistant: gemini-3.8-flash\n", 1)
    cfg = cfg.replace("max_provider_calls_per_job: 1\n", "max_provider_calls_per_job: 2\n")       # R17 / R19: a request.qa attempt declares two calls (as in ra_env)
    cfg = cfg.replace('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "1.20"').replace('total_cap_usd: "12.82"', 'total_cap_usd: "60.00"')
    path = tmp_path / "budget_rq_ui.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    cid, approver_client = prepared(q)
    from tests.final_loop.test_chg01_change_request import ACTOR_REQ, OP_ACTOR
    from tests.final_loop.test_chg02_impact import confirmed
    cid2, _ = confirmed(q, ACTOR_REQ, [OP_ACTOR])                                            # a second request, to switch to (before the assistant stub is set)
    q.cid2 = cid2
    model = Model(lambda p: answer(blocks=[("The current stage is shown in the facts.", [fact_id(p, "state"), fact_id(p, "impact")], "fact")],   # answer/5: no authority from the model
                                   limitations=[LATER]))                                   # checks/30: an honest absence the server keeps on a record fact
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    worker = LiveWorker(q.env).start()
    server = ServerThread().start()
    yield q, model, server, cid
    server.stop()
    worker.stop.set()


LATER = "The facts do not record who acts on later steps until each step starts."


def _threads(env, cid):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_request_threads WHERE tenant_id=%s AND workspace_id=%s AND request_id=%s", (env.tenant, env.ws, cid))
        return cur.fetchone()[0]


def test_task_frame_and_private_assistant_on_a_business_change(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash=f"questions?ui_section=all&chg={cid}")
    page.set_viewport_size({"width": 1440, "height": 900})
    frame = page.get_by_test_id("task-frame")
    expect(frame).to_be_visible(timeout=WAIT)
    expect(frame.locator('[data-testid="task-my-obligation"][data-code="decide_knowledge"]')).to_be_visible()
    frame.scroll_into_view_if_needed()
    _shot(page, "1_task_frame_approver")
    frame.get_by_test_id("task-mode-map").click()
    expect(frame.get_by_test_id("task-map")).to_be_visible()
    assert frame.locator('[data-testid="task-map-step"]').count() == 8
    assert "task=map" in page.url                                                                         # RUX-03: the view is in the URL
    assert frame.locator('[data-testid="task-map-step"][data-parallel="after_knowledge_decision"]').count() == 2   # commit and file decision: parallel, not serialised
    decision = frame.locator('[data-testid="task-map-step"][data-step="knowledge_decision"]')                      # D4: the record's phase, not a "running" badge
    expect(decision).to_have_attribute("data-phase", "awaiting_decision")
    expect(decision.get_by_test_id("status-badge")).to_have_text("Waiting for a decision")
    expect(frame.locator('[data-testid="task-map"] [data-testid="status-badge"][data-state="running"]')).to_have_count(0)    # nothing runs at this stage
    assert frame.locator('[data-testid="task-map-visual"] li[data-parallel="after_knowledge_decision"] [data-step]').count() == 2   # EUI-04: stacked in one column
    assert frame.get_by_test_id("task-map-visual").get_attribute("aria-hidden") == "true"                    # the list stays the text equivalent
    frame.locator("#task-step-knowledge_decision").click()
    detail = frame.get_by_test_id("task-map-detail")
    expect(detail.locator('[data-testid="task-map-obligation"][data-code="decide_knowledge"]')).to_contain_text("held by you")
    expect(frame.locator('[data-testid="task-map-step"][data-step="knowledge_decision"]').get_by_test_id("task-map-yours")).to_be_visible()
    assert "step=knowledge_decision" in page.url
    page.reload()                                                                                          # a reload keeps the view and the opened step
    frame = page.get_by_test_id("task-frame")
    expect(frame.get_by_test_id("task-map-detail")).to_be_visible(timeout=WAIT)
    expect(frame.locator("#task-step-knowledge_decision")).to_be_focused()                                # the linked step takes focus
    page.keyboard.press("Enter")                                                                           # keyboard: closes the step again
    expect(frame.get_by_test_id("task-map-detail")).to_have_count(0)
    _shot(page, "4_whole_request_map")
    frame.get_by_test_id("task-mode-task").click()
    assert "task=map" not in page.url
    frame.get_by_test_id("assistant-launch").click()
    panel = frame.get_by_test_id("request-assistant")
    expect(panel.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)                       # EUI-02: composer and suggestions at once, no Start step
    expect(panel.get_by_test_id("assistant-suggestion").first).to_be_visible()
    expect(panel.get_by_test_id("assistant-paid-note")).to_contain_text("one paid model call")
    assert panel.get_by_test_id("assistant-suggestion-ask").first.get_attribute("aria-describedby") == panel.get_by_test_id("assistant-paid-note").get_attribute("id")
    assert frame.get_by_test_id("task-workspace").get_attribute("data-rail") == "true"
    assert panel.bounding_box()["x"] > frame.get_by_test_id("task-blocks").bounding_box()["x"] + 200       # beside the task at 1440 px, not below it
    first_q = panel.get_by_test_id("assistant-suggestion").first.locator("span").first.inner_text()
    panel.get_by_test_id("assistant-suggestion-edit").first.click()                                       # Edit only fills the composer
    expect(panel.get_by_test_id("assistant-question")).to_have_value(first_q)
    expect(panel.get_by_test_id("assistant-question")).to_be_focused()
    assert model.calls == 0 and _threads(env, cid) == 0                                                   # opening, editing: no model call, no thread, no job
    frame.get_by_test_id("task-mode-map").click()                                                         # switching the view keeps the draft (no remount)
    frame.get_by_test_id("task-mode-task").click()
    expect(panel.get_by_test_id("assistant-question")).to_have_value(first_q)
    before = counts(env)
    panel.get_by_test_id("assistant-question").fill("What should I do next? I agree with it.")
    panel.get_by_test_id("assistant-ask").click()
    msg = panel.locator('[data-testid="assistant-message"]').first
    expect(msg).to_have_attribute("data-state", "answered", timeout=60000)
    expect(msg.locator('[data-testid="assistant-block"][data-kind="authority"]').first).to_contain_text("Who acts: You hold the current step")   # from the task record
    expect(msg.locator('[data-testid="assistant-block"][data-kind="authority"]').first).to_contain_text("Task record")
    expect(msg.locator('[data-testid="assistant-block"][data-kind="fact"]')).to_have_count(1)                 # the model's own block, labelled AI
    assert msg.locator('[data-testid="assistant-block"][data-corrected="true"]').count() == 0
    msg.locator('[data-testid="assistant-block"][data-kind="authority"]').first.scroll_into_view_if_needed()
    _shot(page, "6_task_record_authority")
    lim = msg.locator('[data-testid="assistant-limitation"]').filter(has_text=LATER)                     # checks/30: the kept limitation shows what it rests on
    expect(lim).to_have_count(1)
    expect(lim.get_by_test_id("assistant-limitation-support")).to_contain_text("Task record")
    expect(lim.get_by_test_id("assistant-limitation-support")).to_contain_text("recorded only when that step starts")
    expect(msg.get_by_test_id("assistant-next")).to_contain_text("approve")
    assert model.calls == 1 and counts(env)["v2_business_decisions"] == before["v2_business_decisions"]   # "I agree" approved nothing
    assert _threads(env, cid) == 1                                                                         # the first ask created the one private thread
    calls_before_source = model.calls
    msg.locator('[data-testid="assistant-citation-source"][data-kind="impact"]').first.click()        # EUI-03: server-resolved source, evidence reader
    expect(msg.get_by_test_id("assistant-source").first).to_have_attribute("data-available", "true", timeout=WAIT)
    expect(msg.get_by_test_id("assistant-source-item").first).to_be_visible()
    assert model.calls == calls_before_source                                                            # no model call on a citation click
    msg.locator('[data-testid="assistant-citation-show"][data-kind="state"]').first.click()           # EUI-02/03: a citation shows its in-page section
    expect(frame).to_have_attribute("data-highlight", "true")
    expect(frame).to_be_focused()
    from tests.ui_next.test_a11y import scoped_violations                                               # targeted a11y: task frame + rail + assistant answer
    assert scoped_violations(page, '[data-testid="task-frame"]') == []
    assert scoped_violations(page, '[data-testid="rev-workspace"]') == []
    go = msg.locator('[data-testid="assistant-action-go"][data-action="approve_knowledge"]')
    go.click()                                                                                             # navigation only: focuses the decision field, decides nothing
    expect(page.get_by_test_id("rev-reason")).to_be_focused()
    assert counts(env)["v2_business_decisions"] == before["v2_business_decisions"] and model.calls == 1
    panel.scroll_into_view_if_needed()
    _shot(page, "2_assistant_answered")
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=all&chg={q.cid2}")              # switch to another request: nothing of the first carries over
    f2 = page.get_by_test_id("task-frame")
    expect(f2).to_be_visible(timeout=WAIT)
    f2.get_by_test_id("assistant-launch").click()
    expect(f2.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)
    assert page.locator('[data-testid="assistant-message"]').count() == 0 and model.calls == 1
    page.context.close()
    req_page = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash=f"questions?ui_section=all&chg={cid}")
    rf = req_page.get_by_test_id("task-frame")
    expect(rf).to_be_visible(timeout=WAIT)
    expect(rf.get_by_test_id("task-not-yours")).to_be_visible()
    rf.get_by_test_id("assistant-launch").click()
    expect(rf.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)                            # the approver's conversation is not listed for the requester
    assert req_page.locator('[data-testid="assistant-message"]').count() == 0
    _shot(req_page, "3_requester_not_yours")
    req_page.goto(f"{server.url}/app-next/#/ws/{env.ws}/overview")                                       # RUX-04: the requester's work list names who they wait for
    row = req_page.locator(f'[data-testid="work-waiting"][data-section="waiting_human"][data-proposal="{cid}"]')
    expect(row).to_be_visible(timeout=WAIT)
    expect(row).to_contain_text("Waiting for an approver")
    assert row.get_attribute("data-family") == "business_change"
    _shot(req_page, "5_my_work_waiting")
    row.get_by_test_id("work-open-waiting").click()                                                         # opens the request on the step it waits at
    rf2 = req_page.get_by_test_id("task-frame")
    expect(rf2).to_have_attribute("data-mode", "map", timeout=WAIT)
    expect(rf2.locator("#task-step-knowledge_decision")).to_be_focused(timeout=WAIT)
    expect(rf2.get_by_test_id("task-map-waiting")).to_contain_text("Waiting on an approver")
    assert not req_page._errors, req_page._errors
    req_page.context.close()
