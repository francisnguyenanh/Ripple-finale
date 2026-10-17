"""KNO-07 browser journey on the React build (/app-next/): a knowledge owner writes an answer in their own words, asks the model to read it into the form; a reading
with an uncovered exception is shown as a clarification with the uncovered words and cannot be confirmed; a plain answer is read, previewed and confirmed with the
person's own words; the structured controls stay available throughout.

Real server (uvicorn thread), live worker, real Chromium, real routes and store on the throwaway PostgreSQL. STUB model (synthetic reading keyed on the text): proves
the journey and the guards, not the quality of a real model's reading. Simulated roles, not human acceptance. Needs the app-next bundle built from frontend/."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_kno07_answer_interpret import TEXT_ACTOR, mobj  # noqa: E402
from tests.final_loop.test_qry03_qa import Model, qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker  # noqa: E402

PLAIN = "Only the finance director approves these."


def reading(prompt):
    text = prompt.rsplit("<answer>\n", 1)[1].split("\n</answer>")[0]
    if "audit season" in text:
        return mobj("actor", "finance director", quote="Only the finance director approves these", uncovered=["except during audit season"])
    return mobj("actor", "finance director", quote=PLAIN)


@pytest.fixture
def ui(qa_env):                                            # noqa: F811
    q = qa_env
    model = Model(reading)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    worker = LiveWorker(q.env).start()
    server = ServerThread().start()
    yield q.env, model, server
    server.stop()
    worker.stop.set()


def test_an_uncovered_part_blocks_confirmation_and_a_plain_answer_is_read_previewed_and_confirmed_in_the_persons_words(ui, pw_browser):
    env, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    card = page.locator('[data-testid="business-gap"][data-kind="missing_actor"]')
    expect(card).to_be_visible(timeout=WAIT)
    expect(card.get_by_test_id("business-gap-interpret")).to_be_disabled()                    # nothing written yet
    before = counts(env)
    card.get_by_test_id("business-gap-quote").fill(TEXT_ACTOR)
    card.get_by_test_id("business-gap-interpret").click()
    warn = card.get_by_test_id("business-gap-interpret-uncovered")
    expect(warn).to_be_visible(timeout=WAIT)
    expect(warn.get_by_test_id("business-gap-uncovered-part")).to_have_text("except during audit season")
    expect(warn).to_contain_text("nothing is proposed from it yet")
    expect(card.get_by_test_id("business-gap-confirm")).to_have_count(0)                      # a partial reading is never confirmable
    expect(card.get_by_test_id("business-gap-preview")).to_be_visible()                       # the controls stay the fallback
    assert counts(env) == before and model.calls == 1
    card.get_by_test_id("business-gap-quote").fill(PLAIN)
    expect(warn).to_have_count(0)                                                             # editing the words clears the old reading
    card.get_by_test_id("business-gap-interpret").click()
    result = card.get_by_test_id("business-gap-preview-result")
    expect(result).to_be_visible(timeout=WAIT)
    expect(result).to_have_attribute("data-source", "reading")
    expect(result).to_contain_text("Read from your words")
    expect(result).to_contain_text("role-finance-director")
    assert counts(env) == before and model.calls == 2                                         # a reading records nothing
    card.get_by_test_id("business-gap-confirm").click()
    expect(page.locator('[data-testid="business-gap"][data-kind="missing_actor"]')).to_have_count(0, timeout=WAIT)
    after = counts(env)
    assert after["v2_business_decisions"] == before["v2_business_decisions"] + 1
    with env.store._tx() as cur:
        cur.execute("SELECT quote, body FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND kind='answer_gap' ORDER BY created_at DESC LIMIT 1", (env.tenant, env.ws))
        quote, body = cur.fetchone()
        assert quote == PLAIN and body["answer"] == {"actor": "role-finance-director"}         # the person's words are the decision; the model only shaped the form
    assert not page._errors, page._errors
