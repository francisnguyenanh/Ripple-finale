"""The live-local journey driver (tools/judge_local/live_journey.py) against STUB providers (no spend): free text -> clarification -> request -> draft -> impact/simulation ->
proposal -> contract -> review -> approval -> apply -> re-extraction wave -> query on the new revision -> explanation. The same code runs against the real provider in live-local."""
from __future__ import annotations

import pytest

from infra.worker import change_draft, explain, extraction, gap_answer, intent_interpret
from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.conftest import drain
from tests.pitch_brief.test_change_draft import Model
from tests.pitch_brief.test_draft_to_proposal import _grant
from tests.pitch_brief.test_explain import Wording
from tests.pitch_brief.test_intent_chat import READY, VAGUE, Interp
from tests.pitch_brief.test_loop_feedback import GapStub, PostApplyExtraction
from tools.judge_local import live_journey as J


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _ctx(env, ch):
    return J.new_ctx(ws=env.ws, req=env.req_client(), adm=ch.admin, apv=_grant(env, "change_approver"), approval_path=APPROVAL, approval_artifact_id=ch.arts[APPROVAL],
                     extraction_ids=list(ch.jobs.values()), scope_artifact_ids=sorted(ch.arts.values()), pump=lambda: drain(env), poll=0, log=lambda m: None)


def test_the_whole_loop_runs_through_the_product_routes_and_stops_on_a_missing_target(env, ch, monkeypatch):
    interp, draft, post, wording = Interp(VAGUE, READY), Model(), PostApplyExtraction(), Wording()
    gap = GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000")
    monkeypatch.setattr(intent_interpret, "_client_for_model", lambda m: interp)
    monkeypatch.setattr(intent_interpret, "_assert_global_endpoint", lambda: None)
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: draft)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: post)
    monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: gap)
    monkeypatch.setattr(explain, "_client_for_model", lambda m: wording)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    out = J.run_journey(_ctx(env, ch))
    assert interp.calls == 2 and draft.calls == 1 and gap.calls == 1                       # one call per planned step, no retries
    assert out["loop_status"]["status"] == "PARTIAL"                                       # never COMPLETE
    sim = env.store.job_record(env.tenant, env.ws, out["simulation"])["execution_result"]       # the caller's payload reaches the Harbor adapter: it ran, over the public sample
    assert sim["status"] == "EXECUTED" and sim["adapter"] == "harbor_cli" and sim["harbor"]["changed_count"] > 0 and sim["coverage"]["submitted"] == 240
    assert _simulation_ran(env, out)
    assert out["answer_status"] == "answered" and post.calls == out["loop_status"]["stages"]["reanalysis"]["total"]
    for k in ("conversation_id", "intent_id", "draft", "impact", "simulation", "proposal", "review", "approval", "apply", "query"):
        assert out[k]
    # no impact.judge job was created
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='impact.judge'", (env.tenant, env.ws))
        assert cur.fetchone()[0] == 0


def test_a_clarification_that_stays_open_stops_the_journey_with_a_finding(env, ch, monkeypatch):
    interp = Interp(VAGUE)
    monkeypatch.setattr(intent_interpret, "_client_for_model", lambda m: interp)
    monkeypatch.setattr(intent_interpret, "_assert_global_endpoint", lambda: None)
    with pytest.raises(J.JourneyStop) as e:
        J.run_journey(_ctx(env, ch))
    assert e.value.step == "chat" and "stored for a person" in e.value.finding and interp.calls == 2


def _simulation_ran(env, out):
    from infra.v2_explain import build_facts
    facts = build_facts(env.store, env.tenant, env.ws, "approval", out["approval"])
    return facts["simulation"] is not None and facts["simulation"].get("ran") is not False
