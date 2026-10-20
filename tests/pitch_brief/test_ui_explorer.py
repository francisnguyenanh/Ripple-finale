"""J1 browser: Explorer graph + list from the server projection, filters, node detail, confirm reflects after reload.
Real server, real Chromium, stub provider (no model call)."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import UI_BASE, goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build, cand_id  # noqa: E402

WAIT = 30000
NEXT = UI_BASE.startswith("/app-next")


def _view(page, mode):
    """The new interface has a Business/Technical switch; the checks below assert the plain business meaning in Business and the raw keys/ids in Technical
    (the legacy page has no switch and always shows the raw text)."""
    if NEXT:
        tid(page, f"mode-{mode}").click()


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_explorer_graph_list_filter_detail_and_confirm_reload(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        goto_view(page, "Explorer")
        # the legacy page opens on the graph; the new page opens on the tree (addendum: tree is the default, the graph is optional)
        expect(tid(page, "explorer-tree" if NEXT else "graph-svg")).to_be_visible()
        assert page.locator("[data-node][data-type=artifact]").count() == 6
        tidc = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        node = page.locator(f"[data-node='{tidc}']")
        expect(node).to_have_attribute("data-state", "pending-confirmation")
        node.click()
        if NEXT:      # Business: the rule in words (what it is, its value, where it comes from, that no person has confirmed it); Technical: the raw key
            _view(page, "business")
            for text in ("Standard manager approval threshold", "1,000,000 JPY", "approval.yaml", "not yet confirmed"):
                expect(tid(page, "node-detail")).to_contain_text(text)
            assert "standard_manager_approval_threshold" not in tid(page, "node-detail").inner_text()
        _view(page, "technical")
        expect(tid(page, "node-detail")).to_contain_text("standard_manager_approval_threshold")
        _view(page, "business")
        expect(tid(page, "explorer-coverage")).to_contain_text("calculations.py")   # not analysed, disclosed
        # filter + equivalent list from the same data
        tid(page, "explorer-search").fill("freight")
        assert page.locator("[data-node][data-type=candidate]").count() == 1
        tid(page, "explorer-search").fill("")
        tid(page, "explorer-mode").click()
        if NEXT:      # the optional graph view shows the same data; the tree (with its inline text) is the readable alternative
            expect(tid(page, "graph-svg")).to_be_visible()
            assert page.locator(f"[data-node='{tidc}']").count() == 1
            expect(tid(page, "graph-key")).to_contain_text("not a dependency")                 # UX-11: the line key next to the graph
            tid(page, "explorer-mode").click()
            expect(tid(page, "explorer-tree")).to_be_visible()
        else:
            expect(tid(page, "graph-list")).to_be_visible()
            assert page.locator(f"li[data-node='{tidc}']").count() == 1
        # confirm through the API (UI confirm exists on Questions), then a fresh reload shows it
        r = env.admin_client().post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": tidc})
        assert r.status_code == 200, r.text
        page.reload()
        goto_view(page, "Explorer")
        expect(page.locator(f"[data-node='{tidc}']")).to_have_attribute("data-state", "confirmed")
        assert not errors, errors
    finally:
        server.stop()


def test_explorer_confirm_with_rationale_shows_timeline(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        goto_view(page, "Explorer")
        tidc = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        page.locator(f"[data-node='{tidc}']").click()
        if NEXT:      # Business: the history says the AI read it and that this was not a human decision; Technical: the raw event kind
            _view(page, "business")
            expect(tid(page, "node-timeline")).to_contain_text("AI reading")
            expect(tid(page, "node-timeline")).to_contain_text("not a human decision")
        _view(page, "technical")
        expect(tid(page, "node-timeline")).to_contain_text("extracted")
        _view(page, "business")
        tid(page, "confirm-rationale").fill("agrees with policy section 3")
        tid(page, "confirm-rule").click()
        expect(page.locator(f"[data-node='{tidc}']")).to_have_attribute("data-state", "confirmed")
        if not NEXT:      # the legacy graph needs a second click to select again; the tree keeps the rule open and refreshes it
            page.locator(f"[data-node='{tidc}']").click()
        if NEXT:
            expect(tid(page, "node-timeline")).to_contain_text("person decision")
        _view(page, "technical")
        expect(tid(page, "node-timeline")).to_contain_text("decision:confirm")
        _view(page, "business")
        expect(tid(page, "node-timeline")).to_contain_text("agrees with policy section 3")
        expect(tid(page, "node-timeline")).to_contain_text("source file is not changed")
        assert page.locator("[data-testid=confirm-panel]").count() == 0     # already confirmed: no second confirm
    finally:
        server.stop()


def test_explorer_rule_to_prefilled_request_to_impact_report(env, ch, pw_browser):
    from tests.pitch_brief.conftest import drain
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1400, "height": 1200})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        goto_view(page, "Explorer")
        tidc = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        page.locator(f"[data-node='{tidc}']").click()
        tid(page, "request-change").click()
        expect(tid(page, "prefill-note")).to_be_visible()
        expect(tid(page, "sf-subject")).to_have_value("Manager approval")
        expect(tid(page, "sf-old")).to_have_value("1000000")
        expect(tid(page, "sf-new")).to_have_value("")             # the person types the new value
        tid(page, "sf-new").fill("500000")
        tid(page, "sf-unit").fill("net goods amount")              # not in the rule: the person supplies it
        tid(page, "sf-applies").fill("standard, emergency")
        tid(page, "action-parse-structured").click()
        expect(tid(page, "state-intent")).to_have_attribute("data-state", "DRAFT")
        tid(page, "action-confirm-intent").click()
        expect(tid(page, "state-intent")).to_have_attribute("data-state", "CONFIRMED")
        if tid(page, "action-run-relations").is_enabled():
            tid(page, "action-run-relations").click()
            expect(tid(page, "status-live")).to_contain_text("relation coverage")
        else:
            expect(tid(page, "action-run-relations")).to_have_attribute("title", re.compile("Already computed"))
        drain(env)
        tid(page, "action-run-impact").click()
        expect(tid(page, "state-impact")).not_to_have_attribute("data-state", "NONE")
        drain(env)
        expect(tid(page, "state-impact")).to_have_attribute("data-state", "COMPLETED")
        report = tid(page, "impact-report")
        if NEXT:      # Business: the impact classes in words and the rule's file by name; Technical: the raw class names and the full logical path
            _view(page, "business")
            expect(report).to_contain_text("Directly affected (")
            expect(report).to_contain_text("Unknown (")
            expect(report).to_contain_text("Excluded (")
            expect(report).to_contain_text("not a causal dependency")
            expect(report).to_contain_text("approval.yaml")                    # labels resolved from the same snapshot
            _view(page, "technical")
        expect(report).to_contain_text("Direct (")
        expect(report).to_contain_text("Unknown (")
        expect(report).to_contain_text("Excluded (")
        expect(report).to_contain_text("not a causal dependency")
        expect(report).to_contain_text("harbor_demo/config/approval.yaml")     # labels resolved from the same snapshot
        assert not errors, errors
    finally:
        server.stop()
