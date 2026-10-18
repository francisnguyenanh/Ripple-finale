"""KNO-04 browser journey on the React build (/app-next/): business questions -> preview -> confirm, and the same page for a role without authority.

Real server (uvicorn thread), real Chromium, real routes and store on the throwaway PostgreSQL. No provider. Accounts are SIMULATED roles, not human acceptance.
Needs the app-next bundle built from frontend/ (npm run build)."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.adapters.v2_business import analyse_workspace, gap_queue  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_kno03_graph_gaps import seed  # noqa: E402

WAIT = 30000


def sign_in(browser, server, ws_id, account, credential):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 1000})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page._errors = errors
    assert page.goto(server.url + "/app-next/").status == 200
    page.get_by_label("Account ID").fill(account)
    page.get_by_label("Credential").fill(credential)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("session-account")).to_have_text(account, timeout=WAIT)
    page.get_by_label("Workspace").select_option(value=ws_id)
    page.goto(f"{server.url}/app-next/#/ws/{ws_id}/questions?ui_section=all")   # rules and business questions (D2: the page alone opens on Open questions)
    return page


def test_business_question_is_answered_in_the_browser_and_a_requester_only_reads(env, ws, pw_browser):
    head = seed(env, ws)
    analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    conflict = next(g for g in gap_queue(env.store, env.tenant, env.ws)["gaps"] if g["kind"] == "conflicting_facts")
    server = ServerThread().start()
    try:
        # a role without knowledge authority sees the question but gets no controls
        req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
        expect(req.locator(f'[data-testid="business-gap"][data-gap-id="{conflict["gap_id"]}"]')).to_be_visible(timeout=WAIT)
        expect(req.get_by_test_id("business-gap-preview")).to_have_count(0)
        assert not req._errors, req._errors

        page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
        card = page.locator(f'[data-testid="business-gap"][data-gap-id="{conflict["gap_id"]}"]')
        expect(card).to_be_visible(timeout=WAIT)
        expect(card.get_by_test_id("business-gap-question")).to_contain_text("Which one is the business rule?")
        before = counts(env)
        expect(card.get_by_test_id("business-gap-preview")).to_be_disabled()              # nothing chosen yet
        card.get_by_label("2000000").check()
        card.get_by_test_id("business-gap-preview").click()
        expect(card.get_by_test_id("business-gap-preview-result")).to_contain_text("source files are not changed", timeout=WAIT)
        assert counts(env) == before                                                      # a preview records nothing
        expect(card.get_by_test_id("business-gap-confirm")).to_be_disabled()              # their own words are required
        card.get_by_test_id("business-gap-quote").fill("Finance reviews CAPEX from 2,000,000 JPY.")
        card.get_by_test_id("business-gap-confirm").click()
        expect(page.locator(f'[data-testid="business-gap"][data-gap-id="{conflict["gap_id"]}"]')).to_have_count(0, timeout=WAIT)
        after = counts(env)
        assert after["v2_business_decisions"] == before["v2_business_decisions"] + 1
        with env.store._tx() as cur:
            cur.execute("SELECT actor_id, quote FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND kind='answer_gap'", (env.tenant, env.ws))
            assert cur.fetchall() == [(env.admin, "Finance reviews CAPEX from 2,000,000 JPY.")]
        # KNO-05 on the same page: the proposal can be accepted by a person with authority, and not by the requester
        expect(req.get_by_test_id("business-accept")).to_have_count(0)
        expect(page.get_by_test_id("business-version")).to_have_attribute("data-revision", "0", timeout=WAIT)
        expect(page.get_by_test_id("business-proposal")).to_contain_text("A proposal is ready")
        page.get_by_test_id("business-accept").click()
        expect(page.get_by_test_id("business-version")).to_have_attribute("data-revision", "1", timeout=WAIT)
        expect(page.get_by_test_id("business-proposal")).to_have_count(0)
        assert counts(env)["v2_business_snapshots"] >= 3
        with env.store._tx() as cur:
            cur.execute("SELECT business_revision, committed_by FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND lifecycle_state='ACCEPTED'", (env.tenant, env.ws))
            assert cur.fetchall() == [(1, env.admin)]
        assert not page._errors, page._errors
    finally:
        server.stop()
