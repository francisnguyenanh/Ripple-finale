"""UI of live B6 (2026-10-09): the draft fails because the provider's answer was cut off. Real server, real Chromium, real routes/store/worker; STUB provider.

What the person must see: the job FAILED, in words WHY and WHAT TO DO (not only "provider execution failed"), no draft, the machine reason only in the
Technical view, a Try again button that re-runs the SAME job after the cause is fixed and leaves the failed attempt in the history."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft  # noqa: E402
from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import S1, build, intent  # noqa: E402
from tests.pitch_brief.conftest import drain, checks_ready  # noqa: E402
from tests.pitch_brief.test_change_draft import Model  # noqa: E402
from tests.pitch_brief.test_draft_provider_failures import Raw, _resp  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _signed_in  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_a_cut_off_draft_is_explained_and_can_be_tried_again(env, ch, monkeypatch, pw_browser):
    bad = Raw(_resp("MAX_TOKENS", usage=(900, None, None, 7044)))
    good = Model()
    client = {"now": bad}
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: client["now"])
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    iid = intent(env, ch.admin, S1, confirm=True)["intent_id"]
    server = ServerThread().start()
    try:
        ctx, page = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        url = f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={iid}"
        page.goto(url)
        checks_ready(env)
        tid(page, "action-run-draft").click()
        try:
            expect(tid(page, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        except AssertionError:
            raise AssertionError("draft job did not appear; page says: " + page.locator("body").inner_text()[:1800])
        url = page.url                                                    # the page keeps the draft id in its address
        drain(env)
        page.goto("about:blank")
        page.goto(url)
        expect(tid(page, "state-draft")).to_have_attribute("data-state", "FAILED", timeout=WAIT)
        box = tid(page, "job-failure")
        expect(box).to_be_visible(timeout=WAIT)
        expect(box).to_contain_text("cut off")
        expect(box).to_contain_text("Nothing from it was saved as a draft")
        expect(tid(page, "job-failure-next")).to_contain_text("administrator")
        body = page.locator("body").inner_text()
        assert "provider_output_truncated" not in body and "provider_execution_failed" not in body       # business view: words, not codes
        assert tid(page, "draft-status").count() == 0                                                     # a cut-off answer is never shown as a draft
        retry = tid(page, "action-retry-job")
        expect(retry).to_be_enabled()
        # Technical view keeps the server's exact reason
        page.goto(url + "&mode=technical")
        expect(tid(page, "job-reason")).to_contain_text("provider_output_truncated", timeout=WAIT)
        expect(tid(page, "job-reason")).to_contain_text("finish_reason=MAX_TOKENS")
        # the cause is fixed (the stub now answers): Try again re-runs the SAME job
        client["now"] = good
        tid(page, "action-retry-job").click()
        expect(tid(page, "state-draft")).not_to_have_attribute("data-state", "FAILED", timeout=WAIT)
        drain(env)
        page.goto("about:blank")
        page.goto(url)
        expect(tid(page, "draft-status")).to_have_attribute("data-state", "PARTIAL", timeout=WAIT)
        assert tid(page, "job-failure").count() == 0 and bad.calls == 1 and good.calls == 1
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()
