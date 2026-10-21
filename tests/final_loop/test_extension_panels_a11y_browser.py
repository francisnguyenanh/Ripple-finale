"""A21-A25 extension panels in the DARK theme at desktop width, operated by keyboard (2026-10-21; A26 desktop part, plus the A22 / A23 behaviours no test covered):
each control is reached with Tab from its panel and activated with Enter, shows a visible focus outline, and the panel passes scoped axe (WCAG 2.2 AA + best practice,
colour contrast included) in the dark theme. A22: progress "n of N rule groups compared", a group that was not compared offers "Continue the scan", and a scan with no
hypothesis says it is not proof. A23: "N of M turns left". A24: the comparison table scrolls inside its own area (no page-level sideways scroll).
Real server, worker, Chromium and routes on the throwaway PostgreSQL; stub models (the same dispatch as test_lha_ui_browser.py). Mobile is out of scope (owner)."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_aix_k1_mining import mobj as mining_obj  # noqa: E402
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_lha_ui_browser import aiui, dispatch  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT  # noqa: E402
from tests.ui_next.test_a11y import scoped_violations  # noqa: E402

FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'


def dark_page(browser, server, env, page_hash):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 1000}, color_scheme="dark")
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page._errors = errors
    assert page.goto(server.url + "/app-next/").status == 200
    page.get_by_label("Account ID").fill(env.admin)
    page.get_by_label("Credential").fill(f"cred-a-{env.nonce}")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("session-account")).to_have_text(env.admin, timeout=WAIT)
    page.get_by_label("Workspace").select_option(value=env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/{page_hash}")
    return page


def tab_to(page, container, target, limit=80):
    """Focus the panel's first focusable element, then press Tab until `target` has focus (proves it is in the tab order); assert a visible focus outline."""
    container.locator(FOCUSABLE).first.focus()
    for _ in range(limit):
        if target.evaluate("e => e === document.activeElement"):
            break
        page.keyboard.press("Tab")
    assert target.evaluate("e => e === document.activeElement"), "not reachable with Tab from its panel"
    page.keyboard.press("Shift+Tab")                                                          # arrive by keyboard so :focus-visible applies
    page.keyboard.press("Tab")
    assert target.evaluate("e => e === document.activeElement"), "focus did not return to the control with Tab"
    assert target.evaluate("e => getComputedStyle(e).outlineStyle !== 'none' || getComputedStyle(e).boxShadow !== 'none'"), "no visible focus indicator"


def no_page_sideways_scroll(page):
    ok = page.evaluate("() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1")
    if not ok:                                                                                # name the widest elements to make a failure actionable
        wide = page.evaluate("() => { const w = document.documentElement.clientWidth; return [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > w + 1)"
                             ".slice(-5).map(e => (e.getAttribute('data-testid') || e.tagName) + ':' + Math.round(e.getBoundingClientRect().right) + ':' + (e.className || '').toString().slice(0, 80)); }")
        print("SIDEWAYS", wide)
    return ok


def test_conflict_scan_and_interview_by_keyboard_in_the_dark_theme_with_progress_continue_not_proof_and_turns_left(aiui, pw_browser):
    env, model, server, _cid = aiui
    state = {"group": None}

    def first_group_fails(prompt):                                                            # the first rule group always returns an invalid object (every retry too): "not compared"
        if "<fields>" in prompt:
            ids = frozenset(re.findall(r"id=(rule-[a-z0-9-]+)", prompt))
            state["group"] = state["group"] or ids
            if ids == state["group"]:
                return {"unexpected": True}
        return dispatch(prompt)
    model.fn = first_group_fails
    page = dark_page(pw_browser, server, env, "questions?ui_section=all")
    scan = page.get_by_test_id("mining")
    expect(scan).to_be_visible(timeout=WAIT)
    tab_to(page, scan, scan.get_by_test_id("mining-scan"))
    page.keyboard.press("Enter")
    progress = scan.get_by_test_id("mining-progress")
    expect(progress).to_have_text(re.compile(r"\d+ of \d+ rule groups compared"), timeout=120000)
    page.reload()                                                                             # groups waiting for a slot are not continued by a reloaded page by themselves
    scan = page.get_by_test_id("mining")
    cont = scan.get_by_test_id("mining-continue")
    expect(cont).to_be_visible(timeout=120000)
    expect(cont).to_contain_text("not compared")
    tab_to(page, scan, cont)
    page.keyboard.press("Enter")
    expect(scan.get_by_test_id("mining-continue")).to_have_count(0, timeout=180000)
    progress = scan.get_by_test_id("mining-progress")
    expect(progress).to_contain_text("1 not compared", timeout=180000)                         # the always-failing group is reported, never counted as compared
    expect(scan.get_by_test_id("mining-group-failed").first).to_be_visible()
    model.fn = dispatch
    assert scoped_violations(page, '[data-testid="mining"]') == []
    model.fn = lambda p: mining_obj(status="NONE_FOUND") if "<fields>" in p else dispatch(p)     # a new attempt that keeps no hypothesis
    rescan = scan.get_by_test_id("mining-rescan")
    tab_to(page, scan, rescan)
    page.keyboard.press("Enter")
    expect(scan.get_by_test_id("mining-none")).to_contain_text("not proof", timeout=120000)
    model.fn = dispatch
    card = page.locator('[data-testid="business-gap"][data-kind="missing_actor"]').first
    expect(card).to_be_visible(timeout=WAIT)
    tab_to(page, card, card.get_by_test_id("interview-start"))
    page.keyboard.press("Enter")
    expect(card.get_by_test_id("interview-turns-left")).to_have_text(re.compile(r"\d+ of \d+ turns left"), timeout=WAIT)
    tab_to(page, card, card.get_by_test_id("interview-answer"))
    assert scoped_violations(page, '[data-testid="interview"]') == []
    assert no_page_sideways_scroll(page)
    assert not page._errors, page._errors


def test_audience_versions_simulation_and_memo_by_keyboard_in_the_dark_theme_and_the_table_scrolls_in_place(aiui, pw_browser):
    env, model, server, cid = aiui
    page = dark_page(pw_browser, server, env, f"questions?ui_section=all&chg={cid}")
    ai = page.get_by_test_id("change-ai")
    expect(ai).to_be_visible(timeout=WAIT)
    tab_to(page, ai, ai.get_by_test_id("sim-start"))
    page.keyboard.press("Enter")
    run = ai.get_by_test_id("sim-run")
    expect(run).to_have_attribute("data-status", "REVIEW_READY", timeout=120000)
    table = run.get_by_test_id("sim-table")
    expect(table).to_be_visible()
    assert no_page_sideways_scroll(page)                                                       # the comparison table never widens the page
    assert table.evaluate("t => { let e = t; while (e && e !== document.body) { const o = getComputedStyle(e).overflowX; if (o === 'auto' || o === 'scroll') return true; "
                          "e = e.parentElement; } return t.scrollWidth <= t.parentElement.clientWidth + 1; }"), "the table neither fits nor scrolls in its own area"
    assert scoped_violations(page, '[data-testid="sim-run"]') == []
    memo = ai.get_by_test_id("risk-memo")
    tab_to(page, memo, memo.get_by_test_id("risk-memo-request"))
    page.keyboard.press("Enter")
    expect(memo.get_by_test_id("risk-memo-memo")).to_be_visible(timeout=WAIT)
    assert scoped_violations(page, '[data-testid="risk-memo"]') == []
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/documents")
    topic = page.locator('[data-testid="docs-topic"][data-topic="all"]')
    expect(topic).to_be_visible(timeout=WAIT)
    topic.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "available", timeout=90000)
    panel = page.get_by_test_id("variants")
    sop = panel.locator('[data-testid="variant-row"][data-audience="sop"]')
    expect(sop).to_be_visible(timeout=WAIT)
    tab_to(page, panel, sop.get_by_test_id("variant-generate"))
    page.keyboard.press("Enter")
    expect(panel.get_by_test_id("variant-fallback")).to_be_visible(timeout=WAIT)
    assert scoped_violations(page, '[data-testid="variants"]') == []
    assert no_page_sideways_scroll(page)
    assert not page._errors, page._errors
