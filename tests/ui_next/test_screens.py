"""Screenshots of every page at 1440/1280/1024/390 with real local data (Harbor stub fixtures: SIMULATED), plus the check that
nothing scrolls sideways. Images go to UI_NEXT_SHOTS (outside the repository); they are for a person to look at, not an oracle."""
from __future__ import annotations

import os
import pathlib

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

SHOTS = os.environ.get("UI_NEXT_SHOTS")
WAIT = 30000
PAGES = [("Overview", "overview"), ("Sources", "sources"), ("Explorer", "explorer"), ("Questions", "questions"), ("Changes", "changes"),
         ("Impact & Simulation", "impact"), ("History", "history"), ("Support", "support")]
# what shows that a page has its real data (not its loading state)
READY = {"overview": "overview-cta", "sources": "artifact-row", "explorer": "explorer-counts", "questions": "questions-note", "changes": "chat-turns",
         "impact": "flow-whatif", "history": "history-activity", "support": "support-matrix"}


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


@pytest.mark.parametrize("w,h", [(1440, 900), (1280, 800), (1024, 768), (390, 844)])
def test_every_page_renders_without_sideways_scroll_and_is_photographed(env, ch, pw_browser, w, h):
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": w, "height": h})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        out = pathlib.Path(SHOTS) if SHOTS else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
        # a started conversation makes the Changes page show real content
        if w < 1024:
            tid(page, "menu-button").click()
        goto_view(page, "Changes")
        tid(page, "chat-input").fill("Please lower the limit that managers can approve from 1,000,000 to 500,000 JPY for standard orders")
        tid(page, "chat-start").click()
        expect(tid(page, "chat-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        chat_url = page.url
        for label, slug in PAGES:
            if w < 1024:
                tid(page, "menu-button").click()
            if slug == "changes":
                page.goto("about:blank")
                page.goto(chat_url)                       # the conversation is part of the link
            else:
                goto_view(page, label)
            expect(tid(page, "page-title")).to_be_visible()
            expect(tid(page, READY[slug]).first).to_be_visible(timeout=WAIT)
            page.wait_for_timeout(500)
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), f"sideways scroll on {label} at {w}"
            if out:
                page.screenshot(path=str(out / f"{slug}_{w}.png"), full_page=(w == 1280))
        ctx.close()
    finally:
        server.stop()
