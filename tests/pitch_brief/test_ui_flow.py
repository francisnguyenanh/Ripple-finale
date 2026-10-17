"""UI: request/confirmed intent -> GenAI draft + citations -> generated proposal (nothing retyped) -> [contract, review,
approval, apply through the existing API/UI] -> loop status -> query on the new revision. Real server (uvicorn thread),
real Chromium, real routes/store/worker; STUB providers only (wiring evidence, not live GenAI evidence). Roles are real:
requester, admin (confirms intent, approves model calls), change_approver (contract author + decision) -- SIMULATED
accounts, not human acceptance."""
from __future__ import annotations

import re

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, extraction, gap_answer  # noqa: E402
from tests.ai10ui.contract import login, open_app, select_workspace, goto_view, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build  # noqa: E402
from tests.pitch_brief.conftest import create_job, drain, job_body, checks_ready, LiveWorker  # noqa: E402
from tests.pitch_brief.test_change_draft import Model  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import Y, post_contract  # noqa: E402
from tests.pitch_brief.test_loop_feedback import GapStub, PostApplyExtraction  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _account(env, role):
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.pitch_brief.conftest import DSN
    ids = PostgresIdentityStore(DSN)
    acct, cred = f"{role[:3]}-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, (role,))
    return acct, cred


def _page(world_ctx, server, who):
    ctx = world_ctx.new_context(base_url=server.url, viewport={"width": 1280, "height": 1000})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page._errors = errors
    open_app(page, server.url)
    login(page, who[0], who[1])
    return page


def _click_then_pump(env, page, button, state_name, final="COMPLETED"):
    """Click, wait until the job exists (the UI shows it), run the worker, wait for the final state."""
    tid(page, button).click()
    expect(tid(page, "state-" + state_name)).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
    drain(env)
    expect(tid(page, "state-" + state_name)).to_have_attribute("data-state", final, timeout=WAIT)


def _expand(page, stage):
    """A finished stage is collapsed: open it the way a person does."""
    card = tid(page, stage)
    expect(card).to_be_visible(timeout=WAIT)
    if card.get_attribute("data-open") != "true":
        tid(page, stage + "-toggle").click()


def _open(page, url):
    page.goto("about:blank")
    page.goto(url)


def test_ui_request_to_draft_to_proposal_to_loop_status_and_query(env, ch, monkeypatch, pw_browser):
    model = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    server = ServerThread().start()
    try:
        requester = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        approver_acct = _account(env, "change_approver")
        approver_page = None
        for p in (requester, admin):
            select_workspace(p, env.ws)
            goto_view(p, "Changes")
        tid(requester, "advanced-input").locator("summary").click()      # the form is the Advanced input path now
        # ---- 1. structured request (exact values kept)
        for tidname, val in (("sf-subject", "Manager approval limit"), ("sf-scope-key", "order_type"),
                             ("sf-scope-val", "standard"), ("sf-unit", "net goods amount"), ("sf-currency", "JPY"),
                             ("sf-old", "1,000,000"), ("sf-new", "500,000"), ("sf-applies", "standard, emergency")):
            tid(requester, tidname).fill(val)
        tid(requester, "action-parse-structured").click()
        expect(tid(requester, "state-intent")).to_have_attribute("data-state", "DRAFT", timeout=WAIT)
        url = requester.url
        # ---- 2. the requester confirms their OWN request (no administrator involved)
        assert tid(requester, "action-confirm-intent").count() == 1
        tid(requester, "action-confirm-intent").click()
        expect(tid(requester, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=WAIT)
        # the finding stage (operator/API step): a real finding job whose scope_mismatch must reach the draft and the UI
        from tests.pitch_brief.chain import POLICY, FINGUIDE, cand_id
        fj, rf = create_job(env.req_client(), env, "finding", {"candidate_ids": [
            cand_id(ch, POLICY, "capex_finance_review_threshold"), cand_id(ch, FINGUIDE, "capex_finance_review_threshold")]})
        assert rf.status_code == 200, rf.text
        admin_api = env.admin_client()
        assert admin_api.post(f"/v2/jobs/{fj}/approve", json={"workspace_id": env.ws}).status_code == 200
        drain(env)
        # ---- 3. GenAI draft: the requester starts it under their own AI permission; nobody has to approve the model call; the worker runs it
        checks_ready(env)
        tid(requester, "action-run-draft").click()
        expect(tid(requester, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)      # admitted by policy, waiting for the worker
        url = requester.url
        assert tid(requester, "action-approve-draft").count() == 0       # no approve button for anyone: there is nothing to approve
        drain(env)
        _open(requester, url)
        expect(tid(requester, "draft-status")).to_have_attribute("data-state", "PARTIAL", timeout=WAIT)
        assert model.calls == 1
        assert tid(requester, "draft-item").count() >= 3
        assert tid(requester, "draft-citation").count() == 0 and tid(requester, "draft-citation-hidden").count() >= 3      # the requester has no evidence.read: the cited text is hidden, the file/location stay
        assert tid(requester, "draft-not-covered").count() >= 1
        assert tid(requester, "draft-open-item").count() >= 1               # C02-style scope_mismatch finding shown, unchanged             # emergency / python limits are visible
        body = requester.locator("body").inner_text()
        assert "AI-assisted draft" in body and "nothing is applied" in body.lower()
        # ---- 4. impact + simulation + proposal generated from the draft (no path/current/new typed)
        _click_then_pump(env, requester, "action-run-impact", "impact")
        _click_then_pump(env, requester, "action-run-simulation", "simulation")
        # R2/R3/R4 in the browser: the simulation was built by the server from the draft and says which direction it tested;
        # the AI reading is only OFFERED (no model call happened); a draft can be revised with confirmed feedback.
        expect(tid(requester, "sim-direction")).to_contain_text("requested change", timeout=WAIT)
        expect(tid(requester, "sim-analysis")).to_have_attribute("data-state", "none", timeout=WAIT)
        expect(tid(requester, "sim-analysis-request")).to_contain_text("uses the paid model")
        assert tid(requester, "action-revise-draft").count() == 1 and tid(requester, "draft-superseded").count() == 0
        assert model.calls == 1, "viewing the simulation or the AI-reading offer made no provider call"
        assert tid(requester, "field-proposal-path").count() == 0
        _click_then_pump(env, requester, "action-prepare-proposal-from-draft", "proposal")
        expect(tid(requester, "left-for-humans").first).to_be_visible(timeout=WAIT)
        url = requester.url
        proposal_id = tid(requester, "state-proposal").get_attribute("data-job-id")
        # ---- 5. contract -> review -> approval -> decision -> apply (existing product path, through the API)
        from fastapi.testclient import TestClient
        from infra import judge

        def client(acct, cred):
            c = TestClient(judge.app, base_url="https://judge.test")
            c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
            assert c.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
            return c
        op = client(*approver_acct)
        rc = post_contract(env, op, proposal_id, 500000)
        assert rc.status_code == 200, rc.text
        req = env.req_client()
        rj, r = create_job(req, env, "review", {"proposal_job_id": proposal_id, "verification_contract_id": rc.json()["contract_id"]})
        assert r.status_code == 200
        drain(env)
        aj, r = create_job(req, env, "approval", {"proposal_job_id": proposal_id, "review_job_id": rj})
        assert r.status_code == 200
        drain(env)
        appr = job_body(op, env, aj)["execution_result"]
        assert op.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "ok",
                                                       "expected_approval_digest": appr["approval_digest"]}).status_code == 200
        post = PostApplyExtraction()
        monkeypatch.setattr(extraction, "_client_for_model", lambda m: post)
        apj, r = create_job(req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
        assert r.status_code == 200
        drain(env)
        drain(env)
        # ---- 6. loop status in the UI: PARTIAL, residual sources listed, re-extraction counted
        sep = "&" if "?" in url else "?"
        _open(requester, f"{url}{sep}review={rj}&approval={aj}&apply={apj}")
        expect(tid(requester, "loop-status")).to_have_attribute("data-state", "PARTIAL", timeout=WAIT)
        assert tid(requester, "loop-residual").count() >= 1
        stages = tid(requester, "loop-stages").inner_text()
        assert re.search(r"files re-read (\d+) of \1", stages) or re.search(r"(\d+)/\1", stages), stages          # every file that had to be read again was; the count includes the relation/finding jobs the checks behind the draft created
        assert tid(requester, "state-draft").count() == 1
        assert tid(requester, "loop-supported-state").get_attribute("data-state") in ("PASSED", "PENDING")      # supported edit apart from the request status
        assert "not the whole request" in tid(requester, "loop-supported").inner_text()
        # an explanation written for the approval describes the state BEFORE the apply: after the head moved it is shown as an older revision
        _expand(requester, "flow-approval")
        expect(tid(requester, "ai-explanation-historical")).to_be_visible(timeout=WAIT)
        # ---- 7. query on the new revision (a model call under the requester's own AI permission)
        gap = GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000")
        monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: gap)
        tid(requester, "field-query").fill("What is the Manager approval limit for standard orders?")
        expect(tid(requester, "query-scope-plan")).to_contain_text("all ")          # no file to pick: every current file is searched by default
        tid(requester, "action-run-query").click()
        expect(tid(requester, "state-query")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        qurl = requester.url
        drain(env)
        _open(requester, qurl)
        expect(tid(requester, "query-status")).to_have_attribute("data-state", "answered", timeout=WAIT)
        assert "500000" in tid(requester, "query-answer").inner_text()
        assert tid(requester, "query-citation").count() == 1 and tid(requester, "citation-hidden").count() == 1       # the span is listed (file, revision); its verbatim text is hidden for this role
        assert "routing_rules.standard_manager_threshold_jpy: 500000" not in tid(requester, "query-citation").inner_text()
        # ---- 8. feedback continues the cycle: revise the draft; the lineage is read from the server, so it survives a reload and another browser
        _open(requester, f"{url}{sep}review={rj}&approval={aj}&apply={apj}")
        old_url = requester.url
        _expand(requester, "flow-draft")
        # the apply moved the revision and the changed file was read again: the system itself created the checks (relation, finding) for the NEW readings; nobody was asked
        drain(env)                                                                                              # the worker runs them
        requester.reload()
        _expand(requester, "flow-draft")
        expect(tid(requester, "coverage-finding")).to_have_attribute("data-state", "done", timeout=WAIT)       # said, and still said after a refresh
        expect(tid(requester, "coverage-relation")).to_have_attribute("data-state", "done")
        calls_before = model.calls
        live = LiveWorker(env).start()
        try:
            tid(requester, "action-revise-draft").click()
            expect(tid(requester, "coverage-finding")).to_have_attribute("data-state", "done", timeout=60000)
            expect(tid(requester, "coverage-relation")).to_have_attribute("data-state", "done")
            requester.wait_for_function("(u) => location.href !== u", arg=old_url, timeout=WAIT)                 # the page moved to a NEW draft job
            new_url = requester.url
            assert new_url != old_url
            new_draft = next(v for k, v in (kv.split("=", 1) for kv in new_url.split("?", 1)[1].split("&")) if k == "draft")
            assert job_body(ch.admin, env, new_draft)["approved_by"] == "policy:ai.use"                           # admitted by policy: nobody allowed it, nobody was asked
            assert tid(requester, "action-approve-draft").count() == 0 and model.calls in (calls_before, calls_before + 1)                  # the page's own worker may already have run it
            _open(admin, new_url)
            _expand(admin, "flow-draft")
            expect(tid(admin, "draft-revises")).to_be_visible(timeout=WAIT)
            _open(admin, old_url)
            _expand(admin, "flow-draft")
            expect(tid(admin, "draft-superseded")).to_be_visible(timeout=WAIT)
            assert tid(admin, "action-revise-draft").count() == 0
        finally:
            live.stop.set()
        for p in (requester, admin):
            assert not p._errors, p._errors
    finally:
        server.stop()


def test_ui_asks_which_rule_when_several_match_and_never_takes_the_first(env, ch, monkeypatch, pw_browser):
    """R2 in the browser with REAL data: a second extraction of the same config file states a second, different rule about the same value
    (kept apart by the R1 signature). The real draft then carries two candidates for its edit; the impact analysis stays disabled until the
    person picks one, and the pick is what the request carries."""
    from tests.pitch_brief.chain import APPROVAL as APPROVAL_PATH, StubClient
    second = StubClient([("routing_rules.standard_manager_threshold_jpy", [{
        "subject": "Manager approval", "predicate": "manager_limit_restated_with_outcome", "value": 1000000, "unit": "JPY", "operator": ">=",
        "modality": "must", "scope": {"order_type": "standard"}, "exceptions": [], "outcome": "NEEDS_MANAGER",
        "quotes": ["routing_rules.standard_manager_threshold_jpy: 1000000"], "rationale": "second statement of the same value"}])])
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: second)
    xj, rx = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL_PATH], "source_role": "unknown"})
    assert rx.status_code == 200, rx.text
    assert ch.admin.post(f"/v2/jobs/{xj}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    assert job_body(ch.admin, env, xj)["execution_status"] == "COMPLETED"
    model = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    server = ServerThread().start()
    try:
        requester = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        for p in (requester, admin):
            select_workspace(p, env.ws)
            goto_view(p, "Changes")
        tid(requester, "advanced-input").locator("summary").click()
        for tidname, val in (("sf-subject", "Manager approval limit"), ("sf-scope-key", "order_type"), ("sf-scope-val", "standard"),
                             ("sf-unit", "net goods amount"), ("sf-currency", "JPY"), ("sf-old", "1,000,000"), ("sf-new", "500,000"),
                             ("sf-applies", "standard, emergency")):
            tid(requester, tidname).fill(val)
        tid(requester, "action-parse-structured").click()
        expect(tid(requester, "state-intent")).to_have_attribute("data-state", "DRAFT", timeout=WAIT)
        url = requester.url
        tid(requester, "action-confirm-intent").click()
        expect(tid(requester, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=WAIT)
        checks_ready(env)
        tid(requester, "action-run-draft").click()
        expect(tid(requester, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        url = requester.url
        drain(env)
        seen = []
        requester.on("request", lambda r: seen.append(r.url) if "impact-context" in r.url else None)
        _open(requester, url)
        expect(tid(requester, "draft-status")).to_be_visible(timeout=WAIT)
        _expand(requester, "flow-analysis")
        picker = tid(requester, "impact-target-picker")
        expect(picker).to_be_visible(timeout=WAIT)
        values = [o.get_attribute("value") for o in picker.locator("option").all() if o.get_attribute("value")]
        assert len(values) == 2 and all(v.startswith("cand-") for v in values), values        # two REAL candidates from the store
        assert tid(requester, "action-run-impact").is_disabled() and tid(requester, "action-run-relations").is_disabled()
        assert not seen, "nothing was looked up for a guessed first rule"
        picker.locator("select").select_option(value=values[1])                                  # the SECOND rule
        assert tid(requester, "action-run-relations").is_enabled()
        tid(requester, "action-run-relations").click()
        requester.wait_for_timeout(1500)
        assert any(values[1] in u for u in seen) and not any(values[0] in u for u in seen), seen
        for p in (requester, admin):
            assert not p._errors, p._errors
    finally:
        server.stop()
