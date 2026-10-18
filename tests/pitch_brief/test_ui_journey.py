"""J4/J5 end-to-end in the BROWSER, in sandbox mode: every step a person does is a UI action; jobs run only through the
bounded sandbox pump (no drain(), no API shortcut, no shell). Three distinct people (requester, admin, approver) take part.
Real server, real Chromium, real routes/store/worker dispatcher; STUB providers only = wiring evidence, not live GenAI."""
from __future__ import annotations

import re

import json
import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, extraction, gap_answer  # noqa: E402
from tests.ai10ui.contract import goto_view, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build, cand_id  # noqa: E402
from tests.pitch_brief.conftest import sandbox_config, checks_ready  # noqa: E402
from tests.pitch_brief.test_change_draft import Model  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import Y  # noqa: E402
from tests.pitch_brief.test_loop_feedback import GapStub, PostApplyExtraction  # noqa: E402
from tests.pitch_brief.test_ui_flow import _account, _page  # noqa: E402

WAIT = 30000
PUMP_JS = """async (ws) => {
    const h = {"X-Ripple-Judge": "1", "Content-Type": "application/json"};
    const r = await fetch("/v2/sandbox/pump", {method: "POST", headers: h, body: JSON.stringify({workspace_id: ws})});
    return {status: r.status, body: await r.json()};
}"""


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _pump(page, env, until=None):
    """What the 'Run queued jobs' button does, until nothing runnable is left. `until` = a condition a just-approved job
    must reach: a held claim is deferred a few seconds (HOLD_SECONDS) before the pump can pick it up, so the person clicks
    again (here: retry once a second)."""
    import time
    if until is not None:
        for _ in range(30):
            out = page.evaluate(PUMP_JS, env.ws)
            assert out["status"] in (200, 202), out
            if until():
                return
            time.sleep(1)
        raise AssertionError("approved job did not run through the pump")
    for _ in range(25):
        out = page.evaluate(PUMP_JS, env.ws)
        assert out["status"] in (200, 202), out
        if out["body"].get("processed", 0) == 0 and not out["body"].get("busy"):
            return
    raise AssertionError("pump never settled")


def _open(page, url):
    page.goto("about:blank")
    page.goto(url)


def test_full_judge_journey_in_the_browser_with_the_bounded_pump(env, ch, monkeypatch, pw_browser):
    approver_acct = _account(env, "change_approver")
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))      # the canonical limits, unchanged
    model = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    server = ServerThread().start()
    try:
        requester = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        approver = _page(pw_browser, server, approver_acct)
        for p in (requester, admin, approver):
            select_workspace(p, env.ws)
        # ---- 0. sandbox: without the lease the requester cannot write (server-enforced)
        goto_view(requester, "Explorer")
        tidc = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        requester.locator(f"[data-node='{tidc}']").click()
        tid(requester, "request-change").click()
        tid(requester, "sf-new").fill("500000")
        tid(requester, "sf-unit").fill("net goods amount")
        tid(requester, "sf-applies").fill("standard, emergency")
        tid(requester, "action-parse-structured").click()
        expect(tid(requester, "error-banner")).to_contain_text("cannot change anything right now", timeout=WAIT)
        # ---- 1. admin takes the writing session for the three accounts
        goto_view(admin, "Sandbox")
        tid(admin, "sandbox-participants").fill(f"{env.req}, {approver_acct[0]}")
        tid(admin, "sandbox-acquire").click()
        expect(tid(admin, "sandbox-lease-state")).to_have_attribute("data-state", "MINE")
        # ---- 2. request from the Explorer rule (prefilled, the person fills the rest), admin confirms the intent
        tid(requester, "action-parse-structured").click()
        expect(tid(requester, "state-intent")).to_have_attribute("data-state", "DRAFT", timeout=WAIT)
        url = requester.url
        _open(admin, url)
        tid(admin, "action-confirm-intent").click()
        expect(tid(admin, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=WAIT)
        # ---- 3. GenAI draft: requester asks, admin approves the model spend, the pump runs it
        _open(requester, url)
        checks_ready(env)
        tid(requester, "action-run-draft").click()
        expect(tid(requester, "state-draft")).to_have_attribute("data-state", "PENDING_APPROVAL", timeout=WAIT)
        url = requester.url
        _pump(admin, env)                                              # nothing runs: the approval is still pending
        assert model.calls == 0
        _open(admin, url)
        tid(admin, "action-approve-draft").click()
        expect(tid(admin, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        # no explicit pump here: the page with a live job asks the bounded pump itself (a judge never touches a shell)
        _open(requester, url)
        expect(tid(requester, "draft-status")).to_have_attribute("data-state", "PARTIAL", timeout=60000)
        assert model.calls == 1
        # ---- 4. impact (with relation coverage), simulation, proposal from the draft
        if tid(requester, "action-run-relations").is_enabled():                 # the checks behind the draft already created it; the button is only for a first run
            tid(requester, "action-run-relations").click()
            expect(tid(requester, "status-live")).to_contain_text("relation coverage", timeout=WAIT)
        else:
            expect(tid(requester, "action-run-relations")).to_have_attribute("title", re.compile("Already computed"))
        _pump(admin, env)
        for button, state in (("action-run-impact", "impact"), ("action-run-simulation", "simulation")):
            tid(requester, button).click()
            expect(tid(requester, "state-" + state)).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
            _pump(admin, env)
            _open(requester, requester.url)
            expect(tid(requester, "state-" + state)).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        if os.environ.get("UI_MODE") == "technical":
            expect(tid(requester, "impact-report")).to_contain_text("Direct (")
        else:       # the same server-built section, titled in business words
            expect(tid(requester, "impact-report")).to_contain_text("Directly affected (")
        # the simulation is Harbor itself (pinned public copy), with a per-transaction table and its limits
        expect(tid(requester, "sim-report")).to_be_visible(timeout=WAIT)
        expect(tid(requester, "sim-integrity")).to_contain_text("identical")
        expect(tid(requester, "sim-summary")).to_contain_text("all explained by the edited keys" if os.environ.get("UI_MODE") == "technical" else "all explained by the edited values")
        assert requester.locator("[data-testid=sim-changed] tbody tr").count() >= 1
        expect(tid(requester, "sim-runner")).to_contain_text("NOT a security boundary")
        expect(tid(requester, "preview-simulation")).to_have_attribute("data-state", "RAN")
        tid(requester, "action-prepare-proposal-from-draft").click()
        expect(tid(requester, "state-proposal")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        _pump(admin, env)
        _open(requester, requester.url)
        expect(tid(requester, "state-proposal")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        url = requester.url
        # ---- 5. approver states the expected value (contract), requester reviews and prepares approval, approver decides
        _open(approver, url)
        expect(tid(approver, "action-confirm-checks")).to_be_enabled(timeout=WAIT)           # the check was built by the server; the approver reads it and confirms
        tid(approver, "action-confirm-checks").click()
        expect(tid(approver, "contract-row")).to_have_count(1, timeout=WAIT)                  # the stage folds to its summary once the check is confirmed
        _open(requester, url)
        tid(requester, "action-run-review").click()
        expect(tid(requester, "state-review")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        _pump(admin, env)
        _open(requester, requester.url)
        expect(tid(requester, "state-review")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        tid(requester, "action-prepare-approval").click()
        expect(tid(requester, "state-approval")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        _pump(admin, env)
        _open(requester, requester.url)
        expect(tid(requester, "state-approval")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        url = requester.url
        _open(approver, url)
        tid(approver, "field-decision-reason").fill("matches the confirmed request")
        tid(approver, "action-approve").click()
        expect(tid(approver, "state-approval-decision")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        # ---- 6. apply (requester), re-extraction wave through the pump, loop status
        post = PostApplyExtraction()
        monkeypatch.setattr(extraction, "_client_for_model", lambda m: post)
        _open(requester, url)
        tid(requester, "action-apply").click()
        # the pump only runs what exists: wait until the server has the apply job (the click's request can arrive late on a loaded machine;
        # a pump that finds nothing returns at once, and the re-analysis then never ran: UI-UX15-01 flake, reproduced by delaying that request)
        expect(tid(requester, "state-apply")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        _pump(admin, env)
        _pump(admin, env)
        _open(requester, requester.url)
        expect(tid(requester, "loop-status")).to_have_attribute("data-state", "PARTIAL", timeout=WAIT)
        assert post.calls == 6
        applied_url = requester.url
        # ---- 7. the explorer shows the NEW revision: the rule is 'updated' with a reason and its history
        goto_view(requester, "Explorer")
        updated = requester.locator("[data-node][data-state=updated]")
        expect(updated).to_have_count(1, timeout=WAIT)
        updated.click()
        expect(tid(requester, "node-detail")).to_contain_text("NOT carried over")
        # the source edit by the apply is on the rule's history (stored provenance: the apply job published this revision)
        expect(tid(requester, "node-timeline")).to_contain_text("source_changed" if os.environ.get("UI_MODE") == "technical"
                                                                else "The source file was edited by an applied change")
        expect(tid(requester, "node-state")).to_contain_text("Value changed by an applied change")
        # ---- 8. query on the new revision (model call approved by a second person, run by the pump)
        gap = GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000")
        gap_calls = lambda: gap.calls
        monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: gap)
        _open(requester, applied_url)
        tid(requester, "field-query").fill("What is the Manager approval limit for standard orders?")
        expect(tid(requester, "query-scope-plan")).to_contain_text("all ")          # no file to pick: every current file is searched by default
        tid(requester, "action-run-query").click()
        expect(tid(requester, "state-query")).to_have_attribute("data-state", "PENDING_APPROVAL", timeout=WAIT)
        qurl = requester.url
        _open(admin, qurl)
        tid(admin, "action-approve-query").click()
        expect(tid(admin, "state-query")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        _pump(admin, env, until=lambda: gap_calls() == 1)
        _open(requester, qurl)
        expect(tid(requester, "query-status")).to_have_attribute("data-state", "answered", timeout=WAIT)
        assert "500000" in tid(requester, "query-answer").inner_text()
        for p in (requester, admin, approver):
            assert not p._errors, p._errors
    finally:
        server.stop()
