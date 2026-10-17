"""UX1: the hand-over between people. A requester saves a request from a rule and confirms it THEMSELVES (nobody else has to), copies the link
with the button, a DIFFERENT account (a reviewer, separate browser context, not signed in yet) opens it, signs in, lands on the same request, can
read it but is not offered the confirmation, and sees the next step by itself when the requester confirms. Plus: clipboard fallback, missing/foreign context, link with no rights,
Business/Technical switch (no request, no lost form), inert generated text. Real server, real Chromium, stub provider."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft  # noqa: E402
from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tech_url, tid, UI_BASE# noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build, cand_id  # noqa: E402
from tests.pitch_brief.conftest import create_job, drain, job_body  # noqa: E402
from tests.pitch_brief.test_change_draft import Model  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _ctx(pw_browser, server, **kw):
    c = pw_browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900}, **kw)
    p = c.new_page()
    p.set_default_timeout(WAIT)
    p._errors = []
    p.on("pageerror", lambda e: p._errors.append(str(e)))
    return c, p


def _signed_in(pw_browser, server, env, who, **kw):
    c, p = _ctx(pw_browser, server, **kw)
    open_app(p, server.url)
    acct, cred = who
    login(p, acct, cred)
    select_workspace(p, env.ws)
    return c, p


def _account(env, role):
    from uuid import uuid4
    from infra.adapters.v2_identity import PostgresIdentityStore
    ids = PostgresIdentityStore(env.dsn)
    acct, cred = f"u-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, (role,))
    return acct, cred


def test_requester_confirms_their_own_request_and_a_colleague_follows_it_from_the_link(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        c1, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"), permissions=["clipboard-read", "clipboard-write"])
        c1.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
        # before anything is saved the copy button exists but is disabled, and says why
        goto_view(req, "Changes")
        if UI_BASE.startswith("/app-next"):      # the new page shows no copy button until something is saved, and says so instead of a disabled button
            expect(tid(req, "copy-request-link")).to_have_count(0)
        else:
            expect(tid(req, "copy-request-link")).to_be_disabled()
        if UI_BASE.startswith("/app-next"):      # nothing saved: the new page shows no request card at all
            expect(tid(req, "request-card")).to_have_count(0)
        else:
            expect(tid(req, "request-card")).to_contain_text("Nothing is saved yet")
        # start from a rule in the Explorer (nothing typed except the new value)
        goto_view(req, "Explorer")
        rule = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        req.locator(f"[data-node='{rule}']").click()
        tid(req, "request-change").click()
        expect(tid(req, "prefill-summary")).to_contain_text("1,000,000 JPY")
        tid(req, "sf-new").fill("500000")
        tid(req, "sf-unit").fill("net goods amount")
        tid(req, "sf-applies").fill("standard, emergency")
        tid(req, "action-parse-structured").click()
        expect(tid(req, "request-card")).to_have_attribute("data-saved", "true", timeout=WAIT)
        expect(tid(req, "request-summary")).to_contain_text("1,000,000 JPY")
        expect(tid(req, "request-summary")).to_contain_text("500,000 JPY")
        expect(tid(req, "request-status")).to_contain_text("waiting for the person who made it")
        expect(tid(req, "next-action")).to_contain_text("Confirm your request")
        # business view hides ids; technical view shows them (same data)
        if UI_BASE.startswith("/app-next"):      # the bridge runs in Technical: look at the Business view for this assertion, then go back
            req.locator("[data-testid=mode-business]").click()
        assert not req.locator("[data-testid=request-card] .techfield").first.is_visible()
        if UI_BASE.startswith("/app-next"):
            req.locator("[data-testid=mode-technical]").click()
        # copy
        tid(req, "copy-request-link").click()
        expect(tid(req, "copy-status")).to_contain_text("Copied")
        link = req.evaluate("navigator.clipboard.readText()")
        assert link == tid(req, "request-link").input_value()
        assert link.startswith(server.url + UI_BASE + "#/ws/" + env.ws + "/changes") and "intent=" in link
        for secret in ("password", "cookie", "token", "ripple_v2", "cred-"):
            assert secret not in link.lower()
        # a DIFFERENT, signed-out browser (a reviewer) opens the link: it asks to sign in, then returns to the SAME request, read-only
        acct, cred = _account(env, "reviewer")
        c2, adm = _ctx(pw_browser, server)
        adm.goto(link)
        expect(tid(adm, "login-context")).to_be_visible()
        adm.get_by_label("Account ID").fill(acct)
        adm.get_by_label("Credential").fill(cred)
        adm.get_by_role("button", name="Sign in").click()
        expect(tid(adm, "request-summary")).to_contain_text("500,000 JPY", timeout=WAIT)
        assert adm.url == link or adm.url.endswith(link.split(server.url)[1])      # same place, nothing re-created
        assert tid(adm, "action-confirm-intent").count() == 0                       # only the person who made it (or a confirmer) is offered it
        expect(tid(adm, "intent-owner-only")).to_contain_text(env.req)
        expect(tid(adm, "next-action")).to_contain_text(env.req)
        # the requester confirms their own request: no administrator is involved
        tid(req, "action-confirm-intent").click()
        expect(tid(req, "request-status")).to_contain_text("Confirmed", timeout=WAIT)
        expect(tid(req, "next-action")).to_contain_text("Ask for the AI draft")
        # the colleague, who never reloaded, sees the next step by itself (bounded polling)
        expect(tid(adm, "request-status")).to_contain_text("Confirmed", timeout=25000)
        for p in (req, adm):
            assert not p._errors, p._errors
    finally:
        server.stop()


def test_clipboard_refused_falls_back_to_a_selected_read_only_box(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        c, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        goto_view(req, "Explorer")
        rule = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        req.locator(f"[data-node='{rule}']").click()
        tid(req, "request-change").click()
        tid(req, "sf-new").fill("500000")
        tid(req, "sf-unit").fill("net goods amount")
        tid(req, "sf-applies").fill("standard")
        tid(req, "action-parse-structured").click()
        expect(tid(req, "request-card")).to_have_attribute("data-saved", "true", timeout=WAIT)
        req.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new Error('denied'))}, configurable: true})")
        tid(req, "copy-request-link").click()
        expect(tid(req, "copy-status")).to_contain_text("did not allow automatic copying")
        assert tid(req, "request-link").get_attribute("readonly") is not None
        assert req.evaluate("document.activeElement && document.activeElement.getAttribute('data-testid')") == "request-link"
        assert req.evaluate("window.getSelection().toString() || document.activeElement.value.substring(document.activeElement.selectionStart, document.activeElement.selectionEnd)").startswith(server.url)
    finally:
        server.stop()


def test_missing_and_foreign_context_show_a_clear_page_and_no_data(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        c, adm = _signed_in(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        adm.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent=intent-does-not-exist")
        expect(tid(adm, "link-unavailable")).to_be_visible(timeout=WAIT)
        expect(tid(adm, "link-unavailable")).to_contain_text("Nothing was changed")
        adm.goto("about:blank")
        adm.goto(f"{server.url}{UI_BASE}#/ws/ws-of-another-tenant/changes?intent=x")
        expect(tid(adm, "link-unavailable")).to_be_visible(timeout=WAIT)
        assert "Manager" not in adm.locator("main").inner_text() and "1,000,000" not in adm.locator("main").inner_text()
    finally:
        server.stop()


def test_view_switch_keeps_the_form_creates_nothing_and_both_views_show_the_same_values(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        c, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        goto_view(req, "Explorer")
        rule = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
        req.locator(f"[data-node='{rule}']").click()
        tid(req, "request-change").click()
        tid(req, "sf-new").fill("612345")                      # an unsaved form value
        calls = []
        req.on("request", lambda r: calls.append((r.method, r.url)) if "/v2/" in r.url else None)
        if UI_BASE.startswith("/app-next"):      # the bridge signs in with the Technical view (UI_MODE) because the legacy suites assert raw states
            req.locator("[data-testid=mode-business]").click()
        expect(req.locator("body")).to_have_attribute("data-mode", "business")
        req.locator("[data-testid=mode-technical]").click()
        expect(req.locator("body")).to_have_attribute("data-mode", "technical")
        assert tid(req, "sf-new").input_value() == "612345"    # nothing lost
        req.locator("[data-testid=mode-business]").click()
        assert tid(req, "sf-new").input_value() == "612345"
        assert calls == [], calls                              # no request of any kind: no job, no model call, not even a read
        # same values in both views: the saved request shows identical numbers
        tid(req, "sf-unit").fill("net goods amount")
        tid(req, "sf-applies").fill("standard")
        tid(req, "action-parse-structured").click()
        expect(tid(req, "request-summary")).to_contain_text("612,345 JPY", timeout=WAIT)
        business = tid(req, "request-summary").inner_text()
        req.locator("[data-testid=mode-technical]").click()
        assert tid(req, "request-summary").inner_text() == business
        expect(req.locator("[data-testid=request-card] .techfield").first).to_be_visible()
        assert ("intent id" if UI_BASE.startswith("/app-next") else "intent_id") in req.locator("[data-testid=request-card]").inner_text()
        assert not req.url.endswith("mode=technical") or "mode=technical" in req.url
    finally:
        server.stop()


def test_generated_text_with_html_stays_inert(env, ch, monkeypatch, pw_browser):
    def mutate(obj, prompt):
        obj["summary"] = 'Lower the limit <img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script> **bold**'
        obj["questions_for_human"] = ['Is <b>this</b> ok? <img src=y onerror="window.__pwned=3">']
    model = Model(mutate=mutate)
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    from tests.pitch_brief.chain import intent
    form = {"subject": "Manager approval", "scope": {"order_type": "standard"}, "unit": "net goods amount", "currency": "JPY",
            "old_value": "1000000", "new_value": "500000", "applies_to": ["standard"]}
    parsed = intent(env, ch.admin, form)
    jid, r = create_job(env.req_client(), env, "change.draft", {"intent_id": parsed["intent_id"], "extraction_job_ids": list(ch.jobs.values())})
    assert r.json()["status"] == "APPROVED"                                       # admitted by policy: the requester's own AI permission
    drain(env)
    assert job_body(ch.admin, env, jid)["execution_status"] == "COMPLETED"
    server = ServerThread().start()
    try:
        c, adm = _signed_in(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        adm.goto("about:blank")
        adm.goto(tech_url(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={parsed['intent_id']}&draft={jid}"))
        expect(tid(adm, "draft-summary")).to_be_visible(timeout=WAIT)
        adm.wait_for_timeout(500)
        assert adm.evaluate("window.__pwned") is None
        assert adm.locator("[data-testid=draft-summary] img, main img, main script").count() == 0
        assert "<img" in tid(adm, "draft-summary").inner_text()          # shown as text
    finally:
        server.stop()
