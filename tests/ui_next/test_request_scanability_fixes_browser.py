"""Revision 2 PC-07a (2026-10-27): the three scanability observations of the agent comprehension test (HK-04) on the React build — real server, Chromium, routes
and store on the throwaway PostgreSQL; no model call.
- S4a: a requester asked to ask for the AI draft gets "Go to the draft control", which only scrolls to and focuses the control (no request is sent).
- S5b: on a 390 px phone the header's role is shown whole (wrapped, never cut), with no sideways page scroll.
- S5b: a request whose title is its id fallback says it has no title yet; the id stays as it was."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_request_task_view import ch  # noqa: E402,F401  (fixture)
from tests.pitch_brief.chain import S1, intent  # noqa: E402


def _shot(page, name):
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"r2_{name}.png"), full_page=False)


def test_draft_jump_phone_role_and_untitled_label(env, ch, pw_browser):
    iid = intent(env, env.req_client(), S1)["intent_id"]                                       # confirmed by the requester, no AI draft yet
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash="overview")
        page.set_viewport_size({"width": 1440, "height": 900})
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        row = page.locator(f'[data-testid="work-todo"][data-section="mine"][data-proposal="{iid}"]')
        expect(row).to_be_visible(timeout=WAIT)
        row.get_by_test_id("work-open-todo").click()
        frame = page.get_by_test_id("task-frame")
        expect(frame).to_have_attribute("data-stage", "ASK_DRAFT", timeout=WAIT)
        jump = frame.get_by_test_id("task-jump")
        expect(jump).to_have_text("Go to the draft control")
        _shot(page, "s4a_task_with_draft_jump_1440")
        before = len(posts)
        page.emulate_media(reduced_motion="reduce")
        jump.click()
        assert page.evaluate("() => document.activeElement && document.activeElement.id") == "action-run-draft"
        assert page.evaluate("() => { const r = document.activeElement.getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; }")
        assert len(posts) == before, posts[before:]                                          # scroll and focus only: no draft is started
        _shot(page, "s4a_after_jump_focus_1440")
        # S5b: the phone header keeps the role whole
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/overview")
        role = page.get_by_test_id("header-role")
        expect(role).to_be_visible(timeout=WAIT)
        assert role.evaluate("(e) => e.scrollWidth <= e.clientWidth + 1"), "the role is cut"
        assert role.inner_text().strip() == "requester"
        assert not page.evaluate("() => document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1")
        _shot(page, "s5b_phone_header_role_390")
        # S5b: a title that is only the id fallback says so
        def untitled(route):
            resp = route.fetch()
            body = resp.json()
            for it in list(body.get("todo", [])) + list((body.get("sections") or {}).get("mine", [])):
                if it.get("proposal_job_id") == iid:
                    it["title"] = iid
            route.fulfill(response=resp, json=body)
        page.route("**/v2/work/inbox*", untitled)
        page.reload()
        r2 = page.locator(f'[data-testid="work-todo"][data-proposal="{iid}"]')
        expect(r2.get_by_test_id("work-title")).to_have_attribute("data-fallback", "true", timeout=WAIT)
        expect(r2.get_by_test_id("work-title-untitled")).to_have_text("· no title yet")
        assert iid in r2.get_by_test_id("work-title").text_content()                         # the full id stays available
        _shot(page, "s5b_untitled_label_390")
        page.context.close()
    finally:
        server.stop()
