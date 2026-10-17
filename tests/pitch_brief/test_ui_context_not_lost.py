"""Context is not lost: a page opened without the full link (role change, re-login, the sidebar, a shortened link, back/forward) shows a chooser or finds its own stages from the server;
nobody types an id; nothing is widened (the chooser is the server's per-account list, the chain names job ids only). Real server, real Chromium, stub providers."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import _to_approval, chain_to_proposal  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _account, _signed_in  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_without_the_full_link_the_page_offers_the_persons_own_changes_and_finds_the_chain(env, ch, monkeypatch, pw_browser):
    c = chain_to_proposal(env, ch, monkeypatch)
    _to_approval(env, ch, c)
    server = ServerThread().start()
    try:
        apv_acct = _account(env, "change_approver")
        _, apv = _signed_in(pw_browser, server, env, apv_acct)
        _, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        bare = f"{server.url}{UI_BASE}#/ws/{env.ws}/changes"
        for page in (apv, req):
            page.goto("about:blank")
            page.goto(bare)                                                                          # no ids at all: not a trap, not a blank "not started" page
            expect(tid(page, "changes-chooser")).to_be_visible(timeout=WAIT)
            expect(tid(page, "changes-none-selected")).to_contain_text("No change is selected")
            expect(tid(page, "work-inbox")).to_be_visible(timeout=WAIT)
        # the approver is offered the approval; opening it lands on the right request with its permitted stage, no id typed
        link = tid(apv, "work-open-todo").first
        expect(link).to_be_visible(timeout=WAIT)
        link.click()
        expect(apv).to_have_url(re.compile(r"proposal=.*approval=|approval=.*proposal="), timeout=WAIT)
        expect(tid(apv, "changes-chooser")).to_have_count(0)
        expect(tid(apv, "flow-approval")).to_be_visible(timeout=WAIT)
        assert "Not started" not in tid(apv, "flow-approval").inner_text() or tid(apv, "action-approve").count() == 1
        # back returns to the chooser (the address is the truth; the page does not keep showing the other change)
        apv.go_back()
        expect(tid(apv, "changes-chooser")).to_be_visible(timeout=WAIT)
        # a shortened link with only the proposal: the missing review / approval ids are filled in from the server for the same proposal, and only those
        part = f"{bare}?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}"
        req.goto("about:blank")
        req.goto(part)
        expect(req).to_have_url(re.compile(r"review=.*approval=|approval=.*review="), timeout=WAIT)
        expect(tid(req, "state-approval")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)      # not "Not started. Needs a proposal first"
        # an id of another workspace is not widened into anything: the page says so and the chooser/own list is still reachable
        req.goto("about:blank")
        req.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?proposal=job-of-nobody")
        expect(tid(req, "link-unavailable")).to_be_visible(timeout=WAIT)
        assert "job-of-nobody" not in (tid(req, "link-unavailable").inner_text().replace("Go to Changes", ""))
    finally:
        server.stop()
