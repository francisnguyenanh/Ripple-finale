"""CHG-05 browser journey: after a separate approver accepted the knowledge change, a knowledge owner commits it from the review panel; the page shows the four axes
(knowledge committed, source and code pending, behaviour not tested); the approver's view shows the commit; nothing in the files changed. Reuses the CHG-04 browser fixture.
Simulated roles, synthetic understanding stub; not human acceptance."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.adapters.v2_business import business_view  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_chg01_change_request import NUM_REQ  # noqa: E402
from tests.final_loop.test_chg04_browser import ui  # noqa: E402,F401  (ui is a fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402


def test_a_knowledge_owner_commits_the_approved_change_and_the_axes_stay_separate(ui, pw_browser):
    env, admin, model, server, approver, viewer = ui
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.get_by_test_id("chg-input").fill(NUM_REQ)
    req.get_by_test_id("chg-submit").click()
    expect(req.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "READY", timeout=WAIT)
    req.get_by_test_id("chg-confirm").click()
    expect(req.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    link = req.url
    owner = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    owner.goto(link)
    expect(owner.get_by_test_id("rev-completion")).to_have_attribute("data-knowledge", "not_committed", timeout=WAIT)
    expect(owner.get_by_test_id("rev-commit")).to_be_disabled()                                       # no approval yet
    expect(owner.get_by_test_id("rev-why-not-commit")).to_contain_text("approval")
    a = sign_in(pw_browser, server, env.ws, approver, "ca")
    a.goto(link)
    expect(a.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    a.get_by_test_id("rev-reason").fill("Checked with Finance.")
    a.get_by_test_id("rev-approve").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Knowledge change: Accepted", timeout=WAIT)
    assert a.get_by_test_id("rev-commit").count() == 0                                                 # an approver does not commit
    owner.reload()
    expect(owner.get_by_test_id("rev-commit")).to_be_enabled(timeout=WAIT)
    owner.get_by_test_id("rev-commit").click()
    expect(owner.get_by_test_id("rev-note")).to_contain_text("committed as business version 2", timeout=WAIT)
    expect(owner.get_by_test_id("rev-completion")).to_have_attribute("data-knowledge", "committed", timeout=WAIT)
    expect(owner.get_by_test_id("rev-completion")).to_contain_text("Source files: pending")
    expect(owner.get_by_test_id("rev-completion")).to_contain_text("Behaviour: not tested")
    expect(owner.get_by_test_id("rev-committed")).to_contain_text("version 2")
    assert owner.get_by_test_id("rev-commit").count() == 0                                             # committed once; no second button
    a.reload()
    expect(a.get_by_test_id("rev-committed")).to_contain_text("version 2", timeout=WAIT)
    view = business_view(env.store, env.tenant, env.ws)
    rules = {r["rule_id"]: r for r in view["accepted"]["rules"]}
    assert view["business_head"]["business_revision"] == 2 and rules["rule-standard-manager-review-target"]["condition"]["value"] == 1500000
    assert rules["rule-standard-manager-review"]["condition"]["value"] == 1000000 and model.calls == 1
    for page in (req, owner, a):
        assert not page._errors, page._errors
