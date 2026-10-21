"""F-ACC-1 + F-ACC-2 browser journey on the React build (/app-next/), two accounts, the throwaway PostgreSQL (2026-10-21): from the extracted Harbor sources to accepted
business knowledge in the UI, then a business question that needs a SECOND PERSON in a sandbox workspace.

Admin: Rules says the business rules have not been read; reading without the writing lease is refused with the reason; after the lease the reading succeeds and
states the result and the next step; Business questions -> "Accept this knowledge". Requester: asks; the answer waits and says why, who may allow it and where; the
requester has no allow control. Admin: the run is listed under "AI runs waiting for a second person" and allowed there; the requester then gets the answer.
Real server, worker, Chromium and routes; stub model (no provider). Not model quality, not human acceptance."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_kno03_graph_gaps import seed  # noqa: E402
from tests.final_loop.test_kno04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_qry03_qa import Model, obj  # noqa: E402
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, LiveWorker, sandbox_config  # noqa: E402


@pytest.fixture
def biz(env, ws, monkeypatch, tmp_path):                       # noqa: F811
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("role_model_ids:\n", "role_model_ids:\n  knowledge.qa: gemini-3.8-flash\n", 1)   # the stub serves 3.8 Flash (as qa_env)
    path = tmp_path / "budget_biz.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    model = Model(lambda p: obj("ABSTAINED", uncertainty="The stub declines."))
    monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    head = seed(env, ws)                                                                       # stands in for the extraction RESULT: observed rules, not analysed
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, head.source_revision_id))
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    yield env, model, server
    server.stop()
    worker.stop.set()


def test_source_to_accepted_knowledge_then_a_question_allowed_by_a_second_person(biz, pw_browser):
    env, model, server = biz
    admin = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
    admin.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=rules")
    read = admin.get_by_test_id("business-read").first
    expect(read).to_have_attribute("data-state", "never", timeout=WAIT)
    read.get_by_test_id("business-read-run").click()                                          # no writing lease yet: refused, with the reason, nothing read
    expect(read.get_by_test_id("business-read-error")).to_contain_text("Sandbox page")
    lease = env.admin_client().post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire", "participants": [env.req]})   # the Sandbox page's action, same account
    assert lease.status_code == 200, lease.text
    read.get_by_test_id("business-read-run").click()
    result = read.get_by_test_id("business-read-result")
    expect(result).to_contain_text("business question(s)")
    expect(result).to_contain_text("Accept this knowledge")
    expect(admin.locator('[data-testid="business-rules"]')).to_be_visible()                  # the rules section reloaded
    read.get_by_test_id("business-read-next").click()
    version = admin.get_by_test_id("business-version")
    expect(version.get_by_test_id("business-proposal")).to_be_visible(timeout=WAIT)
    if version.get_by_test_id("business-ack").count():
        version.get_by_test_id("business-ack").check()
    version.get_by_test_id("business-accept").click()
    expect(version).to_contain_text("Accepted business knowledge: version", timeout=WAIT)
    assert not admin._errors, admin._errors

    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=ask")
    req.get_by_test_id("qa-input").fill("Who approves a purchase of 1,200,000 JPY?")
    req.get_by_test_id("qa-ask").click()
    hold = req.get_by_test_id("run-hold")
    expect(hold).to_have_attribute("data-state", "waiting_for_approval", timeout=WAIT)
    expect(hold).to_have_attribute("data-may", "false")
    expect(hold.get_by_test_id("run-hold-who")).to_contain_text("other than the person who asked")
    expect(hold.get_by_test_id("run-hold-where")).to_contain_text("AI runs waiting for a second person")
    expect(hold.get_by_test_id("run-hold-why")).to_contain_text("a different person has to allow it")
    expect(req.get_by_test_id("run-hold-allow")).to_have_count(0)                            # the person who asked has no allow control
    expect(req.get_by_test_id("ai-runs-awaiting")).to_have_count(0)
    assert model.calls == 0

    admin.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=ask")
    panel = admin.get_by_test_id("ai-runs-awaiting")
    expect(panel).to_be_visible(timeout=WAIT)
    expect(panel.get_by_test_id("ai-run-awaiting")).to_have_count(1)
    expect(panel).to_contain_text("Who approves a purchase of 1,200,000 JPY?")
    panel.get_by_test_id("ai-run-allow").click()
    expect(admin.get_by_test_id("ai-runs-awaiting")).to_have_count(0, timeout=WAIT)          # allowed: nothing left for this person
    try:
        expect(req.get_by_test_id("qa-answer")).to_be_visible(timeout=90000)                 # waits for a worker slot, then runs (the held claim is deferred)
    except AssertionError:
        with env.store._tx() as cur:
            cur.execute("SELECT job_id, kind, status, execution_status, execution_reason FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='knowledge.qa'", (env.tenant, env.ws))
            rows = cur.fetchall()
        pend = req.get_by_test_id("qa-pending")
        raise AssertionError(f"jobs={rows} pending={pend.get_attribute('data-status') if pend.count() else None} calls={model.calls} "
                             f"failed={req.get_by_test_id('qa-failed').inner_text() if req.get_by_test_id('qa-failed').count() else None}")
    assert model.calls == 1
    assert not req._errors and not admin._errors, (req._errors, admin._errors)
