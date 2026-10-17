"""CHG-02 (F-07 c) browser journey: on the review page the requester measures a numeric change on the Harbor sample with one click; the counts appear with the limits
(hard-coded emergency path), and a reload shows the same measured state. Live worker (deterministic job, 0 provider calls), real Chromium, STUB understanding model.
Simulated roles; not human acceptance. Needs the app-next bundle built from frontend/."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_chg01_change_request import NUM_REQ  # noqa: E402
from tests.final_loop.test_chg04_browser import ui  # noqa: E402,F401  (ui is a fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402


def test_requester_measures_the_numeric_change_on_the_harbor_sample_from_the_review_page(ui, pw_browser):
    env, admin, model, server, approver, viewer = ui
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.get_by_test_id("chg-input").fill(NUM_REQ)
    req.get_by_test_id("chg-submit").click()
    expect(req.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "READY", timeout=WAIT)
    req.get_by_test_id("chg-confirm").click()
    box = req.get_by_test_id("rev-simulation")
    expect(box).to_have_attribute("data-status", "not_requested", timeout=WAIT)
    expect(box.get_by_test_id("rev-sim-op")).to_contain_text("NOT_SIMULATED")
    req.get_by_test_id("rev-simulate").click()
    expect(req.get_by_test_id("rev-measured")).to_be_visible(timeout=90000)
    expect(box).to_have_attribute("data-status", "completed")
    expect(box.get_by_test_id("rev-sim-op")).to_contain_text("MEASURED")
    expect(req.get_by_test_id("rev-measured")).to_contain_text("requisitions run")
    expect(req.get_by_test_id("rev-not-controlled")).to_contain_text("emergency")
    expect(req.get_by_test_id("rev-simulate")).to_have_count(0)                                  # measured once per edit set; no second run offered
    req.reload()
    expect(req.get_by_test_id("rev-simulation")).to_have_attribute("data-status", "completed", timeout=WAIT)
    assert model.calls == 1 and not req._errors, req._errors                                     # the measurement asked no model
