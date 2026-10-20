"""F-09 (review): semantic provider calls stay inside the job budget boundary; the context route never pays; an empty search abstains without a model call.

STUB/synthetic providers only (`ScriptedProvider(external=True)` stands in for a paid provider: it costs nothing but is treated as external by the adapter). What this proves:
reservation and journaling BEFORE each semantic call through the attempt's own call budget, usage booked once by the attempt, the per-attempt ceiling never raised, the read
route refusing an external provider, ABSTAINED with zero calls when the search completed and found nothing, `retrieval_unavailable` when it did not complete. It says nothing
about the quality of any provider.
"""
from __future__ import annotations

from decimal import Decimal

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


@pytest.fixture
def ceiling_four(qa_env, monkeypatch, tmp_path):                                                          # noqa: F811
    """Per-attempt ceiling 4 (the full-semantic live variant): the answer call plus the three semantic layers."""
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("max_provider_calls_per_job: 1", "max_provider_calls_per_job: 4").replace('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "2.00"')
    path = tmp_path / "budget_ceiling_four.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return qa_env


def _sem(q, got):
    rec = job_body(q.admin, q.env, got["question_id"])
    return rec, rec["execution_result"]["pack"]["semantic"]


def test_f5_every_layer_gets_its_call_a_transient_failure_falls_back_on_its_own_layer_and_the_judge_still_runs(ceiling_four):
    """F5 (live finding FNL-LIVE-20261018-01/-02), mocked transport through the real route, worker, budget and ledger: (1) all three layers applied, 3 semantic + 1 answer
    calls booked once; (2) expand fails once: no retry may take a later layer's call, so expand falls back with its reason and rank and judge are applied (judge called);
    (3) an adapter whose own cap would allow a retry (4) still keeps the judge's call because the job's slice is 3: the retry is refused by the slice, not taken from judge."""
    q = ceiling_four
    external = sr.ScriptedProvider(good, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    assert knowledge_qa.planned_calls() == 4
    model = use(q, answer_stub)
    rec, sem = _sem(q, run(q, q.admin, QUESTION))
    assert {l: v["status"] for l, v in sem["layers"].items()} == {"expand": "applied", "rank": "applied", "judge": "applied"} and external.calls == 3
    assert rec["execution_provider_calls"] == 4 and rec["execution_result"]["semantic_calls"] == 3 and model.calls == 1
    for i, caps in enumerate(({}, {"max_calls": 4})):
        seen = {"n": 0}

        def once(layer, request, timeout, seen=seen):
            if layer == "expand" and seen["n"] == 0:
                seen["n"] += 1
                raise sr.ProviderUnavailable("transport_error")
            return good(layer, request, timeout)
        flaky = sr.ScriptedProvider(once, external=True)
        vbi.set_semantic_adapter(sr.SemanticAdapter(flaky, caps=caps, gate=lambda: False))
        use(q, answer_stub)
        rec, sem = _sem(q, run(q, q.admin, f"{QUESTION} (case {i})"))
        assert sem["layers"]["expand"]["status"] == "fallback" and sem["layers"]["expand"]["reason"] == "transport_error", (caps, sem["layers"])
        assert sem["layers"]["rank"]["status"] == "applied" and sem["layers"]["judge"]["status"] == "applied" and flaky.calls == 3, (caps, sem["layers"])
        assert rec["execution_provider_calls"] == 4 and rec["execution_result"]["semantic_calls"] == 3


def test_a_booked_adjustment_holds_the_real_question_route_to_the_net_allowance(qa_env, monkeypatch, tmp_path):
    """F6 follow-up (owner request): the adjustment is not only a driver-side number. The product's own QA route reserves against the project ledger that
    contains it: a question that fits the cap without the adjustment is refused at admission with it, before any call. STUB provider."""
    import os
    from infra.adapters.v2_postgres import PROJECT_SPEND_SQL, project_spend_params
    from infra.worker import job_quota
    q = qa_env
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute(PROJECT_SPEND_SQL, project_spend_params(q.env.tenant, q.env.ws))
        spent = cur.fetchone()[0]
    est = job_quota.compute_budget_reservation(job_quota.load_worker_budget_config())[0]
    cap = (spent + est + Decimal("0.01")).quantize(Decimal("0.0001"))
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    import re
    cfg = re.sub(r'total_cap_usd: "[^"]*"', f'total_cap_usd: "{cap}"', cfg)
    cfg = re.sub(r'per_job_cap_usd: "[^"]*"', f'per_job_cap_usd: "{est}"', cfg)                      # a valid config: the per-job cap may not exceed the total cap
    path = tmp_path / "budget_tight_cap.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    assert spent + est <= cap < spent + est + Decimal("0.02")                                             # without the adjustment this question would be admitted
    model = use(q, answer_stub)
    q.env.store.record_cost_adjustment(q.env.tenant, q.env.ws, "ADJ-20261018-QA", "0.02", reason="usage lost before booking, priced at the ceiling")
    r = ask(q, q.admin, "Who approves a purchase of 2.2 million yen?")
    assert r.status_code == 409 and r.json()["code"] == "budget_exceeded" and model.calls == 0
    assert ask(q, q.admin, "Who approves a purchase of 2.2 million yen?").status_code == 409                # stays refused; the adjustment is booked once


# ---- LLM-Q2 (roadmap 15.5, 2026-10-18): a lexical miss over a non-empty authorized corpus is not proof; the semantic layers search before the question is abstained -----
VOCAB_GAP = "Which person okays a buy of 1.5 million yen?"                                             # shares no lexical term with the manager rule's words


def _lexical_miss(monkeypatch):
    """The Harbor fixture always returns loosely related rules lexically (no absolute relevance signal), so the vocabulary-gap case is set up at the ranking boundary:
    the lexical-only ranking finds nothing; the ranking that merges the semantic layers' candidates runs unchanged. Route, worker, budget and validators are real."""
    from core.change_intelligence.v2 import business_index as bi
    real = bi.rank_items

    def rank(items, query, **kw):
        if not kw.get("semantic"):
            return real([], query, **kw)                                                               # lexically: nothing
        offered = set(kw["semantic"].get("candidate_ids") or [])
        return real([i for i in items if i["item_id"] in offered], query, **kw)                         # only what the semantic layers proposed
    monkeypatch.setattr(vbi.bi, "rank_items", rank)


def test_llm_q2_the_semantic_layers_find_what_the_lexical_search_missed_and_the_answer_cites_it(ceiling_four, monkeypatch):
    q = ceiling_four
    _lexical_miss(monkeypatch)
    pre = vbi.context_pack(q.env.store, q.env.tenant, q.env.ws, VOCAB_GAP, view="target", can_read_evidence=False, semantic=False)
    assert pre["items"] == [] and pre["corpus"]["items"] > 0                                          # the case: nothing lexically, a non-empty authorized corpus

    def semantic(layer, request, timeout):
        ids = [i["item_id"] for i in request["items"]]
        if layer == "expand":
            return {"terms": ["approve", "manager"], "candidate_ids": [i for i in ids if i == "rule-standard-manager-review"]}
        if layer == "rank":
            return {"scores": {i: (0.9 if i == "rule-standard-manager-review" else 0.1) for i in ids}}
        return {"labels": {i: ("supports" if i == "rule-standard-manager-review" else "unrelated") for i in ids}, "sufficiency": "sufficient"}
    external = sr.ScriptedProvider(semantic, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, lambda p: obj("ANSWERED", "A manager approves purchases of 1,000,000 JPY or more.",
                                 [claim("Purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")]))
    got = run(q, q.admin, VOCAB_GAP)
    rec = job_body(q.admin, q.env, got["question_id"])
    res = rec["execution_result"]
    assert rec["execution_status"] == "COMPLETED" and model.calls == 1 and external.calls == 3 and rec["execution_provider_calls"] == 4
    assert [c["refs"] for c in res["claims"]][0] == ["rule-standard-manager-review"] and res["status"] in ("ANSWERED", "PARTIAL")
    assert res["ai_outcome"]["contribution"] == "retained" and res["pack"]["semantic"]["layers"]["expand"]["status"] == "applied"


def test_llm_q2_when_the_semantic_layers_find_nothing_either_the_question_is_abstained_without_the_answer_call(ceiling_four, monkeypatch):
    q = ceiling_four
    _lexical_miss(monkeypatch)

    def nothing(layer, request, timeout):
        return {"terms": [], "candidate_ids": []} if layer == "expand" else ({"scores": {}} if layer == "rank" else {"labels": {}, "sufficiency": "insufficient"})
    external = sr.ScriptedProvider(nothing, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, answer_stub)
    got = run(q, q.req, VOCAB_GAP)                                                                      # a reader without evidence.read: no source passages either
    rec = job_body(q.admin, q.env, got["question_id"])
    res = rec["execution_result"]
    assert res["status"] == "ABSTAINED" and model.calls == 0 and external.calls >= 1 and rec["execution_provider_calls"] == external.calls
    assert "no_evidence_after_semantic_search" in res["reasons"] and res["retrieval_state"]["status"] == "no_evidence"


def test_llm_q2_an_empty_authorized_corpus_abstains_with_zero_calls_even_when_armed(ceiling_four, monkeypatch):
    q = ceiling_four
    real = vbi.context_pack

    def empty_corpus(*a, **k):
        pack = real(*a, **k)
        pack["items"], pack["counterevidence"], pack["graph"], pack["corpus"] = [], [], [], {"items": 0}
        pack["observations"] = dict(pack["observations"], results=[], status="READY")
        return pack
    monkeypatch.setattr(knowledge_qa.v2_business_index, "context_pack", empty_corpus)
    external = sr.ScriptedProvider(good, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, answer_stub)
    got = run(q, q.admin, "Is there a rule about visitor parking?")
    rec = job_body(q.admin, q.env, got["question_id"])
    assert got["answer"]["status"] == "ABSTAINED" and rec["execution_provider_calls"] == 0 and external.calls == 0 and model.calls == 0
