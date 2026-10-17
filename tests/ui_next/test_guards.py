"""Behaviour guards of the new interface: no duplicate job on a double click, polling never takes away what a person is typing,
keyboard and focus rules, reduced motion, 200% zoom. Stub providers only (SIMULATED); no provider call is ever made."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build, intent as make_intent, S1  # noqa: E402
from tests.pitch_brief.test_intent_chat import kinds  # noqa: E402

from tests.pitch_brief.conftest import checks_ready  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _page(browser, server, who, **kw):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900}, **kw)
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._errors, page._calls = [], []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    page.on("request", lambda r: page._calls.append((r.method, r.url)))
    open_app(page, server.url)
    login(page, *who)
    return page, ctx


def test_double_click_on_generate_draft_creates_exactly_one_job(env, ch, pw_browser):
    iid = make_intent(env, ch.admin, S1, confirm=True)["intent_id"]
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/changes?intent={iid}")
        expect(tid(page, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=WAIT)
        before = kinds(env).get("change.draft", 0)
        checks_ready(env)
        tid(page, "action-run-draft").dblclick()
        expect(tid(page, "state-draft")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        page.wait_for_timeout(1500)
        assert kinds(env).get("change.draft", 0) - before == 1, kinds(env)
        posts = [c for c in page._calls if c[0] == "POST" and c[1].endswith("/v2/jobs")]
        assert len(posts) == 1, posts
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_polling_does_not_take_away_typed_text_selection_or_focus(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Changes")
        tid(page, "chat-input").fill("please lower the limit that managers can approve")
        tid(page, "chat-start").click()
        expect(tid(page, "chat-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)      # the AI is replying: the page keeps looking
        reply = tid(page, "chat-reply")
        reply.click()
        reply.fill("My answer is still being typed and must survive every refresh")
        reply.evaluate("e => e.setSelectionRange(3, 9)")
        before = len([c for c in page._calls if "/v2/chat/chat-" in c[1] or "/v2/workspaces/" in c[1]])
        page.wait_for_function("() => true")
        page.wait_for_timeout(7500)                                                   # longer than three live polls (2 s)
        after = len([c for c in page._calls if "/v2/chat/chat-" in c[1] or "/v2/workspaces/" in c[1]])
        assert after > before, "the waiting page should have looked again by itself"
        assert reply.input_value() == "My answer is still being typed and must survive every refresh"
        assert page.evaluate("document.activeElement.dataset.testid") == "chat-reply"
        assert reply.evaluate("e => [e.selectionStart, e.selectionEnd]") == [3, 9]
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_keyboard_only_menu_skip_link_and_focus_return_on_a_small_screen(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"), )
        page.set_viewport_size({"width": 390, "height": 844})
        select_workspace(page, env.ws)
        # the skip link is the first Tab stop and moves focus to the main content
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.textContent.trim()") == "Skip to content"
        page.keyboard.press("Enter")
        assert page.evaluate("document.activeElement.id") == "main"
        # the menu opens from the keyboard; Escape closes it and puts focus back on the button
        tid(page, "menu-button").focus()
        page.keyboard.press("Enter")
        expect(tid(page, "nav-changes")).to_be_visible()
        page.keyboard.press("Escape")
        expect(tid(page, "nav-changes")).not_to_be_visible()
        assert page.evaluate("document.activeElement.dataset.testid") == "menu-button"
        ctx.close()
    finally:
        server.stop()


def test_reduced_motion_removes_animation_and_zoom_200_keeps_everything_reachable(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"), reduced_motion="reduce")
        select_workspace(page, env.ws)
        goto_view(page, "Changes")
        expect(tid(page, "chat-panel")).to_be_visible()
        names = page.evaluate("() => [...document.querySelectorAll('*')].map(e => getComputedStyle(e).animationName).filter(n => n && n !== 'none').length")
        assert names == 0, "an animation is still running under prefers-reduced-motion"
        # 200% zoom == a 640 CSS px wide window: no horizontal page scroll, the menu is reachable
        page.set_viewport_size({"width": 640, "height": 450})
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        tid(page, "menu-button").click()
        expect(tid(page, "nav-explorer")).to_be_visible()
        ctx.close()
    finally:
        server.stop()
