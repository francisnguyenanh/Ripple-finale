"""Review claim 1 in the browser: before the model is asked for a draft, the checks behind it (relation coverage and the conflict check of THIS revision) are created once by the page
(deterministic, no AI) and their state is shown; a failed check stops the draft with a reason instead of a draft that silently lacks it. A background worker runs the jobs like the live
server does. Real server, real Chromium, real routes/store/worker, STUB provider."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, findings as findings_mod  # noqa: E402
from infra.worker.execution_types import NonRetryableExecutionError  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker  # noqa: E402
from tests.ai10ui.contract import UI_BASE, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import S1, build, intent  # noqa: E402
from tests.pitch_brief.test_change_draft import Model  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _jobs(env, kind):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s", (env.tenant, env.ws, kind))
        return cur.fetchone()[0]


def test_generate_runs_the_checks_first_and_the_draft_says_it_had_them(env, ch, monkeypatch, pw_browser):
    model = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    iid = intent(env, ch.admin, S1, confirm=True)["intent_id"]
    worker = LiveWorker(env)
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        page.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={iid}")
        panel = tid(page, "coverage-panel")
        expect(panel).to_be_visible(timeout=WAIT)
        expect(tid(page, "coverage-finding")).to_have_attribute("data-state", "none")
        expect(tid(page, "coverage-nofinding")).to_contain_text("NOT the same as no conflict")                      # not run: never read as "no conflict"
        worker.start()
        tid(page, "action-run-draft").click()
        expect(tid(page, "state-draft")).not_to_have_attribute("data-state", "NONE", timeout=60000)                  # the checks ran, then the draft was created
        assert _jobs(env, "relation") == 1 and _jobs(env, "finding") == 1 and _jobs(env, "change.draft") == 1       # once each
        expect(tid(page, "coverage-finding")).to_have_attribute("data-state", "done", timeout=WAIT)
        expect(tid(page, "coverage-relation")).to_have_attribute("data-state", "done")
        expect(tid(page, "draft-status")).to_be_visible(timeout=60000)
        expect(tid(page, "draft-basis-findings")).to_contain_text("A conflict and gap check (finding) exists for this revision")
        assert model.calls == 1 and not page._errors, page._errors
    finally:
        worker.stop.set()
        server.stop()


def test_a_failed_check_stops_the_draft_with_a_reason_and_offers_to_try_it_again(env, ch, monkeypatch, pw_browser):
    model = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)

    def boom(self, ctx):
        raise NonRetryableExecutionError("finding_boom", "test failure")
    monkeypatch.setattr(findings_mod.FindingsExecutor, "prepare", boom)
    iid = intent(env, ch.admin, S1, confirm=True)["intent_id"]
    worker = LiveWorker(env)
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        page.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={iid}")
        worker.start()
        tid(page, "action-run-draft").click()
        expect(tid(page, "coverage-failed")).to_contain_text("finding_boom", timeout=60000)
        expect(tid(page, "action-retry-coverage")).to_be_visible()
        page.wait_for_timeout(500)
        assert _jobs(env, "change.draft") == 0 and model.calls == 0                                                # no draft, no model call: nothing was spent on a draft that lacks its check
        assert not page._errors, page._errors
    finally:
        worker.stop.set()
        server.stop()
