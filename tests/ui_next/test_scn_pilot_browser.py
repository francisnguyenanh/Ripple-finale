"""Plan 17 pilot (SCN-02 / 03 / 04) on the React build: real server, worker, Chromium and routes on the throwaway PostgreSQL; SYNTHETIC assistant model (no paid call).
- Overview counts are request entries of the loaded list per server group; a count filters the list (no request, no mutation) and can be cleared; an empty group says so.
- A cut candidate list (`coverage.complete` false) and a failed read are stated; a failed read shows Unavailable, never 0.
- The task header states the step, who holds it and the reader's part from the projection: asked approver, waiting requester, eligible-not-asked administrator.
- The assistant: opening calls no model; a typed draft survives resizes; below 1280 px a citation jump offers "Back to conversation", which returns focus to the composer."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra import v2_work_routes  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_request_assistant_browser import rq_ui  # noqa: E402,F401
from tests.pitch_brief.conftest import env  # noqa: E402,F401


def _posts(page):
    page._posts = []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" else None)


def test_counts_filter_the_loaded_list_and_the_header_states_each_readers_part(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    page.set_viewport_size({"width": 1440, "height": 900})
    _posts(page)
    mine = page.get_by_test_id("count-mine")
    expect(mine).to_have_attribute("data-count", "2", timeout=WAIT)                       # two requests wait for this approver (both families' rule: projection only)
    assert page.locator('[data-testid="work-todo"][data-section="mine"]').count() == 2
    expect(page.get_by_test_id("count-blocked")).to_have_attribute("data-count", "0")
    expect(page.get_by_test_id("overview-counts-scope")).to_contain_text("loaded list")
    page.get_by_test_id("count-blocked").click()                                          # an empty group: said, not a blank list
    expect(page.get_by_test_id("work-filter")).to_contain_text("Blocked")
    expect(page.get_by_test_id("work-filter-none")).to_be_visible()
    assert page.locator('[data-testid="work-todo"]').count() == 0
    page.get_by_test_id("count-mine").click()
    expect(page.get_by_test_id("count-mine")).to_have_attribute("aria-pressed", "true")
    assert page.locator('[data-testid="work-todo"][data-section="mine"]').count() == 2 and page.get_by_test_id("work-section-recent").count() == 0
    page.get_by_test_id("work-filter-clear").click()
    expect(page.get_by_test_id("work-filter")).to_have_count(0)
    assert not page._posts, page._posts                                                   # filtering is presentation only
    page.locator(f'[data-testid="work-todo"][data-proposal="{cid}"]').get_by_test_id("work-open-todo").click()
    frame = page.get_by_test_id("task-frame")
    expect(frame).to_have_attribute("data-asked", "true", timeout=WAIT)
    expect(frame.get_by_test_id("task-holder")).to_contain_text("You")
    expect(frame.get_by_test_id("task-your-part")).to_contain_text("Your step now")
    expect(frame.get_by_test_id("task-stage")).to_contain_text("Waiting for the knowledge decision")
    expect(frame.get_by_test_id("task-as-of")).to_contain_text("Task status read")
    expect(page.get_by_test_id("next-step")).to_have_count(0)                             # the queue line does not contradict the open request
    assert frame.bounding_box()["y"] < 900                                                # the task starts on the first screen (SCN-00 rubric R1)
    page.context.close()
    rp = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash=f"questions?chg={cid}")
    rf = rp.get_by_test_id("task-frame")
    expect(rf).to_have_attribute("data-asked", "false", timeout=WAIT)
    expect(rf.get_by_test_id("task-holder")).to_contain_text("an approver")
    expect(rf.get_by_test_id("task-your-part")).to_contain_text("Nothing for you now")
    rp.context.close()
    ap = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash=f"questions?chg={cid}")
    af = ap.get_by_test_id("task-frame")
    expect(af.get_by_test_id("task-your-part")).to_contain_text("not asked", timeout=WAIT)  # eligible, not obligated: never "You"
    expect(af.get_by_test_id("task-holder")).not_to_contain_text("You")
    ap.context.close()


def test_a_cut_list_and_a_failed_read_are_stated_never_shown_as_zero(rq_ui, pw_browser, monkeypatch):
    q, model, server, cid = rq_ui
    env = q.env
    monkeypatch.setattr(v2_work_routes, "INBOX_LIMIT", 1)                                 # more business changes than the server reads
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    expect(page.get_by_test_id("work-cut")).to_be_visible(timeout=WAIT)
    expect(page.get_by_test_id("count-mine")).to_have_attribute("data-count", "1")
    page.route("**/v2/work/inbox*", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}'))
    page.reload()
    expect(page.get_by_test_id("work-failed")).to_be_visible(timeout=WAIT)
    for k in ("mine", "waiting_human", "blocked"):
        expect(page.get_by_test_id(f"count-{k}")).to_have_attribute("data-count", "unavailable")
        expect(page.get_by_test_id(f"count-{k}")).to_contain_text("Unavailable")
    page.context.close()


def test_assistant_draft_survives_resize_and_a_citation_jump_returns_to_the_conversation(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash=f"questions?chg={cid}")
    page.set_viewport_size({"width": 1440, "height": 900})
    frame = page.get_by_test_id("task-frame")
    expect(frame).to_be_visible(timeout=WAIT)
    frame.get_by_test_id("assistant-launch").click()
    panel = frame.get_by_test_id("request-assistant")
    expect(panel.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)
    expect(panel.get_by_test_id("assistant-scope")).to_contain_text("About:")
    panel.get_by_test_id("assistant-question").fill("A draft I am still writing")
    for w, h in ((1024, 768), (390, 844), (1440, 900)):                                    # the panel moves (rail / below) without being recreated
        page.set_viewport_size({"width": w, "height": h})
        expect(panel.get_by_test_id("assistant-question")).to_have_value("A draft I am still writing")
    assert model.calls == 0                                                                # opening, typing and resizing: no model call
    panel.get_by_test_id("assistant-question").fill("What should I do next?")
    panel.get_by_test_id("assistant-ask").click()
    msg = panel.locator('[data-testid="assistant-message"]').first
    expect(msg).to_have_attribute("data-state", "answered", timeout=60000)
    assert model.calls == 1
    msg.locator('[data-testid="assistant-citation-show"][data-kind="state"]').first.click()   # desktop: the rail stays beside the target, no extra button
    expect(page.get_by_test_id("assistant-back")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    msg.locator('[data-testid="assistant-citation-show"][data-kind="state"]').first.click()
    back = page.get_by_test_id("assistant-back")
    expect(back).to_be_visible()
    back.click()
    expect(panel.get_by_test_id("assistant-question")).to_be_focused()
    expect(back).to_have_count(0)
    assert model.calls == 1                                                                # citations and navigation: no model call
    assert page.evaluate("document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1")
    page.context.close()
