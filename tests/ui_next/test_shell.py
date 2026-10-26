"""Slice A acceptance: shell, real login, deep-link restore, Business/Technical toggle, CSP, viewports.

Runs the real infra.judge app behind uvicorn with the BUILT frontend (app/ui_next).
Stub providers only: SIMULATED, not live evidence.
"""
from __future__ import annotations

import os
import pathlib
from contextlib import contextmanager

import pytest
from playwright.sync_api import expect

from tests.ai10ui.harness import ServerThread

WAIT = 30000
SHOTS = os.environ.get("UI_NEXT_SHOTS")
BUILT = pathlib.Path(__file__).resolve().parents[2] / "app" / "ui_next" / "index.html"
pytestmark = pytest.mark.skipif(not BUILT.is_file(), reason="run `npm run build` in frontend/ first")


def tid(page, name):
    return page.get_by_test_id(name)


@contextmanager
def served():
    server = ServerThread()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _ctx(browser, server, **kw):
    c = browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900}, **kw)
    p = c.new_page()
    p.set_default_timeout(WAIT)
    p._errors, p._requests = [], []
    p.on("pageerror", lambda e: p._errors.append(str(e)))
    # A 401 from /v2/auth/me while signed out is the expected "not signed in" probe, not a defect.
    p.on("console", lambda m: p._errors.append(m.text) if m.type == "error" and "401" not in m.text and "404" not in m.text else None)
    # 404 from /v2/sandbox/status means "this workspace is not a sandbox": expected, not a defect.
    p.on("response", lambda r: p._errors.append(f"HTTP {r.status} {r.url}") if r.status == 404 and "/v2/sandbox/status" not in r.url else None)
    p.on("request", lambda r: p._requests.append((r.method, r.url)))
    return c, p


def _login(page, account, credential):
    page.get_by_label("Account ID").fill(account)
    page.get_by_label("Credential").fill(credential)
    page.get_by_role("button", name="Sign in").click()
    expect(tid(page, "session-account")).to_have_text(account)


def test_two_people_sign_in_separately_and_see_the_role_aware_shell(env, pw_browser):
    with served() as server:
        seen = []
        for acct, cred in [(env.admin, f"cred-a-{env.nonce}"), (env.req, f"cred-r-{env.nonce}")]:
            c, p = _ctx(pw_browser, server)
            p.goto("/app-next/")
            _login(p, acct, cred)
            expect(tid(p, "page-title")).to_have_text("Overview")
            for nav in ("overview", "sources", "explorer", "questions", "changes", "impact", "history", "support"):
                expect(tid(p, f"nav-{nav}")).to_be_visible()
            expect(tid(p, "workspace-roles")).to_be_visible()
            assert p._errors == [], p._errors
            seen.append(tid(p, "session-account").inner_text())
            c.close()
        assert seen[0] != seen[1]


def test_copied_link_signed_out_then_sign_in_lands_on_the_same_page(env, pw_browser):
    with served() as server:
        c, p = _ctx(pw_browser, server)
        p.goto(f"/app-next/#/ws/{env.ws}/changes?chat=abc")
        expect(tid(p, "login-context")).to_be_visible()
        _login(p, env.req, f"cred-r-{env.nonce}")
        expect(tid(p, "page-title")).to_have_text("Changes")
        assert p.evaluate("location.hash") == f"#/ws/{env.ws}/changes?chat=abc"
        tid(p, "account-menu-button").focus()                       # SCN-05: account and sign-out are a disclosure, keyboard first
        p.keyboard.press("Enter")
        expect(tid(p, "signout")).to_be_focused()
        p.keyboard.press("Escape")
        expect(tid(p, "account-menu")).to_have_count(0)
        assert p.evaluate("document.activeElement.dataset.testid") == "account-menu-button"
        tid(p, "account-menu-button").click()
        tid(p, "signout").click()
        expect(p.get_by_label("Account ID")).to_be_visible()
        assert "changes" not in p.evaluate("location.hash")
        c.close()


def test_wrong_credential_is_refused_and_a_foreign_workspace_shows_nothing(env, pw_browser):
    with served() as server:
        c, p = _ctx(pw_browser, server)
        p.goto("/app-next/")
        p.get_by_label("Account ID").fill(env.req)
        p.get_by_label("Credential").fill("wrong")
        p.get_by_role("button", name="Sign in").click()
        expect(tid(p, "login-error")).to_contain_text("not accepted")
        _login(p, env.req, f"cred-r-{env.nonce}")
        p.goto("/app-next/#/ws/some-foreign-workspace/overview")
        expect(tid(p, "link-unavailable")).to_be_visible()
        c.close()


def test_mode_toggle_makes_no_request_and_keeps_focus(env, pw_browser):
    with served() as server:
        c, p = _ctx(pw_browser, server)
        p.goto(f"/app-next/#/ws/{env.ws}/overview")
        _login(p, env.admin, f"cred-a-{env.nonce}")
        expect(tid(p, "page-title")).to_have_text("Overview")
        expect(tid(p, "head-source-revision")).to_have_count(0)
        p.wait_for_load_state("networkidle")

        def api_calls():
            return [r for r in p._requests if "/v2/" in r[1]]

        before = len(api_calls())
        tid(p, "mode-technical").focus()
        tid(p, "mode-technical").press("Enter")
        expect(tid(p, "head-source-revision")).to_be_visible()
        assert p.evaluate("document.activeElement.dataset.testid") == "mode-technical"
        tid(p, "mode-business").click()
        expect(tid(p, "head-source-revision")).to_have_count(0)
        p.wait_for_timeout(500)
        assert len(api_calls()) == before, api_calls()[before:]
        c.close()


def test_csp_clean_dialog_focus_and_react_flow_dynamic_position(env, pw_browser):
    with served() as server:
        c, p = _ctx(pw_browser, server)
        p.goto(f"/app-next/#/ws/{env.ws}/lab")
        _login(p, env.admin, f"cred-a-{env.nonce}")
        tid(p, "lab-open").click()
        expect(p.get_by_role("dialog")).to_be_visible()
        p.keyboard.press("Escape")
        expect(p.get_by_role("dialog")).to_have_count(0)
        assert p.evaluate("document.activeElement.dataset.testid") == "lab-open"
        node = p.locator(".react-flow__node").first
        expect(node).to_be_visible()
        t0 = node.evaluate("e => e.style.transform")
        tid(p, "lab-move").click()
        p.wait_for_function("(t) => document.querySelector('.react-flow__node').style.transform !== t", arg=t0)
        csp = [e for e in p._errors if "Content Security Policy" in e or "Refused to" in e]
        assert csp == [], csp
        assert p._errors == [], p._errors
        c.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (1280, 800), (1024, 768), (390, 844)])
def test_viewports_have_no_horizontal_scroll_and_a_reachable_menu(env, pw_browser, w, h):
    with served() as server:
        c, p = _ctx(pw_browser, server)
        p.set_viewport_size({"width": w, "height": h})
        p.goto(f"/app-next/#/ws/{env.ws}/overview")
        _login(p, env.admin, f"cred-a-{env.nonce}")
        expect(tid(p, "page-title")).to_have_text("Overview")
        assert p.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), "horizontal scroll"
        if SHOTS:
            pathlib.Path(SHOTS).mkdir(parents=True, exist_ok=True)
            p.screenshot(path=str(pathlib.Path(SHOTS) / f"sliceA_overview_{w}.png"))
        if w < 1024:
            expect(tid(p, "nav-changes")).not_to_be_visible()
            tid(p, "menu-button").click()
            expect(tid(p, "nav-changes")).to_be_visible()
            tid(p, "nav-changes").click()
            expect(tid(p, "page-title")).to_have_text("Changes")
        c.close()
