"""KNO-02: extraction keeps business meaning (actor, AND/OR/NOT conditions, exceptions, scope) through the real route and worker.

Provider: counting stub (authored payloads, no model call, no spend). The stub payloads are model-shaped inputs written from the Harbor policy text; they say nothing
about how well a real model extracts (that is REL-03, live, after budget approval). Needs the DB fixture of tests/pitch_brief (skipped otherwise, NOT a pass)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters.v2_business import backfill_observed_snapshot, business_view
from infra.worker import extraction
from tests.pitch_brief.chain import POLICY
from tests.pitch_brief.conftest import EXTRACT_MODEL, create_job, drain, import_harbor, job_body

Q_MANAGER = ("Standard requisitions with a net goods value of **1,000,000 JPY or greater** require documented "
             "sign-off from an authorized **Manager**.")
Q_RISK = ("Any requisition referencing a vendor classified with HIGH risk requires explicit authorization by a **Director**, "
          "regardless of the expense category, process type, or monetary amount.")
Q_CAPEX = ("Capital equipment and long-term asset acquisitions of **2,000,000 JPY or greater** require formal "
           "review and authorization by the **Finance Department**.")
Q_OPEX = "Operational expenditures (OPEX) do not fall under the Finance capital committee review and are routed based on general managerial spending limits."
Q_MAKER = "Requisitioners are strictly prohibited from approving their own purchase requisitions."

AMOUNT = {"field": "request.net_amount_jpy", "op": "gte", "unit": "JPY"}


def cand(subject, predicate, quote, **over):
    base = {"subject": subject, "predicate": predicate, "value": None, "unit": None, "modality": "must", "scope": {"process": "standard"},
            "exceptions": [], "outcome": "review_required", "quotes": [quote], "rationale": "stub payload authored from the policy text"}
    base.update(over)
    return base


SIX_KINDS = [
    cand("Manager approval", "required_from", Q_MANAGER, value=1000000, unit="JPY", operator=">=", actor="Manager",
         condition=dict(AMOUNT, value=1000000)),                                                                                  # numeric threshold + actor
    cand("High-risk supplier", "needs_authorization", Q_RISK, actor="Director", scope={"supplier": "high risk"},
         condition={"field": "supplier.risk", "op": "eq", "value": "HIGH"}),                                                      # actor without any number
    cand("Finance review", "capex_review", Q_CAPEX, actor="Finance Department", scope={"category": "CAPEX"},
         condition={"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, dict(AMOUNT, value=2000000)]}),          # AND
    cand("Finance review", "opex_not_reviewed", Q_OPEX, actor="Finance Department", scope={"category": "OPEX"}, modality="observed",
         exceptions=["OPEX is routed by general managerial limits"],
         condition={"not": {"field": "request.category", "op": "eq", "value": "OPEX"}}),                                           # exception + NOT
    cand("Maker-checker", "no_self_approval", Q_MAKER, actor=None, scope={"all": "requisitions"},
         condition={"not": {"field": "approver.is_requester", "op": "eq", "value": "true"}}),                                     # negation
    cand("Manager approval", "required_from_or_new_supplier", Q_MANAGER, actor="Manager",
         condition={"any": [dict(AMOUNT, value=1000000), {"field": "supplier.is_new", "op": "eq", "value": "true"}]}),             # OR, same words, different rule
]


class Stub:
    def __init__(self, items):
        self.items, self.calls = items, 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        return SimpleNamespace(text=json.dumps({"candidates": self.items}), model_version=EXTRACT_MODEL, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=30, thoughts_token_count=None, total_token_count=70))


def run_extraction(env, monkeypatch, items, stub_class=Stub):
    admin, req = env.admin_client(), env.req_client()
    arts, _ = import_harbor(env, admin)
    drain(env)
    stub = stub_class(items)
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    jid, r = create_job(req, env, "extraction", {"artifact_id": arts[POLICY], "source_role": "normative"})
    assert r.status_code == 200, r.text
    assert admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    return stub, job_body(admin, env, jid)


def test_six_rule_kinds_keep_actor_condition_exception_and_stay_separate(env, monkeypatch):
    stub, body = run_extraction(env, monkeypatch, SIX_KINDS)
    assert body["execution_status"] == "COMPLETED" and stub.calls == 1, body
    cands = body["execution_result"]["candidates"]
    assert len(cands) == 6 and body["execution_result"]["rejected"] == []          # the two Manager statements share a quote but differ in condition: not merged
    by_pred = {c["predicate"]: c for c in cands}
    assert by_pred["required_from"]["actor"] == "role-manager" and by_pred["required_from"]["condition"]["op"] == "gte"
    assert by_pred["needs_authorization"]["actor"] == "role-director" and by_pred["needs_authorization"]["value"] is None
    assert "all" in by_pred["capex_review"]["condition"] and by_pred["capex_review"]["actor"] == "role-finance-department"
    assert by_pred["opex_not_reviewed"]["exceptions"] == ["OPEX is routed by general managerial limits"] and "not" in by_pred["opex_not_reviewed"]["condition"]
    assert "not" in by_pred["no_self_approval"]["condition"] and by_pred["no_self_approval"]["actor"] is None
    assert "any" in by_pred["required_from_or_new_supplier"]["condition"]
    assert all(c["applicability"] == "RECOGNIZED" and c["business_fields_rejected"] is None for c in cands)
    rules = {c["predicate"]: bk.observed_rule_from_candidate(c) for c in cands}      # the same candidates become valid, never-confirmed business rules
    for rule in rules.values():
        bk.validate_business_rule(rule)
        assert rule["status"] == "VALIDATED" and rule["authority"] == "OBSERVED"
    assert rules["required_from"]["actor_ref"] == "role-manager" and rules["required_from"]["unknown_fields"] == []
    assert rules["no_self_approval"]["unknown_fields"] == ["actor_ref"]


def test_durable_snapshot_from_the_real_extraction_keeps_the_meaning(env, monkeypatch):
    run_extraction(env, monkeypatch, SIX_KINDS[:3])
    out = backfill_observed_snapshot(env.store, env.tenant, env.ws, snapshot_id="snap-x", actor_id=env.admin, idempotency_key="x1")
    rules = business_view(env.store, env.tenant, env.ws)["pending"]["rules"]
    capex = next(r for r in rules if r["outcome"]["predicate"] == "capex_review")
    assert capex["condition"]["all"][0]["value"] == "CAPEX" and capex["actor_ref"] == "role-finance-department"
    assert out["skipped"] == [] and all(r["status"] != "CONFIRMED" for r in rules)


def test_bad_business_fields_are_disclosed_and_never_coerced(env, monkeypatch):
    items = [
        cand("Manager approval", "regex_condition", Q_MANAGER, actor="Manager", condition={"field": "a.x", "op": "regex", "value": "x"}),
        cand("Director", "bad_actor", Q_RISK, actor="!!!", condition={"field": "supplier.risk", "op": "eq", "value": "HIGH"}),
        cand("Finance review", "float_value", Q_CAPEX, condition={"field": "request.net_amount_jpy", "op": "gte", "value": 2000000.5}),
        cand("Finance review", "code_in_condition", Q_CAPEX, condition={"field": "a.x", "op": "eq", "value": "1", "python": "os.system('x')"}),
    ]
    _, body = run_extraction(env, monkeypatch, items)
    cands = {c["predicate"]: c for c in body["execution_result"]["candidates"]}
    assert len(cands) == 4                                                        # the statement itself is kept (its quote is real)
    assert cands["regex_condition"]["condition"] is None and "unsupported_expression" in cands["regex_condition"]["business_fields_rejected"]
    assert cands["regex_condition"]["actor"] == "role-manager"                      # a good field next to a bad one survives
    assert cands["bad_actor"]["actor"] is None and "actor_not_a_name" in cands["bad_actor"]["business_fields_rejected"] and cands["bad_actor"]["condition"]["op"] == "eq"
    assert cands["float_value"]["condition"] is None and cands["code_in_condition"]["condition"] is None
    rule = bk.observed_rule_from_candidate(cands["regex_condition"])
    assert rule["status"] == "VALIDATED" and "condition" in rule["unknown_fields"]  # a gap, not a guess


def test_fabricated_quote_is_rejected(env, monkeypatch):
    items = [cand("Invented", "rule", "The Chairman must approve every purchase.", actor="Chairman"), SIX_KINDS[0]]
    stub, body = run_extraction(env, monkeypatch, items)
    result = body["execution_result"]
    assert [c["predicate"] for c in result["candidates"]] == ["required_from"] and stub.calls == 1
    assert result["rejected"][0]["reason"] == "no_verifiable_citation"


def test_partial_coverage_is_disclosed(env, monkeypatch):
    monkeypatch.setattr(extraction, "_MAX_CONTEXT_CHUNKS", 1)
    _, body = run_extraction(env, monkeypatch, [SIX_KINDS[0]])
    cov = body["execution_result"]["coverage"]
    assert cov["truncated"] is True and cov["chunks_used"] == 1 and cov["chunks_total"] > 1


def test_prompt_states_the_business_fields_and_keeps_source_untrusted(env, monkeypatch):
    seen = {}

    class Spy(Stub):
        def _gen(self, **kw):
            seen["prompt"] = kw["contents"]
            return super()._gen(**kw)
    run_extraction(env, monkeypatch, [], Spy)
    p = seen["prompt"]
    assert '"actor"' in p and '"condition"' in p and "UNTRUSTED DATA" in p and "use null instead of guessing" in p
