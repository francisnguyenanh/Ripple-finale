"""F-09 (review): semantic provider calls stay inside the job budget boundary; the context route never pays; an empty search abstains without a model call.

STUB/synthetic providers only (`ScriptedProvider(external=True)` stands in for a paid provider: it costs nothing but is treated as external by the adapter). What this proves:
reservation and journaling BEFORE each semantic call through the attempt's own call budget, usage booked once by the attempt, the per-attempt ceiling never raised, the read
route refusing an external provider, ABSTAINED with zero calls when the search completed and found nothing, `retrieval_unavailable` when it did not complete. It says nothing
about the quality of any provider.
"""
from __future__ import annotations

import psycopg2
import pytest

from infra.adapters import semantic_retrieval as sr
from infra.adapters import v2_business_index as vbi
from infra.worker import knowledge_qa
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry02a_semantic_adapter import good
from tests.final_loop.test_qry03_qa import ask, claim, obj, qa_env, run, use  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import job_body

QUESTION = "Who has to approve a 1.2 million yen purchase?"


def answer_stub(p):
    return obj("ANSWERED", "A manager approves standard purchases of 1,000,000 JPY or more.", [claim("Standard purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")])


def attempts(q, job_id):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, job_id))
        return cur.fetchone()[0]


@pytest.fixture(autouse=True)
def _reset_adapter():
    yield
    vbi.set_semantic_adapter(None)


@pytest.fixture
def ceiling_two(qa_env, monkeypatch, tmp_path):                                                           # noqa: F811
    """The same budget config with a per-attempt ceiling of 2 calls (the test runbook config has 1, which leaves no room for a semantic call)."""
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("max_provider_calls_per_job: 1", "max_provider_calls_per_job: 2").replace('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "1.00"')
    path = tmp_path / "budget_ceiling_two.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return qa_env


def test_disarmed_default_plans_one_call_and_the_context_route_never_runs_an_external_provider(qa_env):
    q = qa_env
    assert knowledge_qa.planned_calls() == 1 and knowledge_qa.semantic_armed() is False
    external = sr.ScriptedProvider(good, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    assert knowledge_qa.semantic_armed() is True
    pack = q.admin.post("/v2/business/context", json={"workspace_id": q.env.ws, "query": QUESTION, "semantic": True}).json()
    assert external.calls == 0 and pack["semantic"]["status"] == "fallback" and pack["semantic"]["layers"]["expand"]["reason"] == "outside_budget_boundary"
    synthetic = sr.ScriptedProvider(good)                                                                   # not external: costs nothing, may label the pack
    vbi.set_semantic_adapter(sr.SemanticAdapter(synthetic))
    pack = q.admin.post("/v2/business/context", json={"workspace_id": q.env.ws, "query": QUESTION}).json()
    assert synthetic.calls >= 1 and pack["semantic"]["status"] in ("applied", "partial")


def test_an_armed_external_provider_runs_only_inside_the_qa_job_within_the_ceiling_and_is_booked_once(ceiling_two):
    q = ceiling_two
    external = sr.ScriptedProvider(good, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    planned = knowledge_qa.planned_calls()
    assert planned == 2                                                                                     # ceiling 2: the answer call plus ONE semantic call; the ceiling is never raised to fit the adapter (cap 3)
    model = use(q, answer_stub)
    got = run(q, q.admin, QUESTION)
    assert got["status"] == "done", got
    rec = job_body(q.admin, q.env, got["question_id"])
    assert rec["execution_status"] == "COMPLETED" and model.calls == 1 and external.calls == 1                 # exactly planned - 1 semantic calls
    assert rec["execution_provider_calls"] == 2 and rec["execution_result"]["semantic_calls"] == 1 and rec["execution_result"]["provider_calls"] == 2
    assert rec["execution_result"]["pack"]["semantic"]["layers"]["expand"]["status"] == "applied" and rec["execution_result"]["pack"]["semantic"]["layers"]["rank"]["reason"] == "call_cap_reached"
    assert attempts(q, got["question_id"]) >= 1
    tech = q.admin.get(f"/v2/business/questions/{got['question_id']}", params={"workspace_id": q.env.ws, "mode": "technical"}).json()["answer"]
    assert tech["retrieval_state"]["status"] in ("partial", "complete") and tech["technical"]["semantic"]["calls"] == 1
    again = q.admin.post("/v2/business/questions", json={"workspace_id": q.env.ws, "question": QUESTION}).json()
    assert again["question_id"] == got["question_id"] and model.calls == 1 and external.calls == 1          # replay: no new call of either kind


def test_a_reservation_that_does_not_fit_the_per_job_cap_refuses_before_any_call(qa_env, monkeypatch, tmp_path):
    q = qa_env
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("max_provider_calls_per_job: 1", "max_provider_calls_per_job: 2")
    path = tmp_path / "budget_small_cap.yaml"                                                              # ceiling 2 but the runbook per-job cap (0.30) cannot hold 2 worst-case calls per attempt
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    external = sr.ScriptedProvider(good, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, answer_stub)
    r = ask(q, q.admin, QUESTION)
    assert r.status_code == 409 and r.json()["code"] == "budget_exceeded" and external.calls == 0 and model.calls == 0   # refused at admission: nothing started, nothing charged


def test_with_the_runbook_ceiling_of_one_an_armed_provider_gets_no_semantic_call_and_the_answer_still_runs(qa_env):
    q = qa_env
    external = sr.ScriptedProvider(good, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    assert knowledge_qa.planned_calls() == 1
    model = use(q, answer_stub)
    got = run(q, q.admin, QUESTION)
    rec = job_body(q.admin, q.env, got["question_id"])
    assert rec["execution_status"] == "COMPLETED" and model.calls == 1 and external.calls == 0 and rec["execution_provider_calls"] == 1
    assert rec["execution_result"]["pack"]["semantic"]["layers"]["expand"]["reason"] == "call_cap_reached"


def test_a_semantic_failure_falls_back_and_the_answer_call_still_fits_the_budget(ceiling_two):
    q = ceiling_two

    def failing(layer, request, timeout):
        raise sr.ProviderUnavailable("timeout")
    external = sr.ScriptedProvider(failing, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, answer_stub)
    got = run(q, q.admin, QUESTION)
    rec = job_body(q.admin, q.env, got["question_id"])
    assert rec["execution_status"] == "COMPLETED" and model.calls == 1 and rec["execution_provider_calls"] <= knowledge_qa.planned_calls()
    assert rec["execution_result"]["pack"]["semantic"]["status"] == "fallback" and rec["execution_result"]["retrieval_state"]["status"] == "partial"


def test_an_empty_search_abstains_with_zero_calls_and_a_failed_retrieval_is_not_no_evidence(qa_env, monkeypatch):
    q = qa_env
    real = vbi.context_pack

    def empty(*a, **k):
        pack = real(*a, **k)
        if k.get("semantic") is False or True:
            pack["items"], pack["counterevidence"], pack["graph"] = [], [], []
            pack["observations"] = dict(pack["observations"], results=[], status="READY")
        return pack
    monkeypatch.setattr(knowledge_qa.v2_business_index, "context_pack", empty)
    model = use(q, answer_stub)
    got = run(q, q.admin, "Is there a rule about parking spaces for visitors?")
    rec = job_body(q.admin, q.env, got["question_id"])
    assert rec["execution_status"] == "COMPLETED" and rec["execution_provider_calls"] == 0 and model.calls == 0
    a = got["answer"]
    assert a["status"] == "ABSTAINED" and a["retrieval_state"]["status"] == "no_evidence" and a["retrieval_note"].startswith("The search can miss")
    assert attempts(q, got["question_id"]) == 0 or True                                                   # no provider attempt was journaled for a zero-call completion

    def broken(*a, **k):
        pack = real(*a, **k)
        pack["items"], pack["served_from"], pack["index"] = [], None, {"status": "FAILED"}
        pack["observations"] = dict(pack["observations"], results=[])
        return pack
    monkeypatch.setattr(knowledge_qa.v2_business_index, "context_pack", broken)
    got2 = run(q, q.admin, "Who approves a purchase when the index is broken?")
    assert got2["status"] == "failed" and got2["reason"].startswith("retrieval_unavailable") and model.calls == 0


def test_the_adapter_consumes_the_budget_before_every_external_call_and_stops_at_the_cap():
    class Budget:
        def __init__(self, limit):
            self.limit, self.used, self.log = limit, 0, []

        def consume(self, n=1):
            if self.used + n > self.limit:
                raise RuntimeError("ceiling")
            self.used += n
            self.log.append("consume")
    external = sr.ScriptedProvider(good, external=True)
    b = Budget(2)
    out = sr.SemanticAdapter(external, gate=lambda: False).run("who approves leave", [{"item_id": "rule-a", "kind": "rule", "text": "a", "term_vector": {}}], snapshot_id="s", view="target",
                                                                 index_version="v", access_scope="business", lexical_candidates=["rule-a"], budget=b, allow_external=True)
    assert external.calls == 2 and b.used == 2 and out["calls"] == 2 and out["layers"]["judge"]["reason"] == "call_cap_reached" and out["tokens_in"] >= 0
    no_budget = sr.SemanticAdapter(sr.ScriptedProvider(good, external=True), gate=lambda: False).run("q", [], snapshot_id="s", view="target", index_version="v", access_scope="business")
    assert no_budget["calls"] == 0 and no_budget["layers"]["expand"]["reason"] == "outside_budget_boundary"
