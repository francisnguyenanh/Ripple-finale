"""Browser, separate contexts per person: hand-over WITHOUT copying a link. Each person finds their own work under Overview -> My work,
opens it, and does only what their role allows: the approver states the check and decides, the reviewer writes notes (and a note that
asks for changes blocks approval until it is cleared), the requester prepares and applies. Real server, real Chromium, stub provider."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import UI_BASE, goto_view, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build  # noqa: E402
from tests.pitch_brief.conftest import create_job, drain, job_body  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import Y, chain_to_proposal  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _account, _signed_in  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _overview(page):
    goto_view(page, "Overview")
    expect(tid(page, "work-inbox")).to_be_visible(timeout=WAIT)


def _todo(page, stage):
    row = page.locator(f"[data-testid=work-todo][data-stage={stage}]")
    expect(row).to_have_count(1, timeout=WAIT)
    return row


def test_each_person_finds_their_work_opens_it_and_does_only_their_part(env, ch, monkeypatch, pw_browser):
    c = chain_to_proposal(env, ch, monkeypatch)                       # the requester's proposal exists (API setup; the UI part is the hand-over)
    P = c.proposal
    server = ServerThread().start()
    try:
        apv_acct = _account(env, "change_approver")
        rev_acct = _account(env, "reviewer")
        c1, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        c2, apv = _signed_in(pw_browser, server, env, apv_acct)
        c3, rev = _signed_in(pw_browser, server, env, rev_acct)
        c4, adm = _signed_in(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))

        # the requester: nothing for them yet; the page says WHO they are waiting for
        _overview(req)
        w = req.locator("[data-testid=work-waiting][data-stage=NEEDS_CHECK]")
        expect(w).to_have_count(1, timeout=WAIT)
        expect(w).to_contain_text("approver")
        assert tid(req, "work-todo").count() == 0
        # the administrator and the reviewer are not asked to do anything here
        _overview(adm)
        assert tid(adm, "work-todo").count() == 0                       # an administrator is not a default approver or reviewer
        _overview(rev)
        _todo(rev, "NEEDS_CHECK")                                       # a reviewer may review a finished proposal at once (optional)
        assert "Optional" in rev.locator("[data-testid=work-todo]").first.inner_text()

        # the approver finds the item in My work (no link was sent), opens it and states the check
        _overview(apv)
        row = _todo(apv, "NEEDS_CHECK")
        row.get_by_test_id("work-open-todo").click()
        expect(tid(apv, "suggested-check")).to_be_visible(timeout=WAIT)                 # nothing to type: the server built the check from the confirmed request
        expect(tid(apv, "suggested-check")).to_contain_text("should be JPY 500,000 in approval.yaml")
        expect(tid(apv, "check-limits")).to_contain_text("does not show that the change achieves")      # the mechanical limit is said out loud
        assert tid(apv, "field-contract-path").is_hidden()                                              # the manual form is under Advanced, closed
        expect(tid(apv, "action-confirm-checks")).to_be_enabled(timeout=WAIT)
        tid(apv, "action-confirm-checks").click()
        expect(tid(apv, "contract-row")).to_have_count(1, timeout=WAIT)
        expect(tid(apv, "contract-row")).to_contain_text("confirmed by")

        # review run + approval bundle: the requester's part (the page offers it from their own My work)
        req.reload()
        _overview(req)
        _todo(req, "RUN_REVIEW").get_by_test_id("work-open-todo").click()
        expect(tid(req, "action-run-review")).to_be_enabled(timeout=WAIT)
        assert tid(req, "action-confirm-checks").count() == 0 and tid(req, "action-set-contract").count() == 0      # the requester never confirms their own check
        assert tid(req, "action-approve").count() == 0
        assert tid(req, "action-apply").count() == 0 or tid(req, "action-apply").is_disabled()            # nothing is approved yet: apply is not available
        tid(req, "action-run-review").click()
        expect(tid(req, "state-review")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)      # the job exists; now the worker runs it
        drain(env)
        expect(tid(req, "state-review")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        tid(req, "action-prepare-approval").click()
        expect(tid(req, "state-approval")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        drain(env)
        expect(tid(req, "state-approval")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        assert tid(req, "action-approve").count() == 0                         # the requester is never offered the approval

        # the reviewer: optional item; writes a note that asks for changes; has no approve and no apply button
        rev.reload()
        _overview(rev)
        r = _todo(rev, "AWAITING_DECISION")
        expect(r).to_contain_text("Optional")
        r.get_by_test_id("work-open-todo").click()
        expect(tid(rev, "review-notes")).to_be_visible(timeout=WAIT)
        assert tid(rev, "action-approve").count() == 0 and tid(rev, "action-apply").count() == 0 and tid(rev, "action-set-contract").count() == 0 and tid(rev, "action-confirm-checks").count() == 0
        tid(rev, "review-note-text").fill("Please state the currency in the request.")
        tid(rev, "review-request-changes").click()
        expect(tid(rev, "review-open-request")).to_be_visible(timeout=WAIT)

        # the requester is told it is their turn (not the approver's)
        req.reload()
        _overview(req)
        link = _todo(req, "REVIEWER_CHANGES").get_by_test_id("work-open-todo").get_attribute("href")

        # the approver cannot approve while the request is open; the server refuses and the page says why
        apv.reload()
        _overview(apv)
        assert apv.locator("[data-testid=work-todo]").count() == 0                              # not theirs while changes are asked
        apv.goto(f"{server.url}{UI_BASE}{link}")                                                  # a link is only a convenience: the same page
        expect(tid(apv, "review-open-request")).to_be_visible(timeout=WAIT)
        # the page lists what the approver decides on and does NOT offer Approve while a reviewer's request is open (the server refuses it too)
        expect(tid(apv, "decision-basis")).to_have_attribute("data-complete", "false", timeout=WAIT)
        expect(tid(apv, "basis-notes")).to_have_attribute("data-ok", "false")
        expect(tid(apv, "basis-missing")).to_contain_text("a reviewer asked for changes")
        expect(tid(apv, "action-approve")).to_be_disabled()
        for k in ("basis-change", "basis-source", "basis-review", "basis-contract", "basis-quotes"):
            expect(tid(apv, k)).to_be_visible()
        expect(tid(apv, "basis-change")).to_contain_text("500000")
        expect(tid(apv, "basis-quotes")).to_contain_text("Hidden from your role")
        expect(tid(apv, "basis-contract")).to_contain_text("must say 500000")
        assert apv.get_by_text("Raw").count() == 0

        # the reviewer marks it ready: the approver may now decide, from their own page
        tid(rev, "review-note-text").fill("")
        tid(rev, "review-ready").click()
        expect(tid(rev, "review-open-request")).to_have_count(0, timeout=WAIT)
        apv.reload()
        expect(tid(apv, "review-open-request")).to_have_count(0, timeout=WAIT)
        expect(tid(apv, "decision-basis")).to_have_attribute("data-complete", "true", timeout=WAIT)
        expect(tid(apv, "action-approve")).to_be_enabled()                        # complete basis: Approve is there and active for the approver
        assert tid(req, "action-approve").count() == 0 and tid(rev, "action-approve").count() == 0      # and does not exist for the requester or the reviewer
        tid(apv, "field-decision-reason").fill("Checked the diff.")
        tid(apv, "action-approve").click()
        expect(tid(apv, "state-approval-decision")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)

        # the requester: ready to apply, from My work; only they hold the apply button
        req.goto(req.url.split("#")[0] + f"#/ws/{env.ws}/overview")
        _overview(req)
        _todo(req, "READY_TO_APPLY").get_by_test_id("work-open-todo").click()
        expect(tid(req, "action-apply")).to_be_enabled(timeout=WAIT)
        assert tid(apv, "action-apply").count() == 0
        for p in (req, apv, rev, adm):
            assert not p._errors, p._errors
    finally:
        server.stop()


def _job_ids(env, kind, proposal):
    with env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s AND payload->>'proposal_job_id'=%s ORDER BY job_id DESC",
                    (env.tenant, env.ws, kind, proposal))
        return [r[0] for r in cur.fetchall()]


def _approval_id(env, proposal):
    return _job_ids(env, "approval", proposal)[0]


def _review_id(env, proposal):
    return _job_ids(env, "review", proposal)[0]


def test_a_viewer_and_a_suspended_member_see_what_they_may_and_a_reason_for_the_rest(env, ch, monkeypatch, pw_browser):
    server = ServerThread().start()
    try:
        c1, viewer = _signed_in(pw_browser, server, env, _account(env, "viewer"))
        _overview(viewer)
        expect(tid(viewer, "work-empty")).to_be_visible(timeout=WAIT)
        goto_view(viewer, "Changes")
        expect(viewer.locator("main")).to_contain_text("cannot start a change", timeout=WAIT)
        adm = env.admin_client()
        assert adm.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": ["requester", "ai_suspended"]}).status_code == 200
        c2, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        goto_view(req, "Changes")
        tid(req, "chat-input").fill("please lower the manager approval limit to 500,000 yen")
        tid(req, "chat-start").click()
        expect(tid(req, "chat-ai-block")).to_have_attribute("data-reason", "suspended", timeout=WAIT)
        expect(tid(req, "chat-ai-block")).to_contain_text("switched off for your account")
        assert tid(req, "chat-allow").count() == 0                                   # no "ask" button when the reason is a switch, not a wait
        for p in (viewer, req):
            assert not p._errors, p._errors
    finally:
        server.stop()


def test_ai_use_switched_off_while_the_page_is_open_is_shown_and_refused_by_the_server(env, ch, monkeypatch, pw_browser):
    """The page was opened while AI use was allowed. An administrator switches it off meanwhile. The next message is stored but NOT sent to the AI:
    the server decides, and the page says why (switched off for the account), with no way to ask again from this page."""
    from infra.worker import intent_interpret
    from tests.pitch_brief.test_intent_chat import READY, Interp
    model = Interp(READY)
    monkeypatch.setattr(intent_interpret, "_client_for_model", lambda m: model)
    monkeypatch.setattr(intent_interpret, "_assert_global_endpoint", lambda: None)
    server = ServerThread().start()
    try:
        c1, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        goto_view(req, "Changes")
        expect(tid(req, "chat-panel")).to_be_visible(timeout=WAIT)
        assert env.admin_client().post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": ["requester", "ai_suspended"]}).status_code == 200
        tid(req, "chat-input").fill("please lower the manager approval limit to 500,000 yen")
        tid(req, "chat-start").click()
        expect(tid(req, "chat-ai-block")).to_have_attribute("data-reason", "suspended", timeout=WAIT)
        assert model.calls == 0 and tid(req, "chat-allow").count() == 0
        # AI use switched on again: the page offers "Ask the AI" after it reads the permissions again (focus/refusal), no new sign-in
        assert env.admin_client().post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": ["requester"]}).status_code == 200
        req.evaluate("window.dispatchEvent(new Event('focus'))")
        req.reload()
        expect(tid(req, "chat-allow")).to_be_enabled(timeout=WAIT)
        assert not req._errors, req._errors
    finally:
        server.stop()


def test_draft_citation_text_is_hidden_from_a_non_reader_in_both_views_and_shown_to_a_reader(env, ch, monkeypatch, pw_browser):
    """The server removes the verbatim source text from the draft for a role without evidence.read (not CSS): business AND technical view show the
    note and the file/location, never the quote; the administrator (a reader) still sees it."""
    from tests.ai10ui.contract import tech_url
    from tests.pitch_brief.test_change_draft import Model, run
    jid, body = run(env, ch, monkeypatch, Model())
    iid = body["execution_result"]["intent"]["intent_id"]
    server = ServerThread().start()
    try:
        url = f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={iid}&draft={jid}"
        c1, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        c2, adm = _signed_in(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        for page, reader in ((req, False), (adm, True)):
            for u in (url, tech_url(url)):
                page.goto("about:blank")
                page.goto(u)
                expect(tid(page, "draft-status")).to_be_visible(timeout=WAIT)
                if reader:
                    expect(tid(page, "draft-citation").first).to_be_visible()
                    assert tid(page, "draft-citation-hidden").count() == 0
                else:
                    expect(tid(page, "draft-citation-hidden").first).to_be_visible()
                    assert tid(page, "draft-citation").count() == 0
                    assert "routing_rules.standard_manager_threshold_jpy: 1000000" not in page.locator("main").inner_text()
        for p in (req, adm):
            assert not p._errors, p._errors
    finally:
        server.stop()
