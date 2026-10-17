"""A stage that renders nothing before a request exists must render the SAME number of hooks once it does: saving a request from the Advanced form must not raise a React error
(live regression 2026-10-09: two hooks sat below an early return and the page went blank with React error #310)."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def test_saving_a_request_does_not_break_the_page(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Changes")
        tid(page, "advanced-input").locator("summary").click()
        for t, v in (("sf-subject", "Manager approval limit"), ("sf-scope-key", "order_type"), ("sf-scope-val", "standard"), ("sf-unit", "net goods amount"), ("sf-currency", "JPY"),
                     ("sf-old", "1,000,000"), ("sf-new", "500,000"), ("sf-applies", "standard, emergency")):
            tid(page, t).fill(v)
        tid(page, "action-parse-structured").click()
        expect(tid(page, "state-intent")).to_have_attribute("data-state", "DRAFT", timeout=30000)
        tid(page, "action-confirm-intent").click()
        expect(tid(page, "state-intent")).to_have_attribute("data-state", "CONFIRMED", timeout=30000)     # the draft stage, the coverage panel and the analysis stage appear too
        expect(tid(page, "coverage-panel")).to_be_visible(timeout=30000)
        assert page._errors == [], page._errors
    finally:
        server.stop()
