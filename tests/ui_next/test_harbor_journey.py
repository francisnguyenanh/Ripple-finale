"""Slice E: the whole Harbor loop in the NEW interface, in sandbox mode, by three distinct people in three browser contexts:
Explorer rule -> request -> administrator confirms -> AI draft (second person allows the model spend) -> relations, impact,
simulation -> proposal -> approver states the expected result -> review -> approval bundle -> approval -> apply -> loop
status -> Explorer shows the updated rule -> a question on the new revision.

Jobs run only through the bounded sandbox pump. STUB providers only: SIMULATED, wiring evidence, not live GenAI.
The Business view is asserted clean (no raw ids, keys, reason codes, JSON; English only) at every stage.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, extraction, gap_answer  # noqa: E402
from tests.ai10ui.contract import goto_view, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build, cand_id  # noqa: E402
from tests.pitch_brief.conftest import sandbox_config, checks_ready  # noqa: E402
from tests.pitch_brief.test_change_draft import Model  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import Y  # noqa: E402
from tests.pitch_brief.test_loop_feedback import GapStub, PostApplyExtraction  # noqa: E402
from tests.pitch_brief.test_ui_flow import _account, _page  # noqa: E402
from tests.pitch_brief.test_ui_journey import PUMP_JS, _open, _pump  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def test_full_harbor_loop_in_the_new_interface_with_a_clean_business_view(env, ch, monkeypatch, pw_browser):
    approver_acct = _account(env, "change_approver")
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))
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

        # ---- every page of the shell is clean in the Business view before anything is done
        for view in ("Overview", "Sources", "Explorer", "Questions", "Changes", "Impact & Simulation", "History", "Support"):
            goto_view(requester, view)
            expect(tid(requester, "page-title")).to_be_visible(timeout=WAIT)
            requester.wait_for_load_state("networkidle")
            assert_business_clean(requester, view)

        # ---- 0. sandbox: without the writing session the requester cannot write (server-enforced)
        goto_view(requester, "Explorer")
        rule = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        requester.locator(f"[data-node='{rule}']").click()
        assert_business_clean(requester, "Explorer with a rule selected")
        tid(requester, "request-change").click()
        tid(requester, "sf-new").fill("500000")
        tid(requester, "sf-unit").fill("net goods amount")
        tid(requester, "sf-applies").fill("standard, emergency")
        tid(requester, "action-parse-structured").click()
        expect(tid(requester, "error-banner")).to_contain_text("cannot change anything right now", timeout=WAIT)
        expect(tid(requester, "lease-bar")).to_be_visible()

        # ---- 1. admin takes the writing session for the three accounts
        goto_view(admin, "Support")
        tid(admin, "sandbox-participants").fill(f"{env.req}, {approver_acct[0]}")
        tid(admin, "sandbox-acquire").click()
        expect(tid(admin, "sandbox-lease-state")).to_have_attribute("data-state", "MINE")
        assert_business_clean(admin, "Support & Access")

        # ---- 2. the request from the Explorer rule, then the administrator confirms it
        tid(requester, "action-parse-structured").click()
        expect(tid(requester, "state-intent")).to_have_attribute("data-state", "DRAFT", timeout=WAIT)
        assert_business_clean(requester, "saved request")
        url = requester.url
        _open(admin, url)
        tid(admin, "action-confirm-intent").click()
        expect(tid(admin, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=WAIT)

        # ---- 3. the AI draft: the requester asks, a second person allows the model spend, the pump runs it
        _open(requester, url)
        checks_ready(env)
        tid(requester, "action-run-draft").click()
        expect(tid(requester, "state-draft")).to_have_attribute("data-state", "PENDING_APPROVAL", timeout=WAIT)
        url = requester.url
        _pump(admin, env)
        assert model.calls == 0
        _open(admin, url)
        tid(admin, "action-approve-draft").click()
        expect(tid(admin, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        _open(requester, url)
        expect(tid(requester, "draft-status")).to_have_attribute("data-state", "PARTIAL", timeout=60000)
        assert model.calls == 1
        assert_business_clean(requester, "AI draft")

        # ---- 4. relations, impact, simulation, proposal from the draft
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
        expect(tid(requester, "impact-report")).to_contain_text("Directly affected")
        expect(tid(requester, "sim-report")).to_be_visible(timeout=WAIT)
        expect(tid(requester, "sim-integrity")).to_contain_text("identical")
        expect(tid(requester, "sim-summary")).to_contain_text("all explained by the edited")
        assert requester.locator("[data-testid=sim-changed] tbody tr").count() >= 1
        expect(tid(requester, "sim-runner")).to_contain_text("NOT a security boundary")
        expect(tid(requester, "preview-simulation")).to_have_attribute("data-state", "RAN")
        assert_business_clean(requester, "impact and simulation")
        tid(requester, "action-prepare-proposal-from-draft").click()
        expect(tid(requester, "state-proposal")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        _pump(admin, env)
        _open(requester, requester.url)
        expect(tid(requester, "state-proposal")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        expect(tid(requester, "diff-view")).to_be_visible()
        assert_business_clean(requester, "proposal")
        url = requester.url

        # ---- 5. the approver states the expected result independently; the requester reviews and prepares the approval
        _open(approver, url)
        tid(approver, "contract-advanced").locator("summary").click()                         # the manual path (Advanced) still works
        tid(approver, "field-contract-path").fill(APPROVAL)
        tid(approver, "field-contract-locator").fill(Y)
        tid(approver, "field-contract-expected").fill("500000")
        tid(approver, "action-set-contract").click()
        expect(tid(approver, "contract-row")).to_have_count(1, timeout=WAIT)           # the stage folds to its summary once the check is saved
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
        assert_business_clean(approver, "approval")
        expl = tid(approver, "ai-explanation")                       # stored AI wording is read, never generated by viewing; the approver may ask for one (own AI permission) but nothing is asked here
        expect(expl).to_have_attribute("data-state", "none", timeout=WAIT)
        expect(expl).to_contain_text("No explanation has been written for the current state")
        assert tid(approver, "ai-explanation-request").count() == 1
        tid(approver, "field-decision-reason").fill("matches the confirmed request")
        tid(approver, "action-approve").click()
        expect(tid(approver, "state-approval-decision")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)

        # ---- 6. apply (requester), re-analysis wave through the pump, loop status
        post = PostApplyExtraction()
        monkeypatch.setattr(extraction, "_client_for_model", lambda m: post)
        _open(requester, url)
        tid(requester, "action-apply").click()
        _pump(admin, env)
        _pump(admin, env)
        _open(requester, requester.url)
        expect(tid(requester, "loop-status")).to_have_attribute("data-state", "PARTIAL", timeout=WAIT)
        assert post.calls == 6
        applied_url = requester.url
        assert_business_clean(requester, "result of the applied change")
        # finished stages are folded to one summary line that still carries who, why and what is unresolved; the person can reopen them
        draft = tid(requester, "flow-draft")
        expect(draft).to_have_attribute("data-open", "false")
        expect(tid(requester, "flow-draft-summary")).to_contain_text("open point")
        expect(tid(requester, "flow-approval-summary")).to_contain_text("matches the confirmed request")
        expect(tid(requester, "flow-approval-summary")).to_contain_text("Approved")
        tid(requester, "flow-draft-toggle").click()
        expect(draft).to_have_attribute("data-open", "true")
        tid(requester, "flow-draft-toggle").click()
        expect(draft).to_have_attribute("data-open", "false")
        tid(requester, "stages-expand-all").click()
        expect(draft).to_have_attribute("data-open", "true")
        tid(requester, "stages-expand-all").click()
        expect(draft).to_have_attribute("data-open", "false")

        # ---- 7. the Explorer shows the NEW revision: the rule is 'updated' with a reason and its history
        goto_view(requester, "Explorer")
        updated = requester.locator("[data-node][data-state=updated]")        # the tree is the default view
        expect(updated).to_have_count(1, timeout=WAIT)
        updated.get_by_test_id("rule-toggle").click()
        expect(tid(requester, "node-detail")).to_contain_text("NOT carried over")
        expect(tid(requester, "node-timeline")).to_contain_text("The source file was edited by an applied change")      # the stored apply provenance names the apply that edited it
        assert_business_clean(requester, "Explorer after the change")

        # ---- 8. ask on the new revision (model call allowed by a second person, run by the pump)
        gap = GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000")
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
        _pump(admin, env, until=lambda: gap.calls == 1)
        _open(requester, qurl)
        expect(tid(requester, "query-status")).to_have_attribute("data-state", "answered", timeout=WAIT)
        assert "500000" in tid(requester, "query-answer").inner_text()
        assert_business_clean(requester, "answer on the new revision")
        for p in (requester, admin, approver):
            assert not p._errors, p._errors
    finally:
        server.stop()
