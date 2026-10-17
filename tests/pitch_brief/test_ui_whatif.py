"""Browser: the what-if control (any supported value on Harbor itself, no draft needed, no model call) and the Support page.
Baseline and proposed share code/input snapshots; a key the code does not read is shown as 'config does not control this
behaviour' with code evidence, and a zero-change edit of a key the code DOES read is shown as 'not inert'."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import harbor_sim  # noqa: E402
from tests.ai10ui.contract import goto_view, select_workspace, tech_url, tid, UI_BASE# noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import drain  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _try(page, env, server, label, value):
    page.goto("about:blank")
    page.goto(tech_url(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes"))
    expect(tid(page, "flow-whatif")).to_be_visible(timeout=WAIT)
    adv = page.get_by_test_id("whatif-advanced")        # the new page keeps the form under "Advanced input"
    if adv.count() and adv.get_attribute("open") is None:
        adv.locator("summary").click()
    tid(page, "whatif-key").select_option(label=label)
    tid(page, "whatif-value").fill(value)
    tid(page, "action-run-whatif").click()
    expect(tid(page, "state-whatif")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
    drain(env)
    page.reload()
    expect(tid(page, "flow-whatif")).to_be_visible(timeout=WAIT)


def test_whatif_with_other_values_and_the_support_page(env, ch, pw_browser, monkeypatch):
    monkeypatch.setenv("UI_MODE", "technical")      # this journey asserts raw paths and the pinned-code line, which exist only in the Technical view by design
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Changes")
        # a value that is NOT 500000: 750000 -> the registered expectation is computed from the shipped baseline, not from the adapter
        _try(page, env, server, "Manager approval threshold (JPY)", "750,000")
        expect(tid(page, "sim-report")).to_be_visible(timeout=WAIT)
        want = {b["request_id"] for b in harbor_sim_shipped_auto(750000)}
        got = {r.get_attribute("data-request") for r in page.locator("[data-testid=sim-changed] tbody tr").all()}
        assert got == want and want
        expect(tid(page, "sim-snapshot")).to_contain_text("same pinned Harbor code")
        expect(tid(page, "sim-not-controlled")).to_contain_text("does not control this behaviour" if UI_BASE.startswith("/app-next") else "Config does not control this behaviour")
        expect(tid(page, "sim-not-controlled")).to_contain_text("app/emergency.py")
        # a value the code reads but that crosses no row of the sample: NOT called inert
        _try(page, env, server, "Invoice price tolerance, percent", "99")
        expect(tid(page, "sim-no-change")).to_contain_text("the code does read this value" if UI_BASE.startswith("/app-next") else "not inert")
        assert page.locator("[data-testid=sim-inert]").count() == 0
        # a key no module reads: traced, with the code evidence
        _try(page, env, server, "Payment terms (days)", "45")
        expect(tid(page, "sim-inert")).to_contain_text("config does not control this behaviour")
        expect(tid(page, "sim-inert")).to_contain_text("app/calculations.py")
        # a bad value is refused by the server, nothing runs
        _try(page, env, server, "Manager approval threshold (JPY)", "0")
        expect(tid(page, "state-whatif")).to_have_attribute("data-state", "FAILED", timeout=WAIT)
        # Support page
        goto_view(page, "Support")
        expect(tid(page, "support-matrix")).to_be_visible()
        emerg = page.locator("[data-testid=support-row][data-row=emerg]")
        expect(emerg).to_contain_text("config does not control this")
        expect(emerg).to_contain_text("app/emergency.py")
        expect(emerg).to_contain_text("needs a code change (final product)" if UI_BASE.startswith("/app-next") else "needs code change (Final)")
        expect(page.locator("[data-testid=support-row][data-row=terms]")).to_contain_text("config does not control this")
        expect(tid(page, "support-isolation")).to_contain_text("NOT a security boundary")
        assert page.locator("[data-testid=support-trace]").count() >= 3
        assert not page._errors, page._errors
    finally:
        server.stop()


def harbor_sim_shipped_auto(new):
    import json
    reqs = {r["request_id"]: r for r in (json.loads(x) for x in (harbor_sim.FIXTURE / "data" / "requisitions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())}
    return [b for b in (json.loads(x) for x in (harbor_sim.FIXTURE / "run_output" / "routes.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())
            if b["route"] == "AUTO" and reqs[b["request_id"]]["process"] == "STANDARD" and reqs[b["request_id"]]["net_amount_jpy"] >= new]
