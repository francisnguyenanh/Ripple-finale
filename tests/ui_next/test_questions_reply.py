"""U4 in the browser (stub interpreter, SIMULATED): a person answers a question of the work queue in their own words; Ripple shows how it will
be recorded (comment or decision, fact or wish, which rule); an answer alone is never knowledge; a decision is recorded only when the
person presses the last button, with their name; a wish is only ever a comment and points to a change request."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import intent_interpret  # noqa: E402
from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import drain  # noqa: E402
from tests.pitch_brief.test_free_text_purposes import _decisions, _second_admin  # noqa: E402
from tests.pitch_brief.test_intent_chat import Interp, allow, kinds  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000


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


def _open(browser, server, env):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    goto_view(page, "Questions")
    first = tid(page, "queue-item").first
    expect(first).to_be_visible(timeout=WAIT)
    first.locator("button[aria-expanded]").first.click()
    first.get_by_test_id("qr-details").locator("summary").click()
    return page, ctx, first


def _cid(page):
    m = re.search(r"(chat-[0-9a-f]+)", page.url)
    assert m, page.url
    return m.group(1)


def test_an_answer_becomes_a_preview_then_a_decision_only_when_the_person_presses_the_last_button(env, ch, use, pw_browser):
    ready = {"status": "READY", "message": "I will record a decision.", "questions": [], "assumptions": [],
             "fields": {"answer": "Yes, that is the manager limit today.", "kind": "decision", "claim": "c1", "wish_or_fact": "fact", "rationale": "Checked against the policy.",
                        "asks_source_change": False, "quote": "Yes, that is the manager limit today."}}
    model = use(Interp(ready))
    server = ServerThread().start()
    try:
        page, ctx, item = _open(pw_browser, server, env)
        expect(item).to_contain_text("why you are confirming this")
        expect(item).to_contain_text("It is not knowledge until a person with authority confirms a rule")
        dec0 = _decisions(env)
        item.get_by_test_id("qr-input").fill("Yes, that is the manager limit today.")
        item.get_by_test_id("qr-start").click()
        expect(item.get_by_test_id("qr-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)      # the AI replies under the person's own permission
        assert model.calls == 0
        drain(env)
        u = item.get_by_test_id("qr-understanding")
        expect(u).to_have_attribute("data-state", "READY", timeout=40000)
        expect(item.get_by_test_id("qr-field-kind")).to_contain_text("A decision with your name that confirms one rule")
        expect(item.get_by_test_id("qr-field-claim")).to_contain_text("=")
        expect(item.get_by_test_id("qr-field-wish")).to_contain_text("A fact")
        expect(item.get_by_test_id("qr-field-why")).to_contain_text("Checked against the policy")
        assert _decisions(env) == dec0, "a preview records nothing"
        assert_business_clean(page, "Questions: reply preview")
        base = kinds(env)                 # the interpreter job of the chat already exists; nothing may be added from here on
        if item.get_by_test_id("qr-ack").count():
            item.get_by_test_id("qr-ack").check()
        item.get_by_test_id("qr-confirm").click()
        expect(item.get_by_test_id("qr-outcome")).to_be_visible(timeout=WAIT)
        assert _decisions(env) == dec0 and kinds(env) == base, "agreeing on the preview records nothing yet"
        item.get_by_test_id("qr-run").dblclick()
        expect(item.get_by_test_id("qr-outcome")).to_have_attribute("data-done", "true", timeout=WAIT)
        assert _decisions(env) == dec0 + 1, "exactly one decision, made by the person who pressed the button"
        with env.store._tx() as cur:
            cur.execute("SELECT actor_id, action FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY epoch DESC LIMIT 1", (env.tenant, env.ws))
            assert cur.fetchone() == (env.admin, "confirm")
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_a_wish_is_saved_as_a_comment_and_points_to_a_change_request(env, ch, use, pw_browser):
    wish = {"status": "READY", "message": "x", "questions": [], "assumptions": [],
            "fields": {"answer": "Managers should be able to approve more.", "kind": "decision", "claim": "c1", "wish_or_fact": "wish", "rationale": None,
                       "asks_source_change": True, "quote": "Managers should be able to approve more."}}
    use(Interp(wish))
    server = ServerThread().start()
    try:
        page, ctx, item = _open(pw_browser, server, env)
        dec0 = _decisions(env)
        item.get_by_test_id("qr-input").fill("Managers should be able to approve more.")
        item.get_by_test_id("qr-start").click()
        expect(item.get_by_test_id("qr-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        drain(env)
        expect(item.get_by_test_id("qr-understanding")).to_have_attribute("data-state", "READY", timeout=40000)
        expect(item.get_by_test_id("qr-field-kind")).to_contain_text("A comment")
        expect(item.get_by_test_id("qr-field-forced")).to_contain_text("cannot be confirmed as knowledge")
        expect(item.get_by_test_id("qr-field-handoff")).to_contain_text("change request")
        assert item.get_by_test_id("qr-field-claim").count() == 0
        if item.get_by_test_id("qr-ack").count():
            item.get_by_test_id("qr-ack").check()
        item.get_by_test_id("qr-confirm").click()
        expect(item.get_by_test_id("qr-handoff")).to_be_visible(timeout=WAIT)
        item.get_by_test_id("qr-run").click()
        expect(item.get_by_test_id("qr-outcome")).to_have_attribute("data-done", "true", timeout=WAIT)
        assert _decisions(env) == dec0, "a wish never becomes a decision"
        ctx.close()
    finally:
        server.stop()
