"""SYN-04 browser journey: the review page shows where the change stands per axis for every role, the next actor and action, and keeps the state across a
role handoff and a reload; a committed knowledge change is shown as knowledge-complete but never as implemented while the files are pending; after the
automatic edit the source axis is "partial" and the change is still not implemented. Reuses the CHG-04 browser fixture (live worker). Simulated roles; not human
acceptance."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_chg01_change_request import NUM_REQ  # noqa: E402
from tests.final_loop.test_chg04_browser import ui  # noqa: E402,F401  (ui is a fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402


def step(page, key):
    return page.locator(f"[data-testid=rev-journey-step][data-step={key}]")


def test_every_role_sees_the_same_axes_and_the_next_action_and_completion_is_never_implied(ui, pw_browser):
    env, admin, model, server, approver, viewer = ui
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.get_by_test_id("chg-input").fill(NUM_REQ)
    req.get_by_test_id("chg-submit").click()
    expect(req.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "READY", timeout=WAIT)
    req.get_by_test_id("chg-confirm").click()
    expect(req.get_by_test_id("rev-journey")).to_be_visible(timeout=WAIT)
    expect(step(req, "confirm")).to_have_attribute("data-state", "done")
    expect(step(req, "review")).to_have_attribute("data-state", "current")
    expect(req.get_by_test_id("rev-journey-next")).to_contain_text("approver")
    expect(req.get_by_test_id("rev-journey")).to_have_attribute("data-knowledge-complete", "false")
    link = req.url
    a = sign_in(pw_browser, server, env.ws, approver, "ca")
    a.goto(link)
    expect(step(a, "review")).to_have_attribute("data-state", "current", timeout=WAIT)                  # the approver sees the same axes
    a.get_by_test_id("rev-reason").fill("Checked with Finance.")
    a.get_by_test_id("rev-approve").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Knowledge change: Accepted", timeout=WAIT)
    expect(step(a, "review")).to_have_attribute("data-state", "done", timeout=WAIT)
    expect(step(a, "commit")).to_have_attribute("data-state", "current")
    expect(a.get_by_test_id("rev-journey-next")).to_contain_text("knowledge owner")
    a.get_by_test_id("rev-reason").fill("The YAML edit is exactly the decided value.")
    a.get_by_test_id("rev-approve-files").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Automatic file edits: Accepted", timeout=WAIT)
    owner = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    owner.goto(link)
    expect(owner.get_by_test_id("rev-commit")).to_be_enabled(timeout=WAIT)
    owner.get_by_test_id("rev-commit").click()
    expect(owner.get_by_test_id("rev-journey")).to_have_attribute("data-knowledge-complete", "true", timeout=WAIT)
    expect(owner.get_by_test_id("rev-journey")).to_have_attribute("data-implemented", "false")
    expect(step(owner, "commit")).to_have_attribute("data-state", "done")
    expect(step(owner, "source_sync")).to_have_attribute("data-state", "current")
    expect(step(owner, "behaviour")).to_have_attribute("data-state", "todo")
    expect(owner.get_by_test_id("rev-journey-next")).to_contain_text("Choose")
    expect(owner.get_by_test_id("rev-journey-statement")).to_contain_text("Not fully implemented")
    owner.reload()                                                                                        # the state is server lineage: a reload shows the same
    expect(owner.get_by_test_id("rev-journey")).to_have_attribute("data-knowledge-complete", "true", timeout=WAIT)
    expect(step(owner, "source_sync")).to_have_attribute("data-state", "current")
    owner.get_by_test_id("rev-apply-source").click()
    expect(owner.get_by_test_id("rev-note")).to_contain_text("being written", timeout=WAIT)
    for _ in range(30):
        owner.reload()
        expect(owner.get_by_test_id("rev-journey")).to_be_visible(timeout=WAIT)
        if step(owner, "source_sync").get_attribute("data-state") == "partial":
            break
        owner.wait_for_timeout(500)
    expect(step(owner, "source_sync")).to_have_attribute("data-state", "partial")                         # published, not read back: not implemented
    expect(owner.get_by_test_id("rev-journey")).to_have_attribute("data-implemented", "false")
    expect(step(owner, "readback")).to_have_attribute("data-state", "current")
    req.reload()
    expect(step(req, "source_sync")).to_have_attribute("data-state", "partial", timeout=WAIT)             # the asker sees the handoff result without evidence values
    assert model.calls == 1
    for page in (req, owner, a):
        assert not page._errors, page._errors
