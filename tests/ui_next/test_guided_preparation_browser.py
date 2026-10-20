"""EUI-08 guided preparation in the browser (feature switched ON for this test only; it is off by default): the manifest shows on the proposal stage of a
confirmed request with a completed draft, nothing is ticked for the person, Start waits for their explicit choice, the run steps impact -> simulation ->
proposal as that person, and the ordinary proposal stage then shows the prepared proposal. Synthetic draft model; deterministic jobs; real Chromium."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_qry04_browser import sign_in  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker  # noqa: E402
from tests.pitch_brief.test_change_draft import Model, _res, run  # noqa: E402

WAIT = 30000


def _shot(page, name):                                                                       # optional review capture (UI_NEXT_SHOTS); never affects the assertions
    import os
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"{name}.png"), full_page=False)


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_manifest_explicit_choice_and_a_run_to_the_prepared_proposal(env, ch, monkeypatch, pw_browser):
    djid, body = run(env, ch, monkeypatch, Model())
    iid = _res(body)["intent"]["intent_id"]
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash=f"changes?intent={iid}&draft={djid}")
        man = page.get_by_test_id("prep-manifest")
        expect(man).to_be_visible(timeout=WAIT)
        expect(man).to_contain_text("0 paid model calls")
        expect(page.get_by_test_id("prep-start")).to_be_disabled()
        expect(page.get_by_test_id("prep-choices")).to_contain_text("accepted edits")
        edit = page.get_by_test_id("prep-edit").first
        assert edit.is_checked() is False                                                            # nothing is ticked for the person
        edit.check()
        expect(page.get_by_test_id("prep-start")).to_be_enabled(timeout=WAIT)
        expect(page.get_by_test_id("prep-choices")).to_have_count(0)                                  # the manifest for the choice has loaded
        page.get_by_test_id("prep-manifest").scroll_into_view_if_needed()
        _shot(page, "eui08_manifest_choice")                                                          # stable: the state the person sees before Start
        from tests.ui_next.test_a11y import scoped_violations
        assert scoped_violations(page, '[data-testid="prep-manifest"]') == []
        page.get_by_test_id("prep-start").click()
        run_el = page.get_by_test_id("prep-run")
        expect(run_el).to_have_attribute("data-status", "PROPOSAL_READY", timeout=120000)
        expect(run_el).to_contain_text("Nothing is approved, committed or applied")
        run_el.scroll_into_view_if_needed()
        _shot(page, "eui08_run_proposal_ready")
        expect(page.get_by_test_id("state-proposal")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)
        assert not page._errors, page._errors
        page.context.close()
    finally:
        server.stop()
        worker.stop.set()
