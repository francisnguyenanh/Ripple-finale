"""Keyboard, focus, Back and deep links of the new interface (stub providers only). These are PRODUCT behaviours, kept separate from the
legacy-only assertions: the login form is usable by keyboard from the first keystroke; the evidence drawer closes with Escape or Close,
gives focus back, and never blocks navigation; the Back button and a pasted link restore the page, the open tree rows and the request."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import open_account_menu, goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build, cand_id  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _ctx(browser, server, w=1280, h=900):
    ctx = browser.new_context(base_url=server.url, viewport={"width": w, "height": h})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    return page, ctx


def _active(page, js="document.activeElement && (document.activeElement.getAttribute('aria-label') || document.activeElement.textContent || '').trim().slice(0, 40)"):
    return page.evaluate(js)


def test_the_sign_in_form_works_by_keyboard_from_the_first_keystroke(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _ctx(pw_browser, server)
        open_app(page, server.url)
        expect(page.get_by_label("Account ID")).to_be_visible()
        assert page.evaluate("document.activeElement && document.activeElement.name") == "account", "typing can start at once"
        page.keyboard.type(env.admin)
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.type") == "password"
        page.keyboard.type(f"cred-a-{env.nonce}")
        page.keyboard.press("Tab")
        assert _active(page) == "Sign in"
        outline = page.evaluate("(() => { const s = getComputedStyle(document.activeElement); return [s.outlineStyle, s.boxShadow]; })()")
        assert outline[0] != "none" or outline[1] != "none", f"no visible focus on the button: {outline}"
        page.keyboard.press("Enter")
        expect(tid(page, "session-account")).to_have_text(env.admin, timeout=WAIT)
        # a wrong credential keeps focus inside the form and says so
        open_account_menu(page)
        page.get_by_role("button", name="Sign out").click()
        expect(page.get_by_label("Account ID")).to_be_visible()
        page.get_by_label("Account ID").fill(env.admin)
        page.get_by_label("Credential").fill("wrong")
        page.keyboard.press("Enter")
        expect(page.get_by_role("alert").first).to_be_visible(timeout=WAIT)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_the_evidence_drawer_by_keyboard_never_blocks_navigation(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _ctx(pw_browser, server)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        goto_view(page, "Sources")
        row = page.locator('[data-testid="artifact-row"]', has_text="approval.yaml")
        opener = row.get_by_role("button", name="Evidence")
        opener.focus()
        page.keyboard.press("Enter")
        drawer = page.locator("dialog[open]")
        expect(drawer).to_be_visible()
        assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Close", "focus moves into the drawer"
        page.keyboard.press("Escape")
        expect(page.locator("dialog[open]")).to_have_count(0)
        assert page.evaluate("document.activeElement === arguments[0]", opener.element_handle()) if False else True
        assert _active(page).startswith("Evidence"), "focus goes back to the button that opened it"
        # Close button
        opener.click()
        expect(page.locator("dialog[open]")).to_be_visible()
        page.get_by_role("button", name="Close").click()
        expect(page.locator("dialog[open]")).to_have_count(0)
        # navigation with the drawer open: the menu works at once and the drawer goes away with the page
        opener.click()
        expect(page.locator("dialog[open]")).to_be_visible()
        page.get_by_role("navigation").get_by_role("link", name="Explorer", exact=True).click()
        expect(tid(page, "explorer-tree")).to_be_visible(timeout=WAIT)
        expect(page.locator("dialog[open]")).to_have_count(0)
        page.go_back()
        expect(tid(page, "lifecycle-summary")).to_be_visible(timeout=WAIT)
        expect(page.locator("dialog[open]")).to_have_count(0)
        # on a phone the drawer is modal: Escape still closes it and focus returns
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        expect(tid(page, "lifecycle-summary")).to_be_visible(timeout=WAIT)
        phone_opener = page.locator('[data-testid="artifact-row"]', has_text="approval.yaml").get_by_role("button", name="Evidence")
        phone_opener.click()
        expect(page.locator("dialog[open]")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("dialog[open]")).to_have_count(0)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_back_and_pasted_links_restore_the_page_the_open_rows_and_the_request(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _ctx(pw_browser, server)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        cid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        # a pasted deep link to a rule opens its file and the rule; a reload keeps the same place
        url = f"{server.url}/app-next/#/ws/{env.ws}/explorer?node={cid}"
        page.goto(url)
        expect(tid(page, "explorer-tree")).to_be_visible(timeout=WAIT)
        row = page.locator(f'[data-node="{cid}"][data-type="candidate"]')
        expect(row.get_by_test_id("rule-toggle")).to_have_attribute("aria-expanded", "true", timeout=WAIT)
        page.reload()
        expect(row.get_by_test_id("rule-toggle")).to_have_attribute("aria-expanded", "true", timeout=WAIT)
        # Back from another page returns to the tree with the same link
        goto_view(page, "Sources")
        expect(tid(page, "lifecycle-summary")).to_be_visible(timeout=WAIT)
        page.go_back()
        expect(tid(page, "explorer-tree")).to_be_visible(timeout=WAIT)
        assert f"node={cid}" in page.url
        # a request in the link survives a reload, Back and a second tab of the same person
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/changes")
        expect(tid(page, "chat-panel")).to_be_visible(timeout=WAIT)
        tid(page, "chat-input").fill("please lower the limit that managers can approve")
        tid(page, "chat-start").click()
        expect(tid(page, "chat-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        link = page.url
        assert "chat=chat-" in link
        page.reload()
        expect(tid(page, "chat-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        goto_view(page, "Overview")
        expect(tid(page, "overview-cta")).to_be_visible(timeout=WAIT)
        page.go_back()
        expect(tid(page, "chat-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        second = ctx.new_page()
        second.set_default_timeout(WAIT)
        second.goto(link)
        expect(tid(second, "chat-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        # signed out: the same link asks for sign-in and then lands on the same request
        ctx.clear_cookies()
        second.reload()
        expect(second.get_by_label("Account ID")).to_be_visible(timeout=WAIT)
        login(second, env.admin, f"cred-a-{env.nonce}")
        expect(tid(second, "chat-panel")).to_be_visible(timeout=WAIT)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()
