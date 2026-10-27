"""QRY-04 browser journeys on the React build (/app-next/): ask, read Business and Technical, reload, follow up, stale marker, withheld source text, no-ask role, escaping.

Real server (uvicorn thread), real worker thread, real Chromium, real routes and store on the throwaway PostgreSQL. The model is a synthetic stub: nothing here says an answer is good.
Accounts are SIMULATED roles, not human acceptance.
"""
from __future__ import annotations

import copy
import json
import os
import re
from uuid import uuid4

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.adapters.v2_identity import PostgresIdentityStore  # noqa: E402
from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import bind, ws  # noqa: E402,F401
from tests.final_loop.test_kno03_graph_gaps import seed  # noqa: E402
from tests.final_loop.test_kno05_accept import accept, answer  # noqa: E402
from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE  # noqa: E402
from tests.final_loop.test_chg01_change_request import OP_VALUE, model_obj  # noqa: E402
from tests.final_loop.test_qry03_qa import Model, claim, ids_in, obj  # noqa: E402
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, LiveWorker, drain  # noqa: E402

WAIT = 40000
PARTIAL_QUESTION = "Is this a capital purchase (CAPEX) or an operating expense?"
EVIL_TITLE = "<b id='xss-b'>bold</b> [x](javascript:alert(1)) approval threshold"


def sign_in(browser, server, ws_id, account, credential, page_hash="questions?ui_section=all"):   # these journeys use every section (D2: the page alone opens on Open questions)
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
    page.goto(f"{server.url}/app-next/#/ws/{ws_id}/{page_hash}")
    return page


def question_of(prompt: str) -> str:
    return prompt.rsplit("<question>\n", 1)[1].split("\n</question>")[0]


def stub(prompt):
    if "<request>\n" in prompt:
        return model_obj(ops=[OP_VALUE])
    q = question_of(prompt)
    if "partly" in q.lower():
        return obj("PARTIAL", "", [claim("Standard purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")],
                   questions=[PARTIAL_QUESTION])
    if "source passage" in q.lower():
        src = ids_in(prompt, "source")
        return obj("ANSWERED", "", [claim("The configuration file holds the manager threshold.", [src[0]], "observed")]) if src else obj("ABSTAINED")
    return obj("ANSWERED", "", [claim("Standard purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")])


@pytest.fixture
def ui(env, ws, monkeypatch, tmp_path, request):          # noqa: F811
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("  intent.explain: gemini-3.5-flash-lite", "  intent.explain: gemini-3.5-flash-lite\n  knowledge.qa: gemini-3.8-flash\n  change.understand: gemini-3.8-flash")
    path = tmp_path / "budget_ui.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    model = Model(stub)
    monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    # The markup-title rule tests safe rendering. By default it decides on another measured field, so it is a different rule; with `twin` (indirect parameter) it keeps
    # the same field, actor, outcome and source place as rule-standard-manager-review: an indistinguishable sibling (contract 8). PC-02 (2026-10-26): the plain
    # copy made the happy path below that duplicate case without saying so, and the server rightly asked which rule was meant.
    evil = copy.deepcopy(KNOWLEDGE["observed_rules"][0])
    evil.update(rule_id="rule-evil-title", title=EVIL_TITLE, concept_ids=["concept-evil"])
    if not getattr(request, "param", {}).get("twin"):          # another measured field: a different rule, no disagreement with the manager rule
        evil.update(condition={"field": "request.line_item_count", "op": "gte", "value": 50, "unit": "items"})
    seed(env, ws, rules=bind(KNOWLEDGE["observed_rules"] + DISTRACTORS + [evil], ws.source_revision_id))
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "conflicting_facts")
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "Finance reviews CAPEX from 2,000,000 JPY.")
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    yield env, admin, model, server
    server.stop()
    worker.stop.set()


def ask(page, text):
    page.get_by_test_id("qa-input").fill(text)
    page.get_by_test_id("qa-ask").click()


def test_ask_read_both_views_reload_follow_up_and_stale_marker(ui, pw_browser):
    env, admin, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    rules = page.get_by_test_id("business-rules")
    expect(rules).to_be_visible(timeout=WAIT)
    expect(page.locator("#xss-b")).to_have_count(0)                                              # a rule title is text, never markup
    expect(rules).to_contain_text("<b id='xss-b'>bold</b>")
    assert page.locator("[data-testid=rule][data-authority=TARGET]").count() == 1 and "rule-" not in rules.inner_text()      # Business view shows no ids
    ask(page, "What is the manager approval threshold?")
    answer_box = page.get_by_test_id("qa-answer")
    expect(answer_box).to_be_visible(timeout=WAIT)
    expect(answer_box).to_have_attribute("data-status", "ANSWERED")
    text = page.get_by_test_id("qa-text").inner_text()
    assert "manager approval" in text and model.calls == 1
    expect(page.get_by_test_id("qa-claim")).to_contain_text("what the sources say")
    expect(page.get_by_test_id("qa-retrieval-note")).to_contain_text("can miss relevant rules")
    expect(page.get_by_test_id("qa-technical")).to_have_count(0)
    page.get_by_test_id("mode-technical").click()                                                # the same stored answer in the technical view: no new model call
    tech = page.get_by_test_id("qa-technical")
    expect(tech).to_be_visible(timeout=WAIT)
    expect(page.get_by_test_id("qa-text")).to_have_text(text)
    snapshot = page.get_by_test_id("qa-answer").get_attribute("data-snapshot")
    assert snapshot and "lexicon-expansion" in tech.inner_text() and model.calls == 1
    page.reload()                                                                                # reload keeps the question in the link and shows the stored answer
    expect(page.get_by_test_id("qa-answer")).to_be_visible(timeout=WAIT)
    assert page.get_by_test_id("qa-answer").get_attribute("data-snapshot") == snapshot and model.calls == 1
    page.get_by_test_id("mode-business").click()
    page.get_by_test_id("qa-followup").click()
    ask(page, "And who is that manager?")
    expect(page.get_by_test_id("qa-question")).to_contain_text("And who is that manager?", timeout=WAIT)
    assert model.calls == 2 and "<previous>" in model.prompts[-1]
    with env.store._tx() as cur:                                                                 # the knowledge moves: a decision is revoked and a new version is accepted
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (env.tenant,))
        decision = cur.fetchone()[0]
    assert admin.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    page.reload()
    expect(page.get_by_test_id("qa-newer")).to_be_visible(timeout=WAIT)                          # an old answer is not presented as current
    assert not page._errors, page._errors


def test_a_partial_answer_shows_its_clarifying_questions_and_the_empty_ask_says_why_it_is_disabled(ui, pw_browser):
    """Backend checkpoint D1: `clarifying_questions` may come with PARTIAL; the page shows them whatever the status, worded for a partial answer."""
    env, admin, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    expect(page.get_by_test_id("qa-ask")).to_be_disabled(timeout=WAIT)
    assert page.get_by_test_id("qa-ask").get_attribute("title") == "Write your question first."      # a disabled action says why
    ask(page, "Is a 1,500,000 JPY standard purchase partly covered by the manager rule?")
    box = page.get_by_test_id("qa-answer")
    expect(box).to_have_attribute("data-status", "PARTIAL", timeout=WAIT)
    expect(page.get_by_test_id("qa-status")).to_contain_text("Partly answered")
    clarify = page.get_by_test_id("qa-clarify")
    expect(clarify).to_have_attribute("data-status", "PARTIAL")
    expect(clarify).to_contain_text("To complete this answer, please say:")
    expect(clarify).to_contain_text(PARTIAL_QUESTION)
    expect(page.get_by_test_id("qa-claim")).to_have_count(1)                                        # the answered part stays visible beside the open question
    if os.environ.get("UI_NEXT_SHOTS"):                                                              # for a person to look at, not an oracle
        box.screenshot(path=os.path.join(os.environ["UI_NEXT_SHOTS"], "partial_answer_with_questions.png"))
    assert model.calls == 1 and not page._errors, page._errors


def test_a_reader_without_evidence_rights_sees_withheld_source_claims_and_a_viewer_cannot_ask(ui, pw_browser):
    env, admin, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    ask(page, "Which source passage holds the manager threshold?")
    expect(page.get_by_test_id("qa-claim")).to_contain_text("configuration file", timeout=WAIT)
    link = page.url
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.goto(link)
    expect(req.get_by_test_id("qa-answer")).to_be_visible(timeout=WAIT)
    expect(req.get_by_test_id("qa-withheld")).to_be_visible()
    assert "configuration file" not in req.get_by_test_id("business-ask").inner_text()
    ids = PostgresIdentityStore(env.dsn)
    acct, cred = f"vie-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, acct, ("viewer",))
    viewer = sign_in(pw_browser, server, env.ws, acct, cred)
    expect(viewer.get_by_test_id("qa-no-ask")).to_be_visible(timeout=WAIT)
    expect(viewer.get_by_test_id("qa-input")).to_have_count(0)
    viewer.goto(link)
    expect(viewer.get_by_test_id("qa-answer")).to_be_visible(timeout=WAIT)                      # a viewer may read the stored answer, with source claims withheld
    expect(viewer.get_by_test_id("qa-withheld")).to_be_visible()
    for p in (req, viewer, page):
        assert not p._errors, p._errors


def test_change_request_is_understood_shown_and_confirmed_without_changing_a_rule(ui, pw_browser):
    env, admin, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    page.get_by_test_id("chg-input").fill("Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY.")
    page.get_by_test_id("chg-submit").click()
    box = page.get_by_test_id("chg-understanding")
    expect(box).to_be_visible(timeout=WAIT)
    expect(box).to_have_attribute("data-status", "READY")
    expect(page.get_by_test_id("chg-descriptions")).to_contain_text("1500000 JPY")
    assert "rule-" not in box.inner_text() and model.calls == 1
    page.reload()                                                                                # the stored understanding comes back without a new model call
    expect(page.get_by_test_id("chg-understanding")).to_be_visible(timeout=WAIT)
    assert model.calls == 1
    before = admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["accepted"]
    page.get_by_test_id("chg-confirm").click()
    expect(page.get_by_test_id("chg-confirmed")).to_contain_text("No rule has been changed", timeout=WAIT)
    assert admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["accepted"] == before
    assert not page._errors, page._errors


@pytest.mark.parametrize("ui", [{"twin": True}], indirect=True)
def test_an_indistinguishable_sibling_rule_is_asked_about_never_picked(ui, pw_browser):
    """Contract 8 through the real caller: the model names one of two rules with the same field, actor, outcome and source place; the server asks which one is
    meant (both named, markup shown as text), offers no confirmation, and changes nothing."""
    env, admin, model, server = ui
    page = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    page.get_by_test_id("chg-input").fill("Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY.")
    page.get_by_test_id("chg-submit").click()
    box = page.get_by_test_id("chg-understanding")
    expect(box).to_have_attribute("data-status", "CLARIFY", timeout=WAIT)
    expect(box).to_contain_text("more than one rule fits")
    expect(box).to_contain_text("Purchases of 1,000,000 JPY or more need manager approval")
    expect(box).to_contain_text("<b id='xss-b'>bold</b>")                                       # the other rule's title, as text
    expect(page.locator("#xss-b")).to_have_count(0)
    expect(page.get_by_test_id("chg-confirm")).to_have_count(0)                                    # nothing to confirm until the person chooses
    before = admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["accepted"]
    assert model.calls == 1
    page.reload()
    expect(page.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "CLARIFY", timeout=WAIT)
    assert model.calls == 1 and admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["accepted"] == before
    assert not page._errors, page._errors
