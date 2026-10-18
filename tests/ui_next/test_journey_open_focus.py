"""UX-08: a journey link opens a folded stage BEFORE moving focus to it, keeps the route hash, makes no request and no POST,
and a step whose stage does not exist shows an explicit reason instead of scrolling somewhere unrelated. Stub providers only."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build, intent as make_intent, S1  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_journey_link_opens_the_folded_stage_then_focuses_it(env, ch, pw_browser):
    iid = make_intent(env, ch.admin, S1, confirm=True)["intent_id"]
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        page._calls = []
        page.on("request", lambda r: page._calls.append((r.method, r.url)))
        open_app(page, server.url)
        login(page, env.req, f"cred-r-{env.nonce}")
        select_workspace(page, env.ws)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/changes?intent={iid}")
        expect(tid(page, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=WAIT)
        stage = tid(page, "flow-draft")
        expect(stage).to_have_attribute("data-open", "true")
        tid(page, "flow-draft-toggle").click()                       # the person folds the stage
        expect(stage).to_have_attribute("data-open", "false")
        hash_before = page.evaluate("location.hash")
        before = len(page._calls)
        page.locator('[data-step="draft"] a').click()
        expect(stage).to_have_attribute("data-open", "true")
        expect(tid(page, "flow-draft-toggle")).to_be_focused()
        assert page.evaluate("location.hash") == hash_before, "the route must not change"
        new = page._calls[before:]
        assert not [c for c in new if c[0] != "GET"], new
        ctx.close()
    finally:
        server.stop()


def test_journey_link_with_no_stage_gives_a_reason(env, ch, pw_browser):
    iid = make_intent(env, ch.admin, S1, confirm=True)["intent_id"]
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        open_app(page, server.url)
        login(page, env.req, f"cred-r-{env.nonce}")
        select_workspace(page, env.ws)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/changes?intent={iid}")
        expect(tid(page, "journey")).to_be_visible(timeout=WAIT)
        # a step whose stage is not on the page (e.g. the final query stage before an apply exists)
        missing = [li for li in page.locator("[data-step]").all() if page.locator(f'[data-testid="{li.locator("a").get_attribute("href")[1:]}"]').count() == 0]
        assert missing, "expected at least one step without a stage on this page"
        scroll_before = page.evaluate("window.scrollY")
        missing[0].locator("a").click()
        expect(tid(page, "journey-missing")).to_be_visible()
        assert page.evaluate("window.scrollY") == scroll_before, "must not scroll to an unrelated place"
        ctx.close()
    finally:
        server.stop()
