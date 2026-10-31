"""KNO-05 browser journey on the React build (/app-next/): the decision behind a rule decided by people is withdrawn from the Questions page by a person with
knowledge authority (preview of what it supersedes, a reason, one confirm), the knowledge version advances and the question is asked again; a role without
authority gets no control; a stale base is refused and shown. Real server, real Chromium, real routes and store on the throwaway PostgreSQL. No provider."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.adapters.v2_business import analyse_workspace, gap_queue  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_kno03_graph_gaps import seed  # noqa: E402
from tests.final_loop.test_kno04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_kno05_accept import accept, answer, conflict_gap  # noqa: E402

RULE = "rule-capex-finance-config-target"


def test_the_decision_behind_a_rule_is_withdrawn_in_the_browser_and_a_requester_cannot(env, ws, pw_browser):
    seed(env, ws)
    analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    admin_api = env.admin_client()
    gap = conflict_gap(env)
    out = answer(admin_api, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "2 million.")
    assert accept(admin_api, env).status_code == 200
    server = ServerThread().start()
    try:
        req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
        expect(req.locator(f'[data-testid="rule"][data-rule-id="{RULE}"]')).to_be_visible(timeout=WAIT)
        expect(req.get_by_test_id("rule-revoke")).to_have_count(0)                              # no control without knowledge authority
        assert not req._errors, req._errors

        page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
        expect(page.get_by_test_id("business-version")).to_have_attribute("data-revision", "1", timeout=WAIT)
        card = page.locator(f'[data-testid="rule"][data-rule-id="{RULE}"]')
        expect(card).to_be_visible(timeout=WAIT)
        button = card.get_by_test_id("rule-revoke")
        expect(button).to_have_attribute("data-decision-id", out["decision_id"])
        before = counts(env)
        button.click()
        form = card.get_by_test_id("rule-revoke-form")
        expect(form.get_by_test_id("rule-revoke-affected")).to_have_count(1)                   # the preview names what will be marked withdrawn
        expect(form.get_by_test_id("rule-revoke-affected").first).to_have_attribute("data-rule-id", RULE)
        expect(form.get_by_test_id("rule-revoke-confirm")).to_be_disabled()                    # a reason is required
        form.get_by_test_id("rule-revoke-cancel").click()
        expect(card.get_by_test_id("rule-revoke-form")).to_have_count(0)
        assert counts(env) == before                                                           # opening and cancelling records nothing
        card.get_by_test_id("rule-revoke").click()
        card.get_by_test_id("rule-revoke-reason").fill("Finance changed its mind.")
        card.get_by_test_id("rule-revoke-confirm").click()
        expect(page.get_by_test_id("business-version")).to_have_attribute("data-revision", "2", timeout=WAIT)
        expect(page.locator(f'[data-testid="rule"][data-rule-id="{RULE}"]')).to_have_attribute("data-authority", "TARGET")
        expect(page.locator(f'[data-testid="rule"][data-rule-id="{RULE}"] [data-testid="rule-revoke"]')).to_have_count(0)   # withdrawn: no second withdrawal
        expect(page.locator(f'[data-testid="business-gap"][data-gap-id="{gap["gap_id"]}"]')).to_be_visible(timeout=WAIT)     # the question is asked again
        after = counts(env)
        assert after["v2_business_snapshots"] == before["v2_business_snapshots"] + 1 and after["v2_business_decisions"] == before["v2_business_decisions"] + 1
        assert not page._errors, page._errors
    finally:
        server.stop()
