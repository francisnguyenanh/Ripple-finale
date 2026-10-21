"""EUI-01 / EUI-04 accessibility checks of the request task frame that no other test covered (2026-10-21, desktop-first): the frame, the whole-request map with the
phase badges (changed in b1f06570), the private assistant panel and the review rail at 1024 px, in the dark theme, at 200% zoom (640 CSS px at device scale 2) and under
reduced motion. Scoped axe (WCAG 2.2 AA + best practice, colour contrast included) on each surface, no page-level sideways scroll, no running spinner while nothing runs,
and the stepper's visible focus. Real server, worker, Chromium and routes on the throwaway PostgreSQL; SYNTHETIC model (no paid call; the assistant is only opened).
Screenshots go to UI_NEXT_SHOTS when set, for the owner's look. Keyboard paths of the same surfaces are already covered by test_request_assistant_browser.py."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT  # noqa: E402
from tests.final_loop.test_request_assistant_browser import rq_ui  # noqa: E402,F401
from tests.ui_next.test_a11y import scoped_violations  # noqa: E402

VIEWS = [("light-1024", "light", 1024, 768, 1, "no-preference"), ("dark-1440", "dark", 1440, 900, 1, "no-preference"),
         ("light-zoom200", "light", 640, 450, 2, "no-preference"), ("dark-zoom200-reduced-motion", "dark", 640, 450, 2, "reduce")]


def _open(browser, server, env, cid, scheme, w, h, scale, motion):
    ctx = browser.new_context(base_url=server.url, viewport={"width": w, "height": h}, device_scale_factor=scale, color_scheme=scheme, reduced_motion=motion)
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    assert page.goto(server.url + "/app-next/").status == 200
    page.get_by_label("Account ID").fill(f"approver-{env.nonce}")
    page.get_by_label("Credential").fill("cv")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("session-account")).to_have_text(f"approver-{env.nonce}", timeout=WAIT)
    if w < 1024:
        page.get_by_test_id("menu-button").click() if page.get_by_test_id("menu-button").is_visible() else None
    page.get_by_label("Workspace").select_option(value=env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=all&chg={cid}")
    return ctx, page


def _no_sideways_scroll(page):
    return page.evaluate("() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1")


def _shot(page, name):
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"eui_a11y_{name}.png"), full_page=False)


@pytest.mark.parametrize("name,scheme,w,h,scale,motion", VIEWS, ids=[v[0] for v in VIEWS])
def test_task_frame_map_assistant_and_rail_at_1024_dark_zoom_and_reduced_motion(rq_ui, pw_browser, name, scheme, w, h, scale, motion):
    q, model, server, cid = rq_ui
    ctx, page = _open(pw_browser, server, q.env, cid, scheme, w, h, scale, motion)
    try:
        frame = page.get_by_test_id("task-frame")
        expect(frame).to_be_visible(timeout=WAIT)
        frame.scroll_into_view_if_needed()
        assert _no_sideways_scroll(page), name
        assert scoped_violations(page, '[data-testid="task-frame"]') == [], name                        # incl. colour contrast in this theme
        _shot(page, f"{name}_1_task")
        frame.get_by_test_id("task-mode-map").click()
        decision = frame.locator('[data-testid="task-map-step"][data-step="knowledge_decision"]')
        expect(decision).to_have_attribute("data-phase", "awaiting_decision")
        expect(decision.get_by_test_id("status-badge")).to_have_text("Waiting for a decision")
        expect(frame.locator('[data-testid="task-map"] [data-testid="status-badge"][data-state="running"]')).to_have_count(0)
        assert _no_sideways_scroll(page), name
        assert scoped_violations(page, '[data-testid="task-map"]') == [], name                           # the new phase badges, both themes
        button = frame.locator("#task-step-knowledge_decision")
        button.focus()
        page.keyboard.press("Shift+Tab")                                                        # reach it by keyboard so :focus-visible applies
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement && document.activeElement.id") == "task-step-knowledge_decision", name
        outline = button.evaluate("e => getComputedStyle(e).outlineStyle")
        assert outline != "none", f"{name}: the focused step shows no outline"
        _shot(page, f"{name}_2_map")
        if motion == "reduce":                                                                  # reduced motion: nothing animates (no spinner, no transition loop)
            moving = page.evaluate("() => [...document.querySelectorAll('*')].filter(e => { const s = getComputedStyle(e); "
                                   "return s.animationName !== 'none' && s.animationPlayState === 'running' && parseFloat(s.animationDuration) > 0.01 }).length")
            assert moving == 0, f"{name}: {moving} element(s) still animate under reduced motion"
        frame.get_by_test_id("assistant-launch").click()
        panel = frame.get_by_test_id("request-assistant")
        expect(panel.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)
        assert _no_sideways_scroll(page), name
        assert scoped_violations(page, '[data-testid="request-assistant"]') == [], name
        assert model.calls == 0                                                                  # opening the panel never calls the model
        _shot(page, f"{name}_3_assistant")
        rail = page.get_by_test_id("rev-workspace")
        if rail.count():
            rail.scroll_into_view_if_needed()
            assert scoped_violations(page, '[data-testid="rev-workspace"]') == [], name
            _shot(page, f"{name}_4_rail")
    finally:
        ctx.close()
