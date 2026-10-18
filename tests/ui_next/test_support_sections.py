"""UX-14: the Support & Access section navigator. Default shows every section; choosing one shows only it, keeps everything mounted,
makes no request, and an unknown ui_section value is ignored. Role/lease/operator controls are unchanged. Stub providers only."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000
SECTIONS = ["ws", "access", "sandbox", "matrix", "live"]


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, env, query=""):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._calls = []
    page.on("request", lambda r: page._calls.append((r.method, r.url)))
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/support{query}")
    expect(tid(page, "support-sections")).to_be_visible(timeout=WAIT)
    return page, ctx


def test_default_shows_all_and_one_section_can_be_selected(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env)
        for sid in SECTIONS:
            expect(tid(page, f"ssection-{sid}")).to_be_visible()
        before = len(page._calls)
        tid(page, "ssec-matrix").click()
        expect(tid(page, "ssec-matrix")).to_have_attribute("aria-current", "true")
        expect(tid(page, "ssection-matrix")).to_be_visible()
        for sid in ("ws", "access", "sandbox"):
            expect(tid(page, f"ssection-{sid}")).to_be_hidden()
        assert "ui_section=matrix" in page.url
        page.wait_for_timeout(400)
        # switching must not WRITE anything or start work; the session's own status polling (GET sandbox/status) may still run
        assert not [c for c in page._calls[before:] if c[0] != "GET"], page._calls[before:]
        assert not [c for c in page._calls[before:] if "/v2/" in c[1] and "/v2/sandbox/status" not in c[1]], page._calls[before:]
        tid(page, "ssec-all").click()
        expect(tid(page, "ssection-ws")).to_be_visible()
        ctx.close()
    finally:
        server.stop()


def test_deep_link_and_unknown_value(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "?ui_section=access")
        expect(tid(page, "ssection-access")).to_be_visible()
        expect(tid(page, "ssection-matrix")).to_be_hidden()
        ctx.close()
        page, ctx = _open(pw_browser, server, env, "?ui_section=bogus")
        for sid in SECTIONS:
            expect(tid(page, f"ssection-{sid}")).to_be_visible()
        ctx.close()
    finally:
        server.stop()


def test_live_updates_section_reports_polling_and_the_sink_and_an_unreadable_status_is_not_up_to_date(env, ch, pw_browser):
    """X3a (M5 option 3): read-only; delivery is polling; the sink state and the feeds the role may read are reported; a failed read is 'could not be read'
    (with the server's reference when it sent one), never 'up to date'; nothing is started or written."""
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "?ui_section=live")
        live = tid(page, "live-status")
        expect(live).to_have_attribute("data-state", "read", timeout=WAIT)
        expect(tid(page, "live-delivery")).to_contain_text("checks the server by itself")
        expect(tid(page, "live-sink")).to_contain_text("No hint sink is configured")
        expect(tid(page, "live-feeds")).to_contain_text("never carries data")
        assert not [c for c in page._calls if c[0] != "GET" and not c[1].endswith("/v2/auth/login")], page._calls   # read-only (the test's own sign-in aside)
        ctx.close()
        page, ctx = _open(pw_browser, server, env)
        page.route("**/v2/projection/status*", lambda r: r.fulfill(status=503, content_type="application/json", body='{"error":"v2 unavailable"}',
                                                                  headers={"X-Request-Id": "0123456789abcdef0123456789abcdef"}))
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/support?ui_section=live")
        expect(tid(page, "live-status")).to_have_attribute("data-state", "unavailable", timeout=WAIT)
        expect(tid(page, "live-unavailable")).to_contain_text("not the same as everything being up to date")
        expect(tid(page, "live-request-id")).to_contain_text("0123456789abcdef0123456789abcdef")
        ctx.close()
    finally:
        server.stop()
