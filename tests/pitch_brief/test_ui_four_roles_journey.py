"""ONE change request through FOUR separate browser contexts (requester, reviewer, approver, administrator) with the journey panel read at every hand-off.
Real server, real Chromium, real routes/store/worker (jobs run by the test's own worker drain), STUB providers = wiring evidence, not live GenAI quality.
Requirement -> evidence:
  * at every step each role sees WHO has to act and what the step is (state + "Your action" only for the right person)     -> _journey(...)
  * the check after the change is built by the server and confirmed by an approver; the requester cannot confirm it          -> phase 1
  * a reviewer's open request blocks approval and the page names the reviewer; marking it ready releases it                   -> phase 2
  * the requester applies only after approval; the approver and the reviewer cannot                                           -> phase 3
  * after apply nothing is reported complete: refresh runs, the result still needs a person's decision                        -> phase 4
  * the question on the new revision searches every file by default and says so; nothing about files is typed                 -> phase 5
  * the administrator does administration (members, AI switch), is not asked to do approver steps, holds no apply button      -> phase 6
  * the same states after a reload (they come from the server, not the browser)                                              -> phase 7"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import extraction, gap_answer  # noqa: E402
from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import drain  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal  # noqa: E402
from tests.pitch_brief.test_loop_feedback import GapStub, PostApplyExtraction  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _account, _signed_in  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _step(page, key):
    return page.locator(f"[data-testid=journey] li[data-step={key}]")


def _journey(page, key, state, mine=None, who=None, timeout=WAIT):
    """The journey row of one step: its state, whether it is THIS account's action, and who is named."""
    row = _step(page, key)
    expect(row).to_have_attribute("data-step-state", state, timeout=timeout)
    if mine is not None:
        expect(row).to_have_attribute("data-mine", "true" if mine else "false")
    if who:
        expect(row).to_contain_text(who)


def _open(page, url):
    page.goto("about:blank")
    page.goto(url)
    expect(tid(page, "journey")).to_be_visible(timeout=WAIT)


def test_one_change_through_requester_reviewer_approver_and_administrator(env, ch, monkeypatch, pw_browser):
    c = chain_to_proposal(env, ch, monkeypatch)                          # draft -> impact -> simulation -> proposal exist (API setup of the earlier steps)
    server = ServerThread().start()
    try:
        apv_acct, rev_acct = _account(env, "change_approver"), _account(env, "reviewer")
        c1, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        c2, apv = _signed_in(pw_browser, server, env, apv_acct)
        c3, rev = _signed_in(pw_browser, server, env, rev_acct)
        c4, adm = _signed_in(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        base = f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}"
        for p in (req, apv, rev, adm):
            _open(p, base)

        # ---- phase 1: the check after the change. Only an approver acts; everybody else is told who.
        _journey(apv, "check", "current", mine=True)
        _journey(req, "check", "waiting", mine=False, who="approver")
        _journey(rev, "check", "waiting", mine=False, who="approver")
        _journey(adm, "check", "waiting", mine=False, who="approver")                       # an administrator is not asked to do approver steps
        for k in ("request", "draft", "analysis", "proposal"):
            _journey(req, k, "done" if k != "draft" else "partial", mine=False)             # the draft is done but only partly processed: said so
        assert tid(req, "action-confirm-checks").count() == 0 and tid(rev, "action-confirm-checks").count() == 0   # nobody else is offered the confirm button
        expect(tid(req, "suggested-check")).to_contain_text("should be JPY 500,000 in approval.yaml")                 # the requester READS the check, built from the confirmed request
        expect(tid(req, "check-waiting")).to_contain_text("independent approver")
        expect(tid(apv, "suggested-check")).to_contain_text("should be JPY 500,000 in approval.yaml")
        tid(apv, "action-confirm-checks").click()
        expect(tid(apv, "contract-row")).to_have_count(1, timeout=WAIT)
        for p in (req, apv, rev, adm):
            p.reload()
            expect(tid(p, "journey")).to_be_visible(timeout=WAIT)
            _journey(p, "check", "done")                                                    # the same for everybody, and after a reload
            tc = p.evaluate("document.body.textContent")                                     # the hidden body counts too: copied or read aloud it must not say the same thing twice
            assert tc.count("must say") == 1 and tc.count("Check confirmed by") == 1 and tc.count(f"{apv_acct[0]}") >= 1, (tc.count("must say"), tc.count("Check confirmed by"))
        _journey(req, "review", "current", mine=True)                                       # the requester's turn: run the review
        _journey(apv, "review", "waiting", mine=False, who="requester")

        # ---- phase 2: review run, approval bundle, a reviewer's open request blocks the approver
        tid(req, "action-run-review").click()
        expect(tid(req, "state-review")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        drain(env)
        expect(tid(req, "state-review")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        tid(req, "action-prepare-approval").click()
        expect(tid(req, "state-approval")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        drain(env)
        _open(req, req.url)
        expect(tid(req, "state-approval")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        url = req.url
        for p in (apv, rev, adm):
            _open(p, url)
        _journey(apv, "approval", "current", mine=True)
        _journey(req, "approval", "waiting", mine=False, who="approver")
        tid(rev, "review-note-text").fill("Please state the currency in the request.")
        tid(rev, "review-request-changes").click()
        expect(tid(rev, "review-open-request")).to_be_visible(timeout=WAIT)
        for p in (apv, req):
            _open(p, url)
            _journey(p, "approval", "blocked", mine=False, who=rev_acct[0])                 # the page names the reviewer, it does not invent one
        expect(tid(apv, "action-approve")).to_be_disabled()
        tid(rev, "review-note-text").fill("")
        tid(rev, "review-ready").click()
        expect(tid(rev, "review-open-request")).to_have_count(0, timeout=WAIT)
        _open(apv, url)
        _journey(apv, "approval", "current", mine=True)

        # ---- phase 3: approval, then apply by the one who may apply
        expect(tid(apv, "decision-basis")).to_have_attribute("data-complete", "true", timeout=WAIT)
        tid(apv, "field-decision-reason").fill("Checked the diff and the simulated cases.")
        tid(apv, "action-approve").click()
        expect(tid(apv, "state-approval-decision")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        for p in (req, apv, rev, adm):
            _open(p, url)
        _journey(req, "apply", "current", mine=True)
        _journey(apv, "apply", "waiting", mine=False, who="requester")
        _journey(rev, "apply", "waiting", mine=False)
        assert tid(apv, "action-apply").count() == 0 and tid(rev, "action-apply").count() == 0 and tid(adm, "action-apply").count() == 1   # the administrator holds change.apply; the approver and the reviewer do not
        post = PostApplyExtraction()
        monkeypatch.setattr(extraction, "_client_for_model", lambda m: post)
        tid(req, "action-apply").click()
        expect(tid(req, "state-apply")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)

        # ---- phase 4: applied, but NOT complete: verification and the knowledge update run, then a person decides the result
        drain(env)
        drain(env)
        _open(req, req.url)
        _journey(req, "apply", "done")
        row = _step(req, "refresh")
        assert row.get_attribute("data-step-state") in ("waiting", "done", "partial"), row.get_attribute("data-step-state")
        expect(tid(req, "state-completion")).not_to_have_attribute("data-state", "COMPLETED")           # never reported complete before a person decides
        assert _step(req, "accept").get_attribute("data-step-state") in ("todo", "waiting", "current")
        assert _step(req, "accept").get_attribute("data-mine") == "false"                                 # the requester holds no result permission: the role is named
        applied = req.url

        # ---- phase 5: ask again on the new revision: no file to pick, the scope is said
        gap = GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000")
        monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: gap)
        _open(req, applied)
        tid(req, "field-query").fill("What is the standard manager approval threshold now?")
        expect(tid(req, "query-scope-plan")).to_contain_text("all ")
        assert tid(req, "query-scope-plan").get_attribute("data-narrowed") == "false"
        tid(req, "action-run-query").click()
        expect(tid(req, "state-query")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        drain(env)
        _open(req, req.url)
        expect(tid(req, "query-status")).to_have_attribute("data-state", "answered", timeout=WAIT)
        expect(tid(req, "query-scope")).to_contain_text("Answered on the current source revision")

        # ---- phase 6: the administrator administers, and is not an approver
        adm.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/support")
        expect(tid(adm, "member-ai-off").first).to_be_visible(timeout=WAIT)
        _open(adm, applied)
        assert tid(adm, "action-approve").count() == 0 and tid(adm, "action-confirm-checks").count() == 0

        # ---- phase 6b: three separate facts; the result decision says why it is not possible yet (a person who may decide sees it; nobody else is offered the button)
        _open(apv, applied)
        expect(tid(apv, "status-axes")).to_be_visible(timeout=WAIT)
        assert tid(apv, "axis-execution").get_attribute("data-status") in ("VERIFIED", "PARTIAL")             # the system's own check
        assert tid(apv, "axis-result").get_attribute("data-status") != "COMPLETED"                           # a person has not accepted this request's result
        assert tid(apv, "axis-knowledge").get_attribute("data-status") in ("PARTIAL", "COMPLETE")           # a third fact, not derived from the other two
        expect(tid(apv, "axis-result")).to_contain_text("not yet")
        assert tid(apv, "action-accept-result").is_disabled()
        expect(tid(apv, "accept-blocked-why")).to_contain_text("not possible yet")
        assert tid(req, "action-accept-result").count() == 0 and tid(rev, "action-accept-result").count() == 0

        # ---- phase 8: knowledge is accepted again after a NEW decision; then the result is decided item by item (the server checks it, the page only offers it)
        from infra import v2_current_findings as CF
        real = CF.classify_for_change

        def with_items(*a, **k):
            out = real(*a, **k)
            out["possible_effect"] = list(out.get("possible_effect", [])) + [
                {"finding_id": "fnd-ui-1", "kind": "GAP", "reason": "missing_scope", "severity": "HIGH", "plain": "p", "todo": "t",
                 "claims": [{"candidate_id": "c1", "logical_path": "x/emergency.py", "subject": "capex"}]}]
            out["same_concept"] = list(out.get("same_concept", [])) + [
                {"item_id": "reading:ui-guide", "candidate_id": "g1", "logical_path": "x/finance_desk_guide.md", "subject": "purchasing commitments", "value": 2000000, "stance": "old_value"}]
            return out
        monkeypatch.setattr(CF, "classify_for_change", with_items)
        _open(adm, applied)
        tid(adm, "action-accept-km").click()
        expect(tid(adm, "state-knowledge")).to_have_attribute("data-state", "ACCEPTED", timeout=WAIT)
        with env.store._tx() as cur:                                                                             # a person records ANY new decision: the accepted version is no longer current
            cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED' AND execution_result IS NOT NULL", (env.tenant, env.ws))
            claim = [x["candidate_id"] for (r,) in cur.fetchall() for x in (r.get("candidates") or [])][-1]
        assert env.admin_client().post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": claim, "rationale": "a person decides something else"}).status_code == 200
        _open(adm, applied)
        expect(tid(adm, "action-accept-km")).to_be_visible(timeout=WAIT)                                         # offered again, for the NEW epoch
        tid(adm, "action-accept-km").click()
        expect(tid(adm, "state-knowledge")).to_have_attribute("data-state", "ACCEPTED", timeout=WAIT)
        _open(apv, applied)
        expect(tid(apv, "disposition-group")).to_have_count(2, timeout=WAIT)
        assert tid(apv, "action-accept-result").is_disabled()                                                    # "I read them" is not a decision
        _open(req, applied)
        assert tid(req, "disposition-group").count() == 2 and tid(req, "disp-unhandled").count() == 0          # a requester may read, is offered no decision
        assert tid(req, "action-accept-result").count() == 0
        groups = tid(apv, "disposition-group")
        for i in range(2):
            groups.nth(i).get_by_test_id("disp-judged_unrelated").check()
        assert tid(apv, "action-accept-result").is_disabled()                                                    # a reason is required for "unrelated"
        groups.nth(0).get_by_test_id("disp-reason").fill("ok")
        assert tid(apv, "action-accept-result").is_disabled()                                                    # too short
        groups.nth(0).get_by_test_id("disp-reason").fill("a different control, owned by another team")
        groups.nth(1).get_by_test_id("disp-unhandled").check()                                                   # leave one unhandled on purpose
        expect(tid(apv, "narrow-scope-note")).to_contain_text("NOT be shown as completely done")
        expect(tid(apv, "action-accept-result")).to_be_enabled(timeout=WAIT)
        tid(apv, "action-accept-result").click()
        expect(tid(apv, "narrow-scope-banner")).to_be_visible(timeout=WAIT)
        expect(tid(apv, "state-outcome")).to_contain_text("NARROW scope")
        expect(tid(apv, "axis-result")).to_contain_text("NARROW")
        assert tid(apv, "recorded-disposition").count() == 2

        # ---- phase 7: the same states after a reload, from the server
        for p, key in ((req, "check"), (apv, "approval"), (rev, "check"), (adm, "apply")):
            p.reload()
            expect(tid(p, "journey")).to_be_visible(timeout=WAIT)
            _journey(p, key, "done")
        for p in (req, apv, rev, adm):
            assert not p._errors, p._errors
    finally:
        server.stop()
