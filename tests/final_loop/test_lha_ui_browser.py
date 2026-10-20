"""LHA-UI / LLM-UX browser journeys on the React build (/app-next/): conflict scan with AI and an interview on the questions page, audience versions on the documents page,
the what-if simulation and the risk memo on the review of a confirmed change. Real server (uvicorn thread), live worker, real Chromium, real routes, store and ledger on the
throwaway PostgreSQL; SYNTHETIC models (stubs through the production pinned-call path): they prove the screens, states and guards, not model quality. Nothing is started on
page load: every paid step waits for an explicit click. Simulated roles, not human acceptance. Needs the app-next bundle built from frontend/."""
from __future__ import annotations

import os
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_aix_k1_mining import cand, hyp, mobj as mining_obj  # noqa: E402
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_kno07_answer_interpret import mobj as reading_obj  # noqa: E402
from tests.final_loop.test_ma_review_board import _confirmed  # noqa: E402
from tests.final_loop.test_ma_simulation import sim_stub  # noqa: E402
from tests.final_loop.test_qry03_qa import Model, qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_sur06_08_memos import RISK_WORDS, memo_writer  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker  # noqa: E402

PLAIN = "Only the finance director approves these."
HEDGED = "I am not sure which, maybe the finance director."
def _shot(page, name):
    """Full-page screenshot for the owner's visual acceptance when UI_NEXT_SHOTS names a directory (outside the repository); nothing otherwise."""
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"lha_ui_{name}.png"), full_page=True)


def _shot_card(card, name):
    """The one card a checklist row is about, at its own size (readable on a desktop screen)."""
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        card.screenshot(path=os.path.join(target, f"lha_ui_{name}.png"))


PINS = ("document.wording", "document.variant", "knowledge.mining", "gap.interview", "gap.interview_question", "ma.specialist", "change.risk_memo", "ma.synthesis_memo")


def dispatch(prompt: str):
    if "<values>" in prompt:                                                                 # SUR-02 unbound triage
        return {"status": "OK", "notes": [], "items": []}
    if "<planned>" in prompt:                                                                # LHA-K1 candidate pairs
        return cand()
    if "<fields>" in prompt:                                                                 # a mining rule group: one ambiguity over the first two rules shown
        ids = list(dict.fromkeys(re.findall(r"id=(rule-[a-z0-9-]+)", prompt)))
        return mining_obj(hyp(kind="ambiguity", rids=tuple(ids[:2]), case={}, reason="The rules do not say whether one review follows or replaces the other.")) \
            if len(ids) >= 2 else mining_obj(status="NONE_FOUND")
    if "<answer>" in prompt:                                                                 # the KNO-07 reading of an interview answer
        return reading_obj("actor", "finance director", quote=HEDGED if HEDGED in prompt else PLAIN)
    if "You are the interviewer" in prompt:
        return {"status": "OK", "question": "Who approves emergency purchases?", "focus": "kind"}
    if any(w in prompt for w in ("SCENARIO DESIGNER", "ASSUMPTION CRITIC", "RESULT INTERPRETER")):
        return sim_stub(prompt)
    if RISK_WORDS in prompt:
        return memo_writer(prompt)
    if "audience" in prompt.lower():                                                         # audience wording: the model declines; the server writes the labelled fallback
        return {"status": "UNSUPPORTED", "sections": [], "omitted": []}
    raise AssertionError("unexpected prompt")


@pytest.fixture
def aiui(qa_env, monkeypatch, tmp_path):                     # noqa: F811
    q = qa_env
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n" + "".join(f"  {r}: gemini-3.8-flash\n" for r in PINS), 1)
    path = tmp_path / "budget_ui.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setenv("RIPPLE_INTERVIEW_QUESTIONER", "model")
    cid, _approver = _confirmed(q)                                                          # synchronous, before the live worker starts
    model = Model(dispatch)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    worker = LiveWorker(q.env).start()
    server = ServerThread().start()
    yield q.env, model, server, cid
    server.stop()
    worker.stop.set()


def test_conflict_scan_starts_only_on_click_and_a_hypothesis_is_dismissed_and_an_interview_is_confirmed_explicitly(aiui, pw_browser):
    env, model, server, _cid = aiui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    scan = page.get_by_test_id("mining")
    expect(scan).to_be_visible(timeout=WAIT)
    expect(scan.get_by_test_id("mining-scan")).to_be_visible()
    assert model.calls == 0                                                                  # opening the page started nothing
    scan.get_by_test_id("mining-scan").click()
    hyp_ = scan.locator('[data-testid="mining-hypothesis"][data-kind="ambiguity"]')
    try:
        expect(hyp_.first).to_be_visible(timeout=90000)
    except AssertionError:
        raise AssertionError(scan.inner_text()[:1500])
    hyp_ = hyp_.first
    expect(hyp_).to_contain_text("Proposed by a model")
    hyp_.get_by_test_id("mining-dismiss-reason").fill("The finance review follows the manager review in practice.")
    hyp_.get_by_test_id("mining-dismiss").click()
    expect(scan.locator('[data-testid="mining-hypothesis"][data-status="DISMISSED"]')).to_have_count(1, timeout=WAIT)
    card = page.locator('[data-testid="business-gap"][data-kind="missing_actor"]').first
    before = counts(env)
    card.get_by_test_id("interview-start").click()
    open_q = card.get_by_test_id("interview-open-question")
    expect(open_q).to_have_attribute("data-questioner", "model", timeout=WAIT)
    expect(open_q).to_contain_text("Who approves emergency purchases?")
    help_ = card.get_by_test_id("interview-help")                                                    # EUI-05: wording help fills the draft only
    expect(help_).to_be_visible(timeout=WAIT)
    calls_before = model.calls
    help_.get_by_test_id("interview-starter").first.click()
    expect(card.get_by_test_id("interview-blank")).to_be_visible()
    expect(card.get_by_test_id("interview-send")).to_be_disabled()                                   # a [blank] is not an answer
    if help_.get_by_test_id("interview-word").count():
        help_.get_by_test_id("interview-word").first.click()                                          # a word fills the first blank
        assert "[" in card.get_by_test_id("interview-answer").input_value() or card.get_by_test_id("interview-send").is_enabled()
    assert model.calls == calls_before and counts(env)["v2_business_decisions"] == before["v2_business_decisions"]
    card.get_by_test_id("interview-answer").fill(PLAIN)
    card.get_by_test_id("interview-send").click()
    expect(card.get_by_test_id("interview-ready")).to_be_visible(timeout=WAIT)
    preview = card.get_by_test_id("interview-preview")                                           # EUI-05: structured slots from the checked reading
    expect(preview).to_have_attribute("data-reading", "READY")
    expect(preview.locator('[data-testid="interview-slot"]').first).to_be_visible()
    expect(preview).to_contain_text(PLAIN[:20])                                                   # the person's words stay beside the slots
    from tests.ui_next.test_a11y import scoped_violations
    assert scoped_violations(page, '[data-testid="interview"]') == []                             # targeted a11y: help, preview, controls
    assert counts(env)["v2_business_decisions"] == before["v2_business_decisions"]           # READY is not confirmed: nothing recorded yet
    _shot_card(card, "interview_turns_ready")
    card.get_by_test_id("interview-confirm").click()
    expect(page.locator('[data-testid="business-gap"][data-kind="missing_actor"]')).to_have_count(0, timeout=WAIT)
    assert counts(env)["v2_business_decisions"] == before["v2_business_decisions"] + 1
    _shot(page, "questions_scan_interview")
    assert not page._errors, page._errors


def test_a_hedged_interview_answer_is_shown_as_not_confirmable_and_the_person_can_stop_without_a_write(aiui, pw_browser):
    env, model, server, _cid = aiui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    card = page.locator('[data-testid="business-gap"][data-kind="missing_actor"]').first
    expect(card).to_be_visible(timeout=WAIT)
    before = counts(env)
    card.get_by_test_id("interview-start").click()
    expect(card.get_by_test_id("interview-open-question")).to_have_attribute("data-questioner", "model", timeout=WAIT)
    card.get_by_test_id("interview-answer").fill(HEDGED)
    card.get_by_test_id("interview-send").click()
    expect(card.get_by_test_id("interview-incomplete")).to_contain_text("you were not sure", timeout=WAIT)   # the reading is shown but not confirmable
    expect(card.get_by_test_id("interview-open-question")).to_be_visible(timeout=WAIT)                     # a bounded follow-up, not an immediate end
    expect(card.get_by_test_id("interview-ready")).to_have_count(0)
    _shot_card(card, "interview_hedged_not_confirmable")
    card.get_by_test_id("interview-stop").click()
    expect(card.get_by_test_id("interview")).to_have_attribute("data-status", "UNRESOLVED", timeout=WAIT)
    expect(card.get_by_test_id("interview-unresolved")).to_contain_text("You stopped the interview")
    expect(card.get_by_test_id("interview-unresolved")).to_contain_text("Nothing was saved")
    assert counts(env) == before
    assert not page._errors, page._errors


def test_audience_versions_are_not_generated_on_open_and_a_fallback_is_labelled(aiui, pw_browser):
    env, model, server, _cid = aiui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash="documents")
    page.locator('[data-testid="docs-topic"][data-topic="all"]').click()
    expect(page.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "available", timeout=90000)
    panel = page.get_by_test_id("variants")
    expect(panel.locator('[data-testid="variant-row"]')).to_have_count(3, timeout=WAIT)
    calls = model.calls
    sop = panel.locator('[data-testid="variant-row"][data-audience="sop"]')
    expect(sop).to_have_attribute("data-freshness", "not_requested")
    expect(sop.get_by_test_id("variant-consent")).to_contain_text("paid model call")
    sop.get_by_test_id("variant-generate").click()
    expect(sop).to_have_attribute("data-status", "fallback", timeout=90000)
    expect(sop).to_contain_text("Deterministic fallback, no model wording")
    assert model.calls == calls + 1
    expect(panel.get_by_test_id("variant-fallback")).to_be_visible(timeout=WAIT)              # the reader opened on the new version
    expect(panel.get_by_test_id("variant-review-note")).to_contain_text("No reviewer")
    expect(panel.get_by_test_id("variant-content")).to_be_visible(timeout=WAIT)
    _shot(page, "documents_variants")
    assert not page._errors, page._errors


def test_simulation_shows_engine_result_and_checked_interpretation_and_the_risk_memo_lists_what_it_did_not_address(aiui, pw_browser):
    env, model, server, cid = aiui
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash=f"questions?ui_section=all&chg={cid}")
    ai = page.get_by_test_id("change-ai")
    expect(ai).to_be_visible(timeout=WAIT)
    expect(ai.get_by_test_id("sim-none")).to_be_visible()
    assert model.calls == 0
    ai.get_by_test_id("sim-start").click()
    run = ai.get_by_test_id("sim-run")
    expect(run).to_have_attribute("data-status", "REVIEW_READY", timeout=120000)
    expect(run).to_contain_text("Exploratory: not calibrated against observed data")
    expect(run.get_by_test_id("sim-table")).to_be_visible()
    expect(run.get_by_test_id("sim-statement")).to_have_count(1)                              # the invented and the certainty statements were refused by the server
    expect(run.get_by_test_id("sim-interpretation")).to_contain_text("refused")
    assert run.get_by_test_id("sim-interpretation").get_attribute("data-origin") == "ai"             # EUI-01: model text and engine numbers carry separate origins
    expect(run.locator('section[aria-label="Engine result"]')).to_have_attribute("data-origin", "engine")
    assert ai.get_by_test_id("sim-how").get_attribute("open") is None                              # routine explanation collapsed, still reachable
    st = run.get_by_test_id("sim-statement").first                                                  # EUI-07: a statement highlights exactly the rows it cites
    cited = {r.rsplit(".", 1)[0] for r in (x.get_attribute("data-ref") for x in st.get_by_test_id("sim-statement-ref").all())}
    assert cited, "the kept statement cites engine metrics"
    st.get_by_test_id("sim-statement-show").click()
    lit = {x.get_attribute("data-ref") for x in run.locator('[data-testid="sim-row"][data-highlight="true"]').all()}
    assert lit == cited, (lit, cited)
    assert run.get_by_test_id("sim-row-cited").count() >= 1                                          # reverse reference on the row
    st.get_by_test_id("sim-statement-show").click()
    assert run.locator('[data-testid="sim-row"][data-highlight="true"]').count() == 0
    memo = ai.get_by_test_id("risk-memo")
    memo.get_by_test_id("risk-memo-request").click()
    expect(memo.get_by_test_id("risk-memo-memo")).to_be_visible(timeout=WAIT)
    expect(memo.get_by_test_id("risk-memo-statement")).to_have_count(1)
    expect(memo.get_by_test_id("risk-memo-rejected")).to_contain_text("figure_not_in_cited_facts")
    show = memo.get_by_test_id("risk-memo-show-impact")                                         # EUI-07: statement -> the impact rows it cites, by key
    show.first.click()                                                                           # the stub memo cites the HIGH affected rule
    lit = page.locator('[data-testid="rev-affected"][data-highlight="true"]')
    expect(lit).to_have_count(1)
    expect(lit.first).to_be_focused()
    assert page.locator('[data-highlight="true"]').count() == 1                                  # nothing else is marked
    expect(ai.get_by_test_id("synthesis-memo-none")).to_be_visible()                          # no review-board run exists for this change
    _shot(page, "review_simulation_memo")
    assert not page._errors, page._errors


def test_a_simulation_after_the_knowledge_moved_is_shown_as_blocked_with_its_reason_and_starts_nothing(aiui, pw_browser):
    """Finding (e), gallery part B: an interview confirmed an answer, the knowledge moved, and the simulation start was refused; the screen now says why, before a click."""
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    env, model, server, cid = aiui
    admin = env.admin_client()
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(admin, env, gap, {"actor": "finance director"}, PLAIN)
    page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash=f"questions?ui_section=all&chg={cid}")
    ai = page.get_by_test_id("change-ai")
    expect(ai).to_be_visible(timeout=WAIT)
    expect(ai.get_by_test_id("sim-blocked")).to_contain_text("business knowledge changed after this change was confirmed")
    expect(ai.get_by_test_id("sim-start")).to_be_disabled()
    expect(page.get_by_test_id("rev-moved")).to_be_visible()
    _shot(page, "review_simulation_blocked_knowledge_moved")
    assert model.calls == 0 and not page._errors, page._errors
