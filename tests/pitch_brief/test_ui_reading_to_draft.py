"""Review claims 3 and 4 through the browser: the AI reading of a simulation is not a dead list. Its questions and options are OPEN POINTS of the draft (same comment ->
confirm -> revised-draft path as every other point). A requester can comment but not decide; a person with confirm rights decides; the next draft is given the decision as a stated
assumption (with who) and shows it as applied. Nothing is applied, nothing becomes a measured fact. Real server, real Chromium, real routes/store/worker, STUB providers."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, explain  # noqa: E402
from tests.ai10ui.contract import UI_BASE, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker, checks_ready, create_job, drain, job_body  # noqa: E402
from tests.pitch_brief.test_change_draft import Model, _res, run  # noqa: E402
from tests.pitch_brief.test_sim_analysis import Reader, pinned  # noqa: E402,F401
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000
ANSWER = "Emergency orders keep the old limit; only standard orders follow the new one."


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(page, url):
    page.goto("about:blank")
    page.goto(url)


def test_the_reading_questions_become_open_points_a_person_decides_and_the_next_draft_uses(env, ch, pinned, monkeypatch, pw_browser):
    jid, body = run(env, ch, monkeypatch, Model())
    intent_id = _res(body)["intent"]["intent_id"]
    sim, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "from_draft_job_id": jid})
    assert r.status_code == 200, r.text
    drain(env)
    assert job_body(ch.admin, env, sim)["execution_result"]["status"] == "EXECUTED"
    reader = Reader()
    monkeypatch.setattr(explain, "_client_for_model", lambda m: reader)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    made = env.req_client().post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sim})
    assert made.status_code == 200, made.text
    drain(env)
    server = ServerThread().start()
    try:
        requester = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        for p in (requester, admin):
            select_workspace(p, env.ws)
        url = f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={jid}&simulation={sim}"
        _open(requester, url)
        # the AI reading is shown, and each question/option says where it stands: an open point of the draft
        expect(tid(requester, "sim-analysis")).to_have_attribute("data-state", "done", timeout=WAIT)
        expect(tid(requester, "sim-analysis-questions")).to_contain_text("Should emergency orders follow the same limit?")
        expect(tid(requester, "sim-analysis-questions").get_by_test_id("reading-point")).to_have_attribute("data-state", "unresolved")
        point = requester.locator("[data-testid=gap-item][data-kind=reading_question]")
        expect(point).to_contain_text("AI reading of the simulation asks: Should emergency orders follow the same limit?", timeout=WAIT)
        assert point.get_by_test_id("gap-confirm").count() == 0                         # a requester cannot decide it
        expect(tid(requester, "sim-analysis-options").get_by_test_id("reading-point")).to_have_attribute("data-state", "unresolved")
        assert tid(requester, "sim-analysis-explore").count() >= 1                       # the what-if is offered for an option (it says when it cannot simulate it)
        # the option travels WITH its text into the what-if (proposed, editable, not sent); it is not an empty box
        tid(requester, "sim-analysis-options").get_by_test_id("sim-analysis-explore").first.click()
        expect(tid(requester, "flow-whatif").locator("textarea").first).to_have_value(re.compile("Keep emergency as is"), timeout=WAIT)
        assert "wis=" in requester.url
        _open(requester, url)
        # a comment is not a decision and does not reach the next draft
        point.get_by_test_id("gap-text").fill("Probably yes")
        point.get_by_test_id("gap-comment-save").click()
        expect(point.get_by_test_id("gap-state")).to_have_attribute("data-state", "commented", timeout=WAIT)
        # a person with confirm rights decides it, with their name
        _open(admin, url)
        apoint = admin.locator("[data-testid=gap-item][data-kind=reading_question]")
        apoint.get_by_test_id("gap-text").fill(ANSWER)
        apoint.get_by_test_id("gap-why").fill("agreed with finance")
        apoint.get_by_test_id("gap-confirm").click()
        expect(apoint.get_by_test_id("gap-state")).to_have_attribute("data-state", "confirmed", timeout=WAIT)
        expect(apoint.get_by_test_id("gap-pending")).to_be_visible()                     # decided, but this draft does not use it yet
        expect(tid(admin, "draft-stale")).to_be_visible()
        _open(requester, url)
        expect(tid(requester, "sim-analysis-questions").get_by_test_id("reading-point")).to_contain_text(f"Decided by {env.admin}", timeout=WAIT)
        # the next draft is given exactly the decision, as an assumption, and says it applied it
        model2 = Model()
        monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model2)
        checks_ready(env)
        tid(requester, "action-run-draft").click()
        expect(tid(requester, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        url2 = requester.url
        drain(env)
        _open(requester, url2)
        expect(tid(requester, "clarifications-used")).to_contain_text(env.admin, timeout=WAIT)
        expect(tid(requester, "feedback-log-item")).to_have_attribute("data-status", "applied")           # and the page says what THIS draft did with the confirmed answer
        expect(tid(requester, "feedback-log-item")).to_contain_text(f"confirmed by {env.admin}")
        assert ANSWER in model2.prompts[0] and "Probably yes" not in model2.prompts[0] and "confirmed_clarifications" in model2.prompts[0]
        for p in (requester, admin):
            assert not p._errors, p._errors
    finally:
        server.stop()


def test_a_finished_what_if_can_start_a_request_that_the_person_confirms_and_creates_nothing_by_itself(env, ch, pinned, monkeypatch, pw_browser):
    """Review claim 7: what-if -> change request. The link only opens the NEW request box with a sentence proposed; no intent, approval or apply exists until the person sends and confirms it."""
    sim, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "case": "route",
                                                              "edits": [{"path": "routing_rules.standard_manager_threshold_jpy", "value": 500000, "expected_current": 1000000}]})
    assert r.status_code == 200, r.text
    drain(env)

    def counts():
        with env.store._tx() as cur:
            cur.execute("SELECT (SELECT count(*) FROM v2_intents WHERE tenant_id=%s AND workspace_id=%s), (SELECT count(*) FROM v2_chat_conversations WHERE tenant_id=%s AND workspace_id=%s), "
                        "(SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind IN ('proposal','approval','apply'))", (env.tenant, env.ws) * 3)
            return cur.fetchone()
    before = counts()
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        _open(page, f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?whatif={sim}")
        box = tid(page, "whatif-request")
        expect(box).to_be_visible(timeout=WAIT)
        expect(box).to_contain_text("Please change the Manager approval threshold")
        expect(box).to_contain_text("500,000")
        expect(box).to_contain_text("does not create or approve anything")
        # the AI reading of this stand-alone what-if: asked for by the person, read by them; its question travels into the what-if box, nothing is created
        monkeypatch.setattr(explain, "_client_for_model", lambda m: Reader())
        monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
        worker = LiveWorker(env).start()
        try:
            expect(tid(page, "sim-analysis")).to_have_attribute("data-state", "none", timeout=WAIT)
            tid(page, "sim-analysis-request").click()
            expect(tid(page, "sim-analysis")).to_have_attribute("data-state", "done", timeout=90000)
            expect(tid(page, "sim-analysis-questions")).to_contain_text("Should emergency orders follow the same limit?")
            assert tid(page, "sim-analysis-questions").get_by_test_id("reading-point").count() == 0           # a what-if has no draft: no open point is invented
        finally:
            worker.stop.set()
        assert counts() == before                                                                              # still no intent, conversation, proposal, approval or apply
        tid(page, "action-whatif-to-request").click()
        expect(tid(page, "chat-input")).to_have_value(__import__("re").compile(r"Please change the .* 500,000"), timeout=WAIT)      # proposed, editable, not sent
        assert counts() == before                                                                                                  # nothing exists yet: no intent, no conversation, no proposal/approval/apply
        assert not page._errors, page._errors
    finally:
        server.stop()
