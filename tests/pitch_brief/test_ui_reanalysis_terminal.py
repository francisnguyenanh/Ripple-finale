"""A change whose AI re-analysis used every approved provider attempt (owner decision, Option A): what each person sees and may do. Real server, real Chromium, real
routes/store/worker drain, STUB provider that always times out. Wiring evidence, not live provider behaviour.
  * the change is applied and verified, the published source is unchanged, and the page says the re-analysis FAILED and will not run again (never "still running")
  * no Retry is offered for a job without attempts (it would be refused); a failed job that still has an attempt keeps its Retry
  * nobody can accept the result; a person with the result permission sees Request changes (the primary next step) and Reject result; others are told who decides
  * the same after a reload (the facts come from the server)"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import extraction  # noqa: E402
from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import create_job, drain  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import _to_approval, chain_to_proposal  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _account, _signed_in  # noqa: E402


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


class _Outage:
    """The re-analysis provider: every call times out (the worker's in-job retry makes the second attempt, then the job is FAILED)."""
    def __init__(self):
        self.calls = 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        raise TimeoutError("provider outage")


def _applied_with_failed_reanalysis(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, _review, aj, appr = _to_approval(env, ch, c)
    dec = {"workspace_id": env.ws, "action": "approve", "reason": "ok", "expected_approval_digest": appr["approval_digest"]}
    assert op.post(f"/v2/jobs/{aj}/decision", json=dec).status_code == 200
    stub = _Outage()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 200, r.text
    for _ in range(6):
        drain(env)
    dto = c.req.get(f"/v2/changes/{apj}", params={"workspace_id": env.ws}).json()
    assert dto["reanalysis"]["status"] == "FAILED", dto["reanalysis"]
    return c, apj, dto, stub


def _url(server, env, c):
    return f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}"


def _open(page, url):
    page.goto("about:blank")
    page.goto(url)
    expect(tid(page, "state-reanalysis")).to_be_visible(timeout=WAIT)


def test_a_reanalysis_without_attempts_is_shown_as_terminal_and_points_to_request_changes_or_reject(env, ch, monkeypatch, pw_browser):
    c, apj, dto, stub = _applied_with_failed_reanalysis(env, ch, monkeypatch)
    assert dto["reanalysis"]["terminal"] is True and dto["completion"]["eligible"] is False
    calls = stub.calls
    server = ServerThread().start()
    try:
        approver, admin = _account(env, "change_approver"), (env.admin, f"cred-a-{env.nonce}")
        c1, req = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        c2, apv = _signed_in(pw_browser, server, env, approver)
        c3, adm = _signed_in(pw_browser, server, env, admin)
        url = _url(server, env, c)
        for p in (req, apv, adm):
            _open(p, url)
        for who, p in (("requester", req), ("approver", apv), ("administrator", adm)):
            expect(tid(p, "state-reanalysis")).to_have_attribute("data-state", "FAILED")
            expect(tid(p, "reanalysis-terminal")).to_be_visible(timeout=WAIT)
            expect(tid(p, "reanalysis-terminal")).to_contain_text("cannot be retried")
            expect(tid(p, "reanalysis-terminal")).to_contain_text("published source is unchanged")
            expect(tid(p, "state-outcome")).to_contain_text("has no provider attempts left")
            assert "still running" not in tid(p, "state-outcome").inner_text(), who
            expect(tid(p, "reanalysis-exhausted").first).to_be_visible()
            assert tid(p, "action-retry-reanalysis").count() == 0, f"{who}: a Retry that the worker would refuse must not be offered"
            tc = p.evaluate("document.body.textContent")
            assert "Machine verification" in tc                                         # the earlier verification result is still on the page
        # who may act: the requester has no result permission and is told who decides; the approver gets the two answers, the primary one first
        expect(tid(req, "reanalysis-terminal")).to_contain_text("a person who may decide the result")
        assert tid(req, "action-request-changes").count() == 0 and tid(req, "action-reject-result").count() == 0
        expect(tid(apv, "reanalysis-terminal")).to_contain_text("Request changes or Reject result below")
        expect(tid(apv, "action-request-changes")).to_be_enabled()
        expect(tid(apv, "action-reject-result")).to_be_enabled()
        expect(tid(apv, "action-accept-result")).to_be_disabled()                       # accepting stays blocked, with the reason in words
        expect(tid(apv, "accept-blocked-why")).to_contain_text("no provider attempts left")
        # the facts come from the server: the same after a reload
        apv.reload()
        expect(tid(apv, "reanalysis-terminal")).to_be_visible(timeout=WAIT)
        # a person with the result permission can answer, and the answer is recorded; no provider call and no retry happened
        tid(apv, "action-request-changes").click()
        expect(tid(apv, "state-completion")).to_have_attribute("data-state", "CHANGES_REQUESTED", timeout=WAIT)
        assert stub.calls == calls, "the pages and the decision made no provider call"
        done = c.req.get(f"/v2/changes/{apj}", params={"workspace_id": env.ws}).json()
        assert done["completion"]["decision"]["action"] == "request_changes" and done["knowledge"]["candidate_revision_id"] is None
    finally:
        server.stop()


def test_a_failed_reanalysis_that_still_has_an_attempt_keeps_its_retry_and_is_not_called_terminal(env, ch, monkeypatch, pw_browser):
    c, apj, dto, stub = _applied_with_failed_reanalysis(env, ch, monkeypatch)
    job_ids = dto["reanalysis"]["job_ids"]
    with env.store._tx() as cur:                      # leave one provider attempt on every failed job (the stub otherwise always spends both)
        for jid in job_ids:
            cur.execute("""DELETE FROM v2_job_attempts WHERE ctid IN (SELECT ctid FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                              AND provider_calls > 0 ORDER BY attempt_key LIMIT 1)""", (env.tenant, env.ws, jid))
    now = c.req.get(f"/v2/changes/{apj}", params={"workspace_id": env.ws}).json()["reanalysis"]
    assert now["terminal"] is False and now["exhausted_job_ids"] == [] and now["retryable_job_ids"], now     # the failed jobs have an attempt left; the others have not run yet
    server = ServerThread().start()
    try:
        c3, adm = _signed_in(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        _open(adm, _url(server, env, c))
        expect(tid(adm, "state-reanalysis")).to_have_attribute("data-state", "FAILED")
        assert tid(adm, "reanalysis-terminal").count() == 0
        assert "has no provider attempts left" not in tid(adm, "state-outcome").inner_text()
        for jid in now["retryable_job_ids"]:
            expect(adm.locator(f"[data-testid=reanalysis-job][data-job-id='{jid}'] [data-testid=action-retry-reanalysis]")).to_be_enabled(timeout=WAIT)
        assert tid(adm, "reanalysis-exhausted").count() == 0
    finally:
        server.stop()
