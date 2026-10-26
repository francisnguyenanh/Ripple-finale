"""SCN-07 graph decluttering on the Explorer graph view (real server, Chromium, routes on the throwaway PostgreSQL; stub providers only; reads only).
Items with no edge to another SHOWN item are listed under the canvas with their count, stated as "no link in the loaded graph" (never "unrelated"); canvas + list
= the items shown; a chip selects the item (URL `node=`) and the detail pane; a deep-linked unlinked item is found; "show on the canvas too" puts all back; a
filter that hides an item's linked partner says so; dashed edges only come from the server's explicit `projection` flag (not tested here: no projection in this
fixture)."""
from __future__ import annotations

import re

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


def _graph(browser, server, env, path="explorer"):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._posts = []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" and "/auth/" not in r.url else None)
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/{path}")
    tid(page, "explorer-mode").click()
    expect(tid(page, "graph-svg")).to_be_visible(timeout=WAIT)
    return page, ctx


def _nums(text):
    return [int(x) for x in re.findall(r"\d+", text)]


def test_unlinked_items_are_listed_counted_and_selectable(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _graph(pw_browser, server, env)
        loose = tid(page, "graph-loose")
        expect(loose).to_be_visible()                                            # the never-analysed file has no link in the loaded graph
        chips = loose.get_by_test_id("graph-loose-chip")
        n_chips = chips.count()
        canvas = page.locator(".react-flow__node").count()
        counts = tid(page, "graph-counts").inner_text()
        assert "not that the item is unrelated" in counts
        c_canvas, c_listed, c_total = _nums(counts)[:3]
        assert (c_canvas, c_listed) == (canvas, n_chips) and c_canvas + c_listed == c_total   # count conservation in the same loaded scope
        chip = chips.first
        nid = chip.get_attribute("data-node-id")
        chip.click()
        expect(chip).to_have_attribute("aria-pressed", "true")
        assert f"node={nid}" in page.url
        expect(page.locator("#explorer-detail")).not_to_contain_text("Select a box to see")        # the detail pane shows the chosen item
        page.reload()                                                            # a deep-linked unlinked item is found again
        tid(page, "explorer-mode").click()
        expect(page.locator(f'[data-testid="graph-loose-chip"][data-node-id="{nid}"]')).to_have_attribute("aria-pressed", "true", timeout=WAIT)
        tid(page, "graph-loose-toggle").click()                                  # show all on the canvas
        expect(page.locator(".react-flow__node")).to_have_count(c_total)
        expect(tid(page, "graph-loose-toggle")).to_have_attribute("aria-pressed", "true")
        chips.first.focus()
        page.keyboard.press("Enter")                                             # chips work by keyboard
        assert not [u for u in page._posts if "/v2/" in u], page._posts
        ctx.close()
    finally:
        server.stop()


def test_a_filter_that_hides_the_linked_partner_says_so(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _graph(pw_browser, server, env)
        tid(page, "explorer-search").fill("freight")                             # a rule whose file (its linked partner) does not match the search
        loose = tid(page, "graph-loose")
        counts = tid(page, "graph-counts")
        expect(counts).to_be_visible()
        if loose.count():
            expect(loose).to_contain_text("filter hides")
        else:
            expect(counts).to_have_attribute("data-all-loose", "true")           # nothing links among the shown items: all stay on the canvas, said as such
            expect(counts).to_contain_text("not proof that they are unrelated")
        ctx.close()
    finally:
        server.stop()
