"""Accessibility (WCAG 2.2 AA target) of the new interface with axe-core on real local data (Harbor stub fixtures).

axe-core is a pinned dev dependency of frontend/ (npm ci). It is evaluated through the debugging protocol, so no script is
loaded by the page and the strict CSP is not weakened.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

AXE = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "node_modules" / "axe-core" / "axe.min.js"
pytestmark = pytest.mark.skipif(not AXE.is_file(), reason="run `npm ci` in frontend/ first")
WAIT = 30000
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _violations(page):
    page.evaluate(AXE.read_text(encoding="utf-8"))
    res = page.evaluate("async (tags) => (await axe.run(document, {runOnly: {type: 'tag', values: tags}})).violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary || '').slice(0, 160))}))", TAGS)
    return res


@pytest.mark.parametrize("scheme,w,h", [("light", 1280, 800), ("dark", 1280, 800), ("light", 390, 844)])
def test_pages_have_no_axe_violations(env, ch, pw_browser, scheme, w, h):
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": w, "height": h}, color_scheme=scheme)
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        found = {}
        for view in ("Overview", "Sources", "Explorer", "Questions", "Changes", "Impact & Simulation", "History", "Support"):
            if w < 1024:
                tid(page, "menu-button").click()
            goto_view(page, view)
            expect(tid(page, "page-title")).to_be_visible()
            page.wait_for_load_state("networkidle")
            if view == "Explorer":
                tid(page, "rule-toggle").first.click()                   # the tree with one rule opened (its panel is part of the scan)
                page.wait_for_load_state("networkidle")
                bad_tree = [v for v in _violations(page) if v["impact"] in ("serious", "critical", "moderate", "minor")]
                if bad_tree:
                    found["Explorer (tree)"] = bad_tree
                tid(page, "explorer-mode").click()                       # then the optional graph view
                expect(tid(page, "graph-svg")).to_be_visible()
            bad = [v for v in _violations(page) if v["impact"] in ("serious", "critical", "moderate", "minor")]
            if bad:
                found[view] = bad
        assert not found, ("A11Y: " + " | ".join(f"{view}: {v['id']} ({v['impact']}) {v['nodes'][:2]}" for view, vs in found.items() for v in vs))
        ctx.close()
    finally:
        server.stop()
