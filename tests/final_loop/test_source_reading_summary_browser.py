"""The reading panel says what reading the sources did with the extracted statements (contract section 18; 2026-10-22): configuration values kept as supporting
facts, links between files, and the statements not read as rules because they name no actor and no outcome, listed on demand with their file; a comparison
that could not be linked is named. React build (/app-next/), real server and routes, throwaway PostgreSQL, MOCK extraction (author-written payloads, no
model). Desktop; keyboard: the list opens from the keyboard."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno02_source_links import YAML_TEXT, extract_and_read  # noqa: E402
from tests.final_loop.test_kno04_browser import WAIT, sign_in  # noqa: E402
from tests.ui_next.test_a11y import scoped_violations  # noqa: E402


@pytest.mark.parametrize("renamed", [False, True])
def test_the_reading_panel_lists_statements_not_read_as_rules_and_names_an_unlinked_comparison(env, monkeypatch, pw_browser, renamed):
    yaml_text = YAML_TEXT.replace("capex_finance_threshold_jpy", "capex_threshold_jpy") if renamed else None
    extract_and_read(env, monkeypatch, yaml_text=yaml_text, analyse=False)
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=rules")
        read = page.get_by_test_id("business-read").first
        expect(read).to_have_attribute("data-state", "never", timeout=WAIT)
        read.get_by_test_id("business-read-run").click()
        counts = read.get_by_test_id("business-read-counts")
        expect(counts).to_contain_text("3 configuration value(s) kept as supporting facts")
        expect(counts).to_contain_text("1 statement(s) not read as rules because they name no actor and no outcome")
        listing = read.get_by_test_id("business-read-incomplete")
        listing.locator("summary").focus()
        page.keyboard.press("Enter")                                                                       # opened from the keyboard
        expect(listing).to_contain_text("exceptions")
        expect(listing).to_contain_text("docs/policy.md")
        expect(read.get_by_test_id("business-read-branches")).to_contain_text("3 decision branch(es) read from the code")
        unlinked = read.get_by_test_id("business-read-unlinked")
        if renamed:
            expect(unlinked).to_contain_text("config.capex_finance_threshold_jpy")
        else:
            expect(unlinked).to_have_count(0)
        assert scoped_violations(page, '[data-testid="business-read"]') == []
        assert not page._errors, page._errors
    finally:
        server.stop()
