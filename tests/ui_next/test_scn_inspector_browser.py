"""SCN-10 inspector shell on its real consumers (Sources selected file, History version). Real server, Chromium and routes on the throwaway PostgreSQL; stub
providers only; reads only. Placement: a pane from 1280 px, a side drawer below, a sheet on a phone; Escape closes and focus returns to the opener; the selection lives
in the URL and survives a reload and a breakpoint change; one detail panel at a time; a refused read and a failed read are different states; a slow older answer
never fills the panel of a newer pick."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, env, path, w=1440, h=900):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._posts = []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" and "/auth/" not in r.url else None)
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/{path}")
    page.set_viewport_size({"width": w, "height": h})
    return page, ctx


def test_sources_inspector_moves_between_pane_drawer_and_sheet_and_keeps_the_selection(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "sources")
        card = tid(page, "artifact-row").first
        expect(card).to_be_visible(timeout=WAIT)
        aid = card.get_attribute("data-artifact-id")
        card.get_by_test_id("artifact-details").click()
        pane = tid(page, "sources-selected")
        expect(pane).to_have_attribute("data-placement", "pane")
        expect(pane.get_by_role("heading", level=2)).to_be_focused()
        assert f"file={aid}" in page.url
        page.set_viewport_size({"width": 1024, "height": 768})                   # a breakpoint change keeps the selection, now as a drawer
        expect(tid(page, "sources-selected")).to_have_attribute("data-placement", "drawer")
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", aid)
        page.reload()
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", aid, timeout=WAIT)   # deep link / reload
        page.keyboard.press("Escape")
        expect(tid(page, "sources-selected")).to_have_count(0)
        assert "file=" not in page.url
        details = tid(page, "artifact-row").first.get_by_test_id("artifact-details")
        details.click()
        expect(tid(page, "sources-selected")).to_be_visible()
        page.keyboard.press("Escape")
        expect(details).to_be_focused()                                          # focus returns to the opener
        details.click()                                                          # one detail panel at a time below 1280 px
        tid(page, "artifact-row").first.get_by_role("button", name="Evidence").click()
        expect(tid(page, "sources-selected")).to_have_count(0)
        expect(tid(page, "evidence-box")).to_be_visible()
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})                    # phone: a modal sheet
        tid(page, "artifact-row").first.get_by_test_id("artifact-details").click()
        sheet = tid(page, "sources-selected")
        expect(sheet).to_have_attribute("data-placement", "drawer")
        assert page.evaluate("document.querySelector('dialog[open]')?.getAttribute('aria-modal')") == "true"
        page.keyboard.press("Escape")
        expect(sheet).to_have_count(0)
        assert page.evaluate("document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1")
        assert not [u for u in page._posts if "/v2/" in u], page._posts           # selecting never starts anything
        ctx.close()
    finally:
        server.stop()


def test_a_slow_older_evidence_answer_never_fills_a_newer_pick(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "sources")
        cards = tid(page, "artifact-row")
        expect(cards.first).to_be_visible(timeout=WAIT)
        cards.first.get_by_test_id("artifact-details").click()
        pane = tid(page, "sources-selected")
        items = pane.get_by_test_id("sel-observation-item")
        expect(items.first).to_be_visible(timeout=WAIT)
        if items.count() < 2:
            pytest.skip("the first file has fewer than two observations in this fixture")
        value = pane.get_by_test_id("sel-evidence-value")
        items.nth(0).click()
        expect(value).to_be_visible()
        v0 = value.inner_text()
        items.nth(1).click()
        page.wait_for_timeout(500)
        v1 = value.inner_text()
        if v0 == v1:
            pytest.skip("the first two observations carry the same value in this fixture")
        held = []

        def slow(route):
            if not held:
                held.append(route.request.url)
                time.sleep(1.5)                                                  # the older pick's answer arrives after the newer one
            route.continue_()
        page.route("**/v2/sources/revisions/*/evidence/*", slow)
        items.nth(0).click()
        items.nth(1).click()
        page.wait_for_timeout(2500)
        page.unroute("**/v2/sources/revisions/*/evidence/*")
        expect(value).to_have_text(v1)                                           # the newest pick wins; the stale answer is ignored
        ctx.close()
    finally:
        server.stop()


def test_history_version_inspector_reads_files_and_keeps_refused_and_failed_apart(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "history")
        row = tid(page, "revision-row").first
        expect(row).to_be_visible(timeout=WAIT)
        rid = row.get_attribute("data-revision-id")
        from tests.ui_next.test_scn_polish_browser import _flush_ok
        _flush_ok(page, '[data-testid="history-versions-table"] table')          # SCN-15: the versions table runs edge to edge, header band, aligned first column
        row.get_by_test_id("history-version-open").click()
        detail = tid(page, "history-version-detail")
        expect(detail).to_have_attribute("data-placement", "pane")
        expect(detail).to_have_attribute("data-state", "ready", timeout=WAIT)
        assert detail.locator('[data-testid="history-version-files"] tbody tr').count() >= 1
        assert f"version={rid}" in page.url
        page.reload()
        expect(tid(page, "history-version-detail")).to_have_attribute("data-revision-id", rid, timeout=WAIT)
        page.route("**/v2/sources/revisions/*", lambda route: route.fulfill(status=403, content_type="application/json", body='{"detail":"denied"}'))
        page.reload()
        expect(tid(page, "history-version-denied")).to_be_visible(timeout=WAIT)
        expect(tid(page, "history-version-files")).to_have_count(0)               # nothing is shown from a refused read
        page.unroute("**/v2/sources/revisions/*")
        page.route("**/v2/sources/revisions/*", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}'))
        page.reload()
        expect(tid(page, "history-version-error")).to_be_visible(timeout=WAIT)   # failed is not "no files"
        page.unroute("**/v2/sources/revisions/*")
        tid(page, "history-version-retry").click()
        expect(tid(page, "history-version-detail")).to_have_attribute("data-state", "ready", timeout=WAIT)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/history?version=rev-not-in-history")
        expect(tid(page, "history-version-missing")).to_be_visible(timeout=WAIT)
        tid(page, "history-version-close").click()
        expect(tid(page, "history-version-detail")).to_have_count(0)
        assert not [u for u in page._posts if "/v2/" in u], page._posts
        ctx.close()
    finally:
        server.stop()
