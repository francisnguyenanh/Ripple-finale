"""UI of live B8 (2026-10-09): the AI reading of a simulation. While it runs the person must SEE that it runs (a coloured, animated state and a banner with the
seconds waited), it must keep checking for long enough (a rate-limited call took ~3 minutes), say when it is ready, and a failure must say why and what to do.
Real server, real Chromium, real routes/store/worker; STUB provider."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import drain  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal  # noqa: E402
from tests.pitch_brief.test_sim_analysis import Reader, pinned  # noqa: E402,F401
from tests.pitch_brief.test_ui_handoff import WAIT, _signed_in  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _expand(page, stage):
    """A finished stage is collapsed: open it the way a person does."""
    card = tid(page, stage)
    expect(card).to_be_visible(timeout=WAIT)
    if card.get_attribute("data-open") != "true":
        tid(page, stage + "-toggle").click()


def _open_url(server, env, c):
    return f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={c.intent}&draft={c.draft}&impact={c.impact}"


def _simulate_in_the_page(env, page):
    """Run the simulation the way a person does (the server builds it from the draft), so the AI reading card is offered."""
    _expand(page, "flow-analysis")
    tid(page, "action-run-simulation").click()
    expect(tid(page, "state-simulation")).not_to_have_attribute("data-state", "NONE", timeout=WAIT)
    drain(env)
    expect(tid(page, "state-simulation")).to_have_attribute("data-state", "COMPLETED", timeout=WAIT)


def test_the_reading_shows_running_then_done_and_says_when_it_is_ready(env, ch, pinned, monkeypatch, pw_browser):
    reader = Reader()
    monkeypatch.setattr(explain, "_client_for_model", lambda m: reader)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    c = chain_to_proposal(env, ch, monkeypatch)
    server = ServerThread().start()
    try:
        ctx, page = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        page.goto(_open_url(server, env, c))
        _simulate_in_the_page(env, page)
        btn = tid(page, "sim-analysis-request")
        try:
            expect(btn).to_be_visible(timeout=WAIT)
        except AssertionError:
            raise AssertionError("no request button; analysis stage says: " + (tid(page, "flow-analysis").inner_text()[:1500]))
        btn.click()
        box = tid(page, "sim-analysis-running")
        expect(box).to_be_visible(timeout=WAIT)                                          # running is a visible banner, not a grey line
        expect(box).to_contain_text("Running: the AI is writing the reading")
        expect(box).to_contain_text("Do not click again")
        pill = tid(page, "sim-analysis-state")
        expect(pill).to_have_attribute("data-state", "RUNNING")
        assert "animate-pulse" in (pill.get_attribute("class") or "")                    # the state itself moves/changes colour
        assert tid(page, "sim-analysis-request").count() == 0                            # no second paid click is offered while it runs
        drain(env)                                                                       # the worker finishes the call
        expect(tid(page, "sim-analysis")).to_have_attribute("data-state", "done", timeout=WAIT)   # the page found out by itself
        expect(tid(page, "sim-analysis-state")).to_have_attribute("data-state", "COMPLETED")
        expect(tid(page, "sim-analysis-state")).to_contain_text("Done")
        assert tid(page, "sim-analysis-running").count() == 0
        expect(page.locator("body")).to_contain_text("The AI reading is ready.")          # and said so
        assert reader.calls == 1 and not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_a_failed_reading_says_why_and_what_to_do(env, ch, pinned, monkeypatch, pw_browser):
    reader = Reader(mutate=lambda obj, facts: {**obj, "changed": {**obj["changed"], "changed_count": 999}})     # a wrong count: the server refuses to store it
    monkeypatch.setattr(explain, "_client_for_model", lambda m: reader)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    c = chain_to_proposal(env, ch, monkeypatch)
    server = ServerThread().start()
    try:
        ctx, page = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        page.goto(_open_url(server, env, c))
        _simulate_in_the_page(env, page)
        tid(page, "sim-analysis-request").click()
        expect(tid(page, "sim-analysis-running")).to_be_visible(timeout=WAIT)
        drain(env)
        expect(tid(page, "sim-analysis")).to_have_attribute("data-state", "failed", timeout=WAIT)
        box = tid(page, "sim-analysis-failed")
        expect(box).to_be_visible(timeout=WAIT)
        expect(box).to_contain_text("The AI reading was not stored")
        expect(tid(page, "sim-analysis-state")).to_have_attribute("data-state", "FAILED")
        expect(tid(page, "sim-analysis-retry")).to_be_visible()
        ctx.close()
    finally:
        server.stop()
