"""U2 in the browser (stub providers only: SIMULATED): the Explorer opens as a tree; every source file is a row (also a file that was never
analysed); a rule opens inline with its evidence, history and next action; open/closed state survives search, reload and a decision;
a deep link opens its ancestors and takes focus; counts are unique; a role without `evidence.read` sees no quote. Screenshots: UI_NEXT_SHOTS."""
from __future__ import annotations

import os
import pathlib

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build, cand_id  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000
SHOTS = os.environ.get("UI_NEXT_SHOTS")


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, who, w=1280, h=900):
    ctx = browser.new_context(base_url=server.url, viewport={"width": w, "height": h})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    open_app(page, server.url)
    login(page, *who)
    select_workspace(page, env_ws(page))
    return page, ctx


_WS = {}


def env_ws(page):
    return _WS["ws"]


def _go(browser, server, env, who, w=1280, h=900):
    _WS["ws"] = env.ws
    page, ctx = _open(browser, server, who, w, h)
    if w < 1024:
        tid(page, "menu-button").click()
    goto_view(page, "Explorer")
    expect(tid(page, "explorer-tree")).to_be_visible(timeout=WAIT)
    return page, ctx


def _shot(page, name):
    if SHOTS:
        pathlib.Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(pathlib.Path(SHOTS) / name), full_page=True)


def _rule(page, cid):
    return page.locator(f'[data-node="{cid}"][data-type="candidate"]')


def test_every_file_is_a_row_and_a_rule_opens_inline_with_its_evidence(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _go(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        files = page.locator('[data-node][data-type="artifact"]')
        assert files.count() == len(ch.arts), "a file with no rules or no analysis is still a row"
        never = page.locator('[data-type="artifact"]', has_text="calculations.py")
        expect(never).to_contain_text("Not analysed")
        expect(never).to_contain_text("not the same as having none")
        cid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        row = _rule(page, cid)
        expect(row).to_have_attribute("data-state", "pending-confirmation")
        toggle = row.get_by_test_id("rule-toggle")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        panel = page.locator(f'[data-testid="node-detail"][data-detail-for="{cid}"]')
        for h in ("What this means", "Where it came from", "Why it needs confirmation", "Related items", "Before, after and history", "Next action"):
            expect(panel.get_by_role("heading", name=h)).to_be_visible()
        ev = panel.get_by_test_id("evidence-item").first
        expect(ev).to_contain_text("approval.yaml")
        assert "Quoted text" not in page.inner_text("main"), "evidence is named by file, location and excerpt"
        assert (ev.inner_text().split(" - ")[1].strip()), "the location is shown"
        expect(panel.get_by_test_id("node-timeline")).to_contain_text("AI reading", timeout=WAIT)
        expect(panel.get_by_test_id("confirm-panel")).to_be_visible()
        assert_business_clean(page, "Explorer tree, rule open")
        _shot(page, "u2_tree_rule_open_1280.png")
        # an action next to the header never toggles the row
        page.locator('[data-type="artifact"]', has_text="approval.yaml").get_by_test_id("file-open-sources").click()
        expect(tid(page, "lifecycle-summary")).to_be_visible(timeout=WAIT)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_open_state_survives_search_reload_and_a_decision_and_the_keyboard_works(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _go(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        cid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        toggle = _rule(page, cid).get_by_test_id("rule-toggle")
        toggle.focus()
        page.keyboard.press("Enter")                                   # keyboard only
        expect(toggle).to_have_attribute("aria-expanded", "true")
        page.keyboard.press("Space")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        before = page.locator('[data-testid="rule-toggle"][aria-expanded="true"]').count()
        tid(page, "explorer-search").fill("freight")                   # a search opens the way down to each match
        matched = page.locator('[data-node][data-type="candidate"]')
        assert matched.count() >= 1
        assert page.locator(f'[data-node="{cid}"]').count() == 0, "rows that do not match are hidden while searching"
        tid(page, "explorer-search").fill("")                          # clearing restores what was open
        expect(_rule(page, cid).get_by_test_id("rule-toggle")).to_have_attribute("aria-expanded", "true")
        assert page.locator('[data-testid="rule-toggle"][aria-expanded="true"]').count() == before
        # a decision refreshes the data and keeps the row open
        tid(page, "confirm-rationale").fill("agrees with the policy")
        tid(page, "confirm-rule").click()
        expect(_rule(page, cid)).to_have_attribute("data-state", "confirmed", timeout=WAIT)
        expect(_rule(page, cid).get_by_test_id("rule-toggle")).to_have_attribute("aria-expanded", "true")
        expect(page.locator(f'[data-detail-for="{cid}"]').get_by_test_id("node-timeline")).to_contain_text("person decision", timeout=WAIT)
        expect(page.locator(f'[data-detail-for="{cid}"]')).to_contain_text("The source file was not changed")
        # close all / open all files are bounded
        tid(page, "collapse-all").click()
        assert page.locator('[aria-expanded="true"][data-testid$="-toggle"]').count() == 0
        tid(page, "expand-files").click()
        assert page.locator('[data-testid="file-toggle"][aria-expanded="true"]').count() == len(ch.arts)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_a_deep_link_opens_its_ancestors_and_takes_focus(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        _WS["ws"] = env.ws
        page, ctx = _open(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        cid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        # collapsed workspace on purpose: a link to a rule must still open the file and the rule
        graph = env.admin_client().get("/v2/knowledge/graph", params={"workspace_id": env.ws}).json()
        ev = next(e["from"] for e in graph["edges"] if e["type"] == "evidence-of" and e["to"] == cid)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/explorer?node={ev}")
        expect(tid(page, "explorer-tree")).to_be_visible(timeout=WAIT)
        expect(_rule(page, cid).get_by_test_id("rule-toggle")).to_have_attribute("aria-expanded", "true", timeout=WAIT)
        page.wait_for_function("(id) => document.activeElement && document.activeElement.id === 'hdr-' + id", arg=cid, timeout=WAIT)
        ctx.close()
    finally:
        server.stop()


def test_counts_are_unique_and_unknown_is_not_zero(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _go(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        graph = env.admin_client().get("/v2/knowledge/graph", params={"workspace_id": env.ws}).json()
        rules = [n for n in graph["nodes"] if n["type"] == "candidate"]
        issues = [n for n in graph["nodes"] if n["type"] == "finding"]
        tid(page, "explorer-counts-more").locator("summary").click()      # the explanation is behind a disclosure (D4)
        text = tid(page, "explorer-counts").inner_text()
        assert f"{len(rules)} rules" in text and f"{len(issues)} issue" in text and "counted once" in text, text
        # an issue that touches several rules is listed under each and is still one record
        tid(page, "expand-rules").click()
        for f in issues:
            rows = page.locator(f'[data-node="{f["id"]}"][data-type="finding"]').count()
            assert rows >= 1
        cov = tid(page, "explorer-coverage").inner_text()
        assert "not analysed" in cov and "unknown, not absent" in cov, cov
        ctx.close()
    finally:
        server.stop()


def test_a_role_without_evidence_rights_sees_no_quote_and_the_tree_fits_a_phone(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _go(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"), 390, 844)
        cid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        _rule(page, cid).get_by_test_id("rule-toggle").click()
        panel = page.locator(f'[data-detail-for="{cid}"]')
        expect(panel).to_contain_text("hidden for your role", timeout=WAIT)
        quote = next(c["evidence"][0]["quote"] for c in ch.cands[APPROVAL] if c["candidate_id"] == cid)
        assert quote[:20] not in page.inner_text("main"), "a quote leaked to a role without evidence.read"
        expect(panel.get_by_test_id("confirm-panel")).to_have_count(0)
        expect(panel).to_contain_text("Knowledge owner role")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), "sideways scroll at 390"
        _shot(page, "u2_tree_phone_390.png")
        ctx.close()
    finally:
        server.stop()
