"""SYN-03 browser journey: after the approver accepted the knowledge change AND the automatic file edit, the knowledge owner commits and then chooses to let the
product write the approved edit; the page shows the job running and then the source axis as "applied pending readback"; the manual choice writes nothing. Reuses
the CHG-04 browser fixture (live worker). Simulated roles, synthetic understanding stub; not human acceptance, not behaviour."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_chg01_change_request import NUM_REQ  # noqa: E402
from tests.final_loop.test_chg04_browser import ui  # noqa: E402,F401  (ui is a fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_syn03_source_apply import NEW, OLD, YAML_PATH, artifacts  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def test_the_owner_lets_the_product_write_the_approved_edit_and_the_page_shows_the_published_revision(ui, pw_browser):
    env, admin, model, server, approver, viewer = ui
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.get_by_test_id("chg-input").fill(NUM_REQ)
    req.get_by_test_id("chg-submit").click()
    expect(req.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "READY", timeout=WAIT)
    req.get_by_test_id("chg-confirm").click()
    expect(req.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    link = req.url
    a = sign_in(pw_browser, server, env.ws, approver, "ca")
    a.goto(link)
    expect(a.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    a.get_by_test_id("rev-reason").fill("Checked with Finance.")
    a.get_by_test_id("rev-approve").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Knowledge change: Accepted", timeout=WAIT)
    a.get_by_test_id("rev-reason").fill("The YAML edit is exactly the decided value.")
    a.get_by_test_id("rev-approve-files").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Automatic file edits: Accepted", timeout=WAIT)
    assert a.get_by_test_id("rev-apply-source").count() == 0                                           # not committed yet: no choice offered
    owner = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    owner.goto(link)
    expect(owner.get_by_test_id("rev-commit")).to_be_enabled(timeout=WAIT)
    owner.get_by_test_id("rev-commit").click()
    expect(owner.get_by_test_id("rev-completion")).to_have_attribute("data-knowledge", "committed", timeout=WAIT)
    expect(owner.get_by_test_id("rev-source-auto")).to_contain_text("approved")
    head0 = env.store.head(env.tenant, env.ws)
    owner.get_by_test_id("rev-manual-source").click()
    expect(owner.get_by_test_id("rev-note")).to_contain_text("Nothing is written by Ripple", timeout=WAIT)
    assert env.store.head(env.tenant, env.ws) == head0
    expect(owner.get_by_test_id("rev-apply-source")).to_be_enabled()
    owner.get_by_test_id("rev-apply-source").click()
    expect(owner.get_by_test_id("rev-note")).to_contain_text("being written", timeout=WAIT)
    for _ in range(30):                                                                                  # the live worker runs the job; the page is reloaded until it shows the result
        owner.reload()
        expect(owner.get_by_test_id("rev-source")).to_be_visible(timeout=WAIT)
        if owner.get_by_test_id("rev-source").get_attribute("data-job") == "COMPLETED":
            break
        owner.wait_for_timeout(500)
    expect(owner.get_by_test_id("rev-source")).to_have_attribute("data-job", "COMPLETED")
    expect(owner.get_by_test_id("rev-source-job")).to_contain_text("new source revision was published")
    expect(owner.get_by_test_id("rev-completion")).to_contain_text("Source files: applied pending readback")
    expect(owner.get_by_test_id("rev-completion")).to_contain_text("Behaviour: not tested")
    assert owner.get_by_test_id("rev-apply-source").count() == 0                                        # one job per approved edit set
    head1 = env.store.head(env.tenant, env.ws)
    assert head1.source_revision_id != head0.source_revision_id
    q = SimpleNamespace(env=env)
    before, after = artifacts(q, head0.source_revision_id), artifacts(q, head1.source_revision_id)
    old = env.store.read_artifact_blob(env.tenant, env.ws, head0.source_revision_id, before[YAML_PATH]["artifact_id"])["content"]
    new = env.store.read_artifact_blob(env.tenant, env.ws, head1.source_revision_id, after[YAML_PATH]["artifact_id"])["content"]
    assert new == old.replace(OLD, NEW) and all(after[p]["blob_hash"] == before[p]["blob_hash"] for p in after if p != YAML_PATH)
    a.reload()
    expect(a.get_by_test_id("rev-source")).to_have_attribute("data-job", "COMPLETED", timeout=WAIT)
    assert a.get_by_test_id("rev-apply-source").count() == 0 and model.calls == 1
    for page in (req, owner, a):
        assert not page._errors, page._errors
