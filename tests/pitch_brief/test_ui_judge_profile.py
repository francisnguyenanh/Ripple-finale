"""Judge profile on the real UI (Chromium): the page shows no import, analysis, review, approval, apply or result-decision action to a judge, says whose turn it is, and the Changes page for a
proposal the judge can read ends at "waiting". Self-check on the offline stack (stub providers); NOT a run on the deployed candidate."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _account, _signed_in  # noqa: E402

FORBIDDEN = ("action-run-review", "action-prepare-approval", "action-approve", "action-apply", "action-accept-result", "action-confirm-checks",
             "action-retry-coverage", "action-set-contract")


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_a_judge_sees_no_action_beyond_the_proposal_and_is_told_who_is_next(env, ch, monkeypatch, pw_browser):
    c = chain_to_proposal(env, ch, monkeypatch)
    server = ServerThread().start()
    try:
        _, jp = _signed_in(pw_browser, server, env, _account(env, "judge"))
        base = f"{server.url}{UI_BASE}#/ws/{env.ws}"
        jp.goto("about:blank")
        jp.goto(f"{base}/changes?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}")
        expect(tid(jp, "journey")).to_be_visible(timeout=WAIT)
        expect(tid(jp, "flow-proposal")).to_be_visible(timeout=WAIT)
        for name in FORBIDDEN:
            assert tid(jp, name).count() == 0, name                                              # not offered, not merely disabled
        expect(tid(jp, "journey").locator("li[data-step=review]")).to_have_attribute("data-mine", "false")
        expect(tid(jp, "journey").locator("li[data-step=check]")).to_have_attribute("data-mine", "false")
        # Sources: reading and the stored results stay, import and re-analysis do not
        jp.goto("about:blank")
        jp.goto(f"{base}/sources")
        expect(jp.locator("h1, h2").first).to_be_visible(timeout=WAIT)
        assert tid(jp, "import-panel").count() == 0
        # Members / operator controls are not offered either: the server refused them already, the page does not suggest them
        jp.goto("about:blank")
        jp.goto(f"{base}/overview")
        expect(tid(jp, "work-inbox")).to_be_visible(timeout=WAIT)
    finally:
        server.stop()
