"""U3 in the browser (stub interpreter, SIMULATED; no provider call): Impact & Simulation opens on free text. Words -> the AI asks for the
missing value -> a preview with what the sources say now, the value, scope and what stays unchanged -> the person confirms -> only then Run
creates ONE deterministic simulation job. Nothing before Run creates a job; nothing mutates knowledge, sources or proposals."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import intent_interpret  # noqa: E402
from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import drain  # noqa: E402
from tests.pitch_brief.test_intent_chat import Interp, allow, kinds  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000
ASK = {"status": "CLARIFY", "message": "Which value should the manager approval threshold become?", "questions": ["Which value should the threshold become?"],
       "assumptions": [], "fields": {"rule": "r1", "value": None}}
READY = {"status": "READY", "message": "I will try the manager approval threshold at 500,000.", "questions": [], "assumptions": ["Emergency orders are not changed."],
         "fields": {"rule": "r1", "value": 500000}}
NOPE = {"status": "UNSUPPORTED", "message": "I cannot simulate the dress code. I can try the approval threshold, the CAPEX threshold, the price tolerance or payment terms.",
        "questions": [], "assumptions": [], "out_of_scope": ["dress code"], "fields": {"rule": None, "value": None}}


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


@pytest.fixture
def use(monkeypatch):
    def _use(model):
        monkeypatch.setattr(intent_interpret, "_client_for_model", lambda m: model)
        monkeypatch.setattr(intent_interpret, "_assert_global_endpoint", lambda: None)
        return model
    return _use


def _page(browser, server, env):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    open_app(page, server.url)
    login(page, env.req, f"cred-r-{env.nonce}")      # a requester asks; an administrator (a different person) allows the AI run
    select_workspace(page, env.ws)
    goto_view(page, "Impact & Simulation")
    expect(tid(page, "flow-whatif")).to_be_visible(timeout=WAIT)
    return page, ctx


def _allow_from_the_other_person(env, page, ch):
    cid = page.url.split("wi=")[1].split("&")[0]
    a = allow(env, ch.admin, cid, 3)
    assert a.status_code == 200, a.text
    return cid


def _decisions(env):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, env.ws))
        return cur.fetchone()[0]


def test_free_text_first_then_preview_then_confirm_then_one_deterministic_run(env, ch, use, pw_browser):
    model = use(Interp(ASK, READY))
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, env)
        expect(tid(page, "flow-whatif")).to_contain_text("What would you like to explore?")
        assert page.get_by_test_id("whatif-advanced").get_attribute("open") is None, "the rule/value form is an advanced path"
        expect(tid(page, "whatif-value")).not_to_be_visible()
        base = kinds(env)
        decisions = _decisions(env)
        tid(page, "wi-input").fill("what happens if we lower the manager approval threshold for standard orders, keeping emergency orders unchanged?")
        tid(page, "wi-start").click()
        expect(tid(page, "wi-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)      # asked under the person's own AI permission
        assert model.calls == 0, "the worker has not run yet"
        drain(env)
        expect(tid(page, "wi-understanding")).to_have_attribute("data-state", "CLARIFY", timeout=40000)
        expect(tid(page, "wi-question").first).to_contain_text("Which value")
        assert tid(page, "wi-confirm").count() == 0, "no value, no confirmation"
        assert kinds(env).get("simulation", 0) == base.get("simulation", 0), "asking creates no simulation"
        tid(page, "wi-reply").fill("500,000 JPY")
        tid(page, "wi-send").click()
        expect(tid(page, "wi-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)      # the reply job exists before the worker looks
        drain(env)
        expect(tid(page, "wi-understanding")).to_have_attribute("data-state", "READY", timeout=40000)
        expect(tid(page, "wi-field-rule")).to_contain_text("Manager approval threshold")
        cur = tid(page, "wi-field-current")
        expect(cur).to_contain_text("approval.yaml")
        expect(cur).to_contain_text("1,000,000")
        expect(cur).to_contain_text("not yet confirmed")
        expect(tid(page, "wi-field-value")).to_contain_text("500,000")
        expect(tid(page, "wi-field-scope")).to_contain_text("Standard orders")
        expect(tid(page, "wi-field-unchanged")).to_contain_text("Emergency orders")
        expect(tid(page, "wi-field-how")).to_contain_text("no AI computes the result")
        assert_business_clean(page, "Impact: what-if preview")
        assert kinds(env).get("simulation", 0) == base.get("simulation", 0), "a preview creates no simulation"
        if tid(page, "wi-ack").count():
            tid(page, "wi-ack").check()
        tid(page, "wi-confirm").click()
        expect(tid(page, "wi-outcome")).to_be_visible(timeout=WAIT)
        assert kinds(env).get("simulation", 0) == base.get("simulation", 0), "confirming the preview still runs nothing"
        tid(page, "wi-run").dblclick()
        expect(tid(page, "state-whatif")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
        drain(env)
        page.reload()
        expect(tid(page, "sim-report")).to_be_visible(timeout=WAIT)
        assert kinds(env).get("simulation", 0) == base.get("simulation", 0) + 1, "exactly one deterministic run"
        expect(tid(page, "whatif-ran")).to_contain_text("Manager approval threshold (JPY) set to 500,000")
        expect(tid(page, "whatif-explorer-link")).to_be_visible()
        after = kinds(env)
        for k in ("proposal", "review", "approval", "apply", "change.draft"):
            assert after.get(k, 0) == base.get(k, 0), k
        assert _decisions(env) == decisions, "a what-if never records knowledge"
        assert model.calls == 2
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_an_unsupported_request_is_not_mapped_to_another_rule(env, ch, use, pw_browser):
    use(Interp(NOPE))
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, env)
        base = kinds(env)
        tid(page, "wi-input").fill("what if the dress code for the finance desk were 5?")
        tid(page, "wi-start").click()
        expect(tid(page, "wi-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        drain(env)
        expect(tid(page, "wi-understanding")).to_have_attribute("data-state", "UNSUPPORTED", timeout=40000)
        assert tid(page, "wi-confirm").count() == 0
        expect(tid(page, "wi-understanding")).to_contain_text("dress code")
        assert kinds(env).get("simulation", 0) == base.get("simulation", 0)
        ctx.close()
    finally:
        server.stop()
