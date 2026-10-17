"""KNO-06 browser journey on the React build (/app-next/): a knowledge owner asks a model for suggestions, a suggested question appears labelled, is answered through
the ordinary preview/confirm controls (exception text + own words), and leaves with the new knowledge version; a requester sees the label but no controls.

Real server (uvicorn thread), live worker, real Chromium, real routes and store on the throwaway PostgreSQL. STUB model (synthetic suggestion): proves the journey, not
the quality of a real model's suggestions. Simulated roles, not human acceptance. Needs the app-next bundle built from frontend/ (npm run build)."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_kno06_gap_proposals import MANAGER, QUESTION, gap, pobj, rel  # noqa: E402
from tests.final_loop.test_qry03_qa import Model, qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker  # noqa: E402


@pytest.fixture
def ui(qa_env):                                            # noqa: F811
    q = qa_env
    model = Model(lambda prompt: pobj([gap()], [rel()]))
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    worker = LiveWorker(q.env).start()
    server = ServerThread().start()
    yield q.env, model, server
    server.stop()
    worker.stop.set()


def test_suggested_question_is_labelled_answered_with_the_controls_and_leaves_with_the_version(ui, pw_browser):
    env, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    box = page.get_by_test_id("business-gaps-proposals")
    expect(box).to_have_attribute("data-status", "not_requested", timeout=WAIT)
    expect(box).to_contain_text("No model suggestions were requested")
    before = counts(env)
    page.get_by_test_id("business-gaps-propose").click()
    expect(box).to_have_attribute("data-status", "completed", timeout=WAIT)
    expect(box).to_contain_text("suggested 1 question(s) and 1 relation(s)")
    expect(box).to_contain_text("Finding nothing is not proof")
    card = page.locator('[data-testid="business-gap"][data-origin="model_proposal"]')
    expect(card).to_have_count(1, timeout=WAIT)
    expect(card.get_by_test_id("business-gap-origin")).to_contain_text("Suggested by a model")
    expect(card.get_by_test_id("business-gap-question")).to_have_text(QUESTION)
    expect(card).to_have_attribute("data-kind", "missing_exception")
    assert counts(env) == before and model.calls == 1                                         # suggesting writes nothing to the knowledge
    structural = page.locator('[data-testid="business-gap"][data-origin="analysis"]')
    assert structural.count() >= 1 and page.locator('[data-testid="business-gap"]').first.get_attribute("data-origin") == "analysis"   # analysis questions come first
    # a requester sees the labelled question but no controls
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    rcard = req.locator('[data-testid="business-gap"][data-origin="model_proposal"]')
    expect(rcard).to_be_visible(timeout=WAIT)
    expect(rcard.get_by_test_id("business-gap-origin")).to_be_visible()
    expect(rcard.get_by_test_id("business-gap-preview")).to_have_count(0)
    expect(req.get_by_test_id("business-gaps-propose")).to_have_count(0)
    assert not req._errors, req._errors
    # the owner answers it with the ordinary controls: exception text, preview, own words, confirm
    card.get_by_test_id("business-gap-text").fill("Except during a declared emergency, when the duty manager decides alone.")
    card.get_by_test_id("business-gap-preview").click()
    expect(card.get_by_test_id("business-gap-preview-result")).to_contain_text("an exception is added", timeout=WAIT)
    expect(card.get_by_test_id("business-gap-preview-result")).to_have_attribute("data-source", "controls")
    assert counts(env) == before                                                              # a preview records nothing
    expect(card.get_by_test_id("business-gap-confirm")).to_be_disabled()
    card.get_by_test_id("business-gap-quote").fill("Emergencies are decided by the duty manager alone.")
    card.get_by_test_id("business-gap-confirm").click()
    expect(page.locator('[data-testid="business-gap"][data-origin="model_proposal"]')).to_have_count(0, timeout=WAIT)
    expect(page.get_by_test_id("business-gaps-proposals")).to_have_attribute("data-status", "not_requested", timeout=WAIT)   # the new version has no suggestions yet
    after = counts(env)
    assert after["v2_business_decisions"] == before["v2_business_decisions"] + 1 and after["v2_business_snapshots"] == before["v2_business_snapshots"] + 1
    with env.store._tx() as cur:
        cur.execute("SELECT quote FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND kind='answer_gap' ORDER BY created_at DESC LIMIT 1", (env.tenant, env.ws))
        assert cur.fetchone()[0] == "Emergencies are decided by the duty manager alone."
        cur.execute("SELECT count(*) FROM v2_business_rules WHERE tenant_id=%s AND workspace_id=%s AND rule_id=%s", (env.tenant, env.ws, MANAGER + "-target"))
        assert cur.fetchone()[0] >= 1
    assert model.calls == 1 and not page._errors, page._errors
