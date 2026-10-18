"""UX-05: a failed read on the Overview is shown as unavailable, never as zero or "nothing waiting", and no "start a change"
step is suggested from missing data. Read failures are injected in the browser; no provider is called (SIMULATED stubs)."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000
FAIL = ("**/v2/hitl/queue*", "**/v2/knowledge/overview*", "**/v2/audit/*")


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, env, fail):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._posts = []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" else None)
    for pattern in fail:
        page.route(pattern, lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}'))
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/overview")
    return page, ctx


def test_overview_shows_unavailable_instead_of_zero_when_reads_fail(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, FAIL)
        expect(tid(page, "overview-unavailable")).to_be_visible(timeout=WAIT)
        expect(tid(page, "activity-unavailable")).to_be_visible()
        stats = page.locator('[data-state="unavailable"]')
        assert stats.count() >= 3, stats.count()
        body = page.inner_text("main")
        assert "Nothing has happened yet" not in body
        assert "Start a change" not in body, "a new change must not be suggested from missing data"
        assert tid(page, "overview-cta").count() == 0
        expect(tid(page, "overview-refresh")).to_be_visible()
        assert not [u for u in page._posts if "/v2/jobs" in u], page._posts
        ctx.close()
    finally:
        server.stop()


def test_overview_with_all_reads_ok_still_has_one_next_action(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, ())
        expect(tid(page, "overview-cta")).to_be_visible(timeout=WAIT)
        expect(tid(page, "next-step")).to_be_visible()
        assert tid(page, "overview-unavailable").count() == 0
        assert page.locator('[data-state="unavailable"]').count() == 0
        expect(page.get_by_role("heading", level=1)).to_have_count(1)
        ctx.close()
    finally:
        server.stop()
