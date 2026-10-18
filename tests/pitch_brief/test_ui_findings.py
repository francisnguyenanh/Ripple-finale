"""The Questions page and the shared findings read model, through the browser. Real server, real Chromium, real routes/store/worker, STUB provider.
  * the same problem found by two check runs is ONE row; the page says how many repeats are not listed                                  -> test 1
  * the row says in business words what is unclear and what to do; a person without the right to resolve is not offered the box       -> test 1
  * a person with the right states, in their own words, where the rule applies; it is recorded and the row becomes RESOLVED            -> test 1"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import UI_BASE, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import APPROVAL, build  # noqa: E402
from tests.pitch_brief.test_current_findings import Y, _cand, _extract_unscoped, _finding_run  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def test_a_repeated_finding_is_one_row_and_a_person_with_the_right_states_where_the_rule_applies(env, ch, monkeypatch, pw_browser):
    ids = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))
    first = _finding_run(env, ch, ids)[0]["finding_id"]
    _finding_run(env, ch, ids)                                                         # the same checks again: another finding id for the same problem
    server = ServerThread().start()
    try:
        req = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        adm = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        for p in (req, adm):
            select_workspace(p, env.ws)
            p.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/questions?ui_section=findings")   # where findings are listed (D2: the page alone opens on Open questions)
        row = adm.locator(f"[data-testid=finding-row][data-finding-id='{first}']")
        expect(row).to_be_visible(timeout=WAIT)
        assert adm.locator("[data-testid=finding-row]").count() == 1                    # two runs, one problem
        expect(tid(adm, "questions-hidden-note")).to_contain_text("1 repeat")
        expect(row.get_by_test_id("finding-plain")).to_contain_text("could not tell which orders")
        expect(row.get_by_test_id("finding-todo")).to_contain_text("What to do")
        # a requester may read it but is not offered the box to decide it
        expect(req.locator("[data-testid=finding-row]").first).to_be_visible(timeout=WAIT)
        assert req.locator("[data-testid=gap-resolve]").count() == 0
        # the person with the right says where the rule applies: nothing is filled in for them
        assert adm.get_by_test_id("action-gap-resolve").is_disabled()
        adm.get_by_test_id("gap-scope-text").fill("Standard orders only.")
        adm.get_by_test_id("action-gap-resolve").click()
        expect(row).to_have_attribute("data-state", "RESOLVED", timeout=WAIT)
        for p in (req, adm):
            assert not p._errors, p._errors
    finally:
        server.stop()
