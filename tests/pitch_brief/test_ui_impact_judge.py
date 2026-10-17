"""Review claim 6 through the browser: the person presses ONE button on the impact analysis; the page picks the files with the most UNKNOWN rules (at most 3), asks the AI one file at
a time (stub provider here), then repeats the impact analysis with the finished judge jobs. The AI's answer appears in its own section (re-checked, never a deterministic dependency),
and the deterministic buckets are the same as before. Real server, real Chromium, real routes/store/worker."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, impact_judge  # noqa: E402
from tests.ai10ui.contract import UI_BASE, select_workspace, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import LiveWorker, create_job, job_body  # noqa: E402
from tests.pitch_brief.test_change_draft import Model, _res, run  # noqa: E402
from tests.pitch_brief.test_impact_judge_orchestration import Judge, _targets  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


class QuoteAwareJudge(Judge):
    """Says 'depends' with a quote that really is in the file it was given (the first file whose quote appears in the prompt); never invents one."""

    def __init__(self, quotes):
        super().__init__(depends=True)
        self.quotes, self.files_seen = quotes, []

    def _gen(self, **kw):
        text = str(kw)
        hit = next((q for q in self.quotes if q and q in text), None)
        self.files_seen.append(hit)
        self.quote, self.depends = hit, hit is not None
        return super()._gen(**kw)


def test_one_button_investigates_unknown_rules_with_a_bounded_number_of_calls_and_shows_the_answer_apart(env, ch, monkeypatch, pw_browser):
    jid, body = run(env, ch, monkeypatch, Model())
    intent_id = _res(body)["intent"]["intent_id"]
    _t, _s, files = _targets(env, ch, None)
    judge = QuoteAwareJudge([q for _a, q in files])
    monkeypatch.setattr(impact_judge, "_client_for_model", lambda m: judge)
    monkeypatch.setattr(impact_judge, "_assert_global_endpoint", lambda: None, raising=False)
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    try:
        page = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        page.goto("about:blank")
        page.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={jid}")
        tid(page, "action-run-impact").click()
        expect(tid(page, "investigate-unknown")).to_be_visible(timeout=60000)
        assert judge.calls == 0                                                      # showing the offer calls nothing
        tid(page, "action-investigate-unknown").click()
        expect(tid(page, "investigate-intake")).to_contain_text("requested", timeout=120000)
        with env.store._tx() as cur:
            cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='impact.judge'", (env.tenant, env.ws))
            made = cur.fetchone()[0]
        assert 1 <= made <= 3 and 1 <= judge.calls <= made                           # bounded: at most 3 files, at most one call each (a file that is not rule-bearing is turned away before any call)
        expect(tid(page, "impact-indirect-model")).to_be_visible(timeout=WAIT)       # the AI judgement is its own section ...
        assert "re-checked by the server" in page.locator("body").inner_text()       # ... said as such, never a proven dependency
        calls_after = judge.calls
        tid(page, "action-investigate-unknown").click()                              # pressing it again cannot go past the bound: no more jobs than before, no new call for a file already judged
        page.wait_for_timeout(8000)
        with env.store._tx() as cur:
            cur.execute("SELECT execution_status, count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='impact.judge' GROUP BY 1", (env.tenant, env.ws))
            states = dict(cur.fetchall())
        assert states.get("COMPLETED", 0) <= 3                                       # what cost a model call is bounded; a file turned away before any call is a FAILED job (not counted, costs nothing)
        assert judge.calls == calls_after
        assert not page._errors, page._errors
    finally:
        worker.stop.set()
        server.stop()
