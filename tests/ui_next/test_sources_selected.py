"""D6 (owner, 2026-10-21): the Sources cards stay; "Details" opens a selected-file pane beside them. Reading, AI extraction and confirmation stay separate, the
cost note sits next to the analysis buttons, what the parser read is shown inline for evidence readers. Selecting starts nothing, the URL keeps the selection, a
person without evidence rights sees no contents, and the existing evidence drawer still works. Stub providers only."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000


def _shot(page, name, el=None):                                                        # optional review capture (UI_NEXT_SHOTS, outside Git); never affects the assertions
    import os
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        if el is not None:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
        page.screenshot(path=os.path.join(target, f"{name}.png"), full_page=False)


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, env, account, cred):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._writes = []
    page.on("request", lambda r: page._writes.append((r.method, r.url)) if r.method not in ("GET", "HEAD", "OPTIONS") and "/auth/" not in r.url else None)
    open_app(page, server.url)
    login(page, account, cred)
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources")
    expect(tid(page, "artifact-row").first).to_be_visible(timeout=WAIT)
    return page, ctx


def test_details_open_a_selected_file_pane_that_starts_nothing_and_survives_a_reload(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, env.admin, f"cred-a-{env.nonce}")
        assert tid(page, "sources-selected").count() == 0                                     # no selection: the page is the cards as before
        n_cards = tid(page, "artifact-row").count()
        card = tid(page, "artifact-row").first
        aid = card.get_attribute("data-artifact-id")
        card.get_by_test_id("artifact-details").click()
        pane = tid(page, "sources-selected")
        expect(pane).to_have_attribute("data-artifact-id", aid)
        expect(card).to_have_attribute("data-selected", "true")
        expect(pane.get_by_test_id("file-stages")).to_be_visible()
        for st in ("stage-parsed", "stage-analysis", "stage-rules"):                          # reading, AI extraction and confirmation stay separate
            expect(pane.get_by_test_id(st)).to_be_visible()
        expect(pane.get_by_test_id("sources-selected-actions")).to_contain_text("live or simulated")   # the cost note next to the analysis buttons, once
        assert pane.get_by_text("whether a run is live or simulated").count() == 1
        expect(pane.get_by_test_id("sel-evidence-box")).to_be_visible()
        _shot(page, "d6_selected_file_pane_1440", pane)
        assert tid(page, "artifact-row").count() == n_cards                                   # every card is still listed
        box = pane.bounding_box(); first = card.bounding_box()
        assert box["x"] > first["x"] + first["width"] - 1                                     # desktop: the pane sits beside the cards
        items = pane.get_by_test_id("sel-observation-item")
        if items.count():
            items.first.click()
            expect(pane.get_by_test_id("sel-evidence-value")).to_be_visible()
        from tests.ui_next.test_a11y import scoped_violations                                   # targeted a11y of the new pane
        assert scoped_violations(page, '[data-testid="sources-selected"]') == []
        assert page._writes == [], page._writes                                               # selecting and reading start nothing
        assert f"file={aid}" in page.url
        page.reload()
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", aid, timeout=WAIT)
        tid(page, "sources-selected-close").click()
        expect(tid(page, "sources-selected")).to_have_count(0)
        assert "file=" not in page.url
        tid(page, "artifact-row").first.get_by_role("button", name="Evidence").click()        # the existing drawer is unchanged
        expect(tid(page, "evidence-box")).to_be_visible()
        ctx.close()
    finally:
        server.stop()


def test_a_person_without_evidence_rights_sees_the_stages_but_no_contents_and_an_unknown_file_says_so(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, env.req, f"cred-r-{env.nonce}")
        tid(page, "artifact-row").first.get_by_test_id("artifact-details").click()
        pane = tid(page, "sources-selected")
        expect(pane.get_by_test_id("file-stages")).to_be_visible()
        expect(pane.get_by_test_id("sources-selected-no-evidence")).to_be_visible()
        assert pane.get_by_test_id("sel-evidence-box").count() == 0
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources?file=art-does-not-exist")
        expect(tid(page, "sources-selected-missing")).to_be_visible(timeout=WAIT)
        assert page._writes == [], page._writes
        ctx.close()
    finally:
        server.stop()
