"""Browser: an invoice price-tolerance request (S-MATCH) and a payment-terms request (S-TERMS) reach a REAL Harbor before/after
table in the UI; the payment-terms key is shown as INERT with the code reference. Stub draft model, deterministic Harbor run."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft  # noqa: E402
from tests.ai10ui.contract import select_workspace, tech_url, tid, UI_BASE# noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build, intent  # noqa: E402
from tests.pitch_brief.conftest import create_job, drain, job_body  # noqa: E402
from tests.pitch_brief.test_change_draft import Model, _res  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000


def _view(page, mode):
    """Business/Technical switch of the new interface (the legacy page has none and always shows the raw text)."""
    if UI_BASE.startswith("/app-next"):
        tid(page, f"mode-{mode}").click()


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _draft(env, ch, monkeypatch, key, old, new, subject):
    def decide(e):
        if e["file_kind"] == "yaml" and key in e["text"]:
            return "edit", "this key holds the old value"
        return "exclude", "a different subject"

    def mutate(obj, prompt):
        obj["interpretation"] = {"old_value": old, "new_value": new, "scope": "standard", "currency": "JPY"}
    model = Model(decide=decide, mutate=mutate)
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    form = {"subject": subject, "scope": {"order_type": "standard"}, "unit": "JPY", "currency": "JPY",
            "old_value": str(old), "new_value": str(new), "applies_to": ["standard"]}
    parsed = intent(env, ch.admin, form)
    jid, r = create_job(env.req_client(), env, "change.draft", {"intent_id": parsed["intent_id"], "extraction_job_ids": list(ch.jobs.values())})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    drain(env)
    res = _res(job_body(ch.admin, env, jid))
    assert [e["proposal_edit"]["path"] for e in res["edits"]] == [key]
    return parsed["intent_id"], jid


def _run(page, env, server, intent_id, draft):
    page.goto("about:blank")
    page.goto(tech_url(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={draft}"))
    tid(page, "action-run-impact").click()
    expect(tid(page, "state-impact")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
    drain(env)
    page.reload()
    expect(tid(page, "state-impact")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
    tid(page, "action-run-simulation").click()
    expect(tid(page, "state-simulation")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
    drain(env)
    page.reload()
    expect(tid(page, "sim-report")).to_be_visible(timeout=WAIT)


def test_invoice_tolerance_and_payment_terms_in_the_browser(env, ch, monkeypatch, pw_browser):
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        iid, d = _draft(env, ch, monkeypatch, "matching_rules.price_tolerance_fixed_jpy", 10000, 20000, "Invoice price tolerance")
        _run(page, env, server, iid, d)
        expect(tid(page, "sim-summary")).to_contain_text("120 public invoices simulated")
        expect(tid(page, "sim-summary")).to_contain_text("all explained by the edited" if UI_BASE.startswith("/app-next") else "Changed: 1 (all explained by the edited keys)")
        row = page.locator("[data-testid=sim-changed] tbody tr[data-request=INV-000006]")
        # Business view (the default): the case in words (before -> after, and that the edited value caused it); no raw key or enum name
        _view(page, "business")
        expect(row).to_contain_text("HOLD PRICE")
        expect(row).to_contain_text("READY")
        expect(row).to_contain_text("the edited value")
        assert "price_tolerance_fixed_jpy" not in row.inner_text() and "HOLD_PRICE" not in row.inner_text()
        # Technical view: the raw outcome name and the configuration key that caused the change
        _view(page, "technical")
        expect(row).to_contain_text("HOLD_PRICE")
        expect(row).to_contain_text("price_tolerance_fixed_jpy")
        _view(page, "business")
        expect(tid(page, "sim-integrity")).to_contain_text("identical")
        # a payment-terms request: Harbor parses the key but the code hard-codes 30 days -> INERT, shown with the code line
        page2 = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page2, env.ws)
        iid2, d2 = _draft(env, ch, monkeypatch, "matching_rules.payment_term_days", 30, 45, "Payment terms")
        _run(page2, env, server, iid2, d2)
        _view(page2, "business")
        expect(tid(page2, "sim-inert")).to_contain_text("config does not control this behaviour")
        expect(tid(page2, "sim-inert")).to_contain_text("Edits the code does not read")
        expect(tid(page2, "sim-inert")).to_contain_text("hard-coded 30 days")
        assert "app/calculations.py" not in tid(page2, "sim-inert").inner_text()          # the code reference is a Technical detail
        _view(page2, "technical")
        expect(tid(page2, "sim-inert")).to_contain_text("app/calculations.py")           # ... and is shown there
        expect(tid(page2, "sim-inert")).to_contain_text("payment_term_days")             # with the raw key
        _view(page2, "business")
        expect(tid(page2, "sim-summary")).to_contain_text("0 change their outcome" if UI_BASE.startswith("/app-next") else "Changed: 0")
        for p in (page, page2):
            assert not p._errors, p._errors
    finally:
        server.stop()
