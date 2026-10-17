"""BAS-02: round-trip and negative cases for the Final knowledge-loop contract (docs/contracts/FINAL_KNOWLEDGE_LOOP.md).

Pure contract tests. The expectations come from fixtures written by the test author
(tests/fixtures/final_knowledge_loop/); nothing here calls a model, a store or a worker.
"""
import copy
import hashlib
import json
import pathlib

import pytest

from core.change_intelligence.v2 import business_knowledge as bk
from core.kernel.serialize import canonical_dumps

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "final_knowledge_loop"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


KNOWLEDGE = _load("expected_knowledge.json")
QUESTIONS = _load("questions.json")
CHANGES = _load("changes.json")
EXAMPLES = _load("contract_examples.json")
RULE_IDS = set(CHANGES["rule_ids"])


def _code(fn, *args, **kw):
    with pytest.raises(bk.ContractError) as exc:
        fn(*args, **kw)
    return exc.value.code


# ---- fixtures are consistent with the sources they were written against -------------------------------------------------

def test_fixture_sources_unchanged():
    for item in _load("sources.json")["sources"]:
        data = (ROOT / item["path"]).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == item["sha256"], f"{item['path']} changed: re-check the expectations"


def test_set_sizes_match_the_plan():
    groups = [q["group"] for q in QUESTIONS["questions"]]
    assert len(groups) == 12
    assert {g: groups.count(g) for g in set(groups)} == {"paraphrase": 4, "multi_hop": 2, "exception_scope": 2, "unknown_conflict": 2, "current_vs_target": 2}
    kinds = {c["kind"] for c in CHANGES["changes"]}
    assert kinds == {"numeric", "actor", "condition", "scope", "exception", "multi_part"}  # the plan's six change types
    assert len(CHANGES["changes"]) == 7


def test_conflict_fixture_has_the_two_disagreeing_capex_values():
    rules = {r["rule_id"]: r for r in KNOWLEDGE["observed_rules"]}
    def threshold(rule):
        return next(leaf["value"] for leaf in rule["condition"]["all"] if leaf["field"] == "request.net_amount_jpy")
    assert threshold(rules["rule-capex-finance-config"]) == 2000000
    assert threshold(rules["rule-capex-finance-policy"]) == 5000000
    assert any(g["kind"] == "conflicting_facts" and g["priority"] == "critical" for g in KNOWLEDGE["expected_gaps"])


# ---- round trip -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rule", KNOWLEDGE["observed_rules"], ids=lambda r: r["rule_id"])
def test_observed_rules_validate_and_roundtrip(rule):
    bk.validate_business_rule(rule)
    back = json.loads(canonical_dumps(rule))
    assert back == rule
    bk.validate_business_rule(back)


def test_other_shapes_validate_and_roundtrip():
    bk.validate_business_rule(EXAMPLES["target_rule"])
    bk.validate_relation(EXAMPLES["relation"])
    bk.validate_gap(EXAMPLES["gap"])
    bk.validate_snapshot_pin(EXAMPLES["snapshot_pin"])
    bk.validate_document_request(EXAMPLES["document_request"])
    bk.validate_document_artifact(EXAMPLES["document_artifact"])
    for key in ("target_rule", "relation", "gap", "document_request", "document_artifact"):
        assert json.loads(canonical_dumps(EXAMPLES[key])) == EXAMPLES[key]


def test_every_expected_change_operation_validates_against_the_rule_set():
    count = 0
    for change in CHANGES["changes"]:
        for op in change["expected_operations"]:
            bk.validate_change_operation(op, known_rule_ids=RULE_IDS)
            count += 1
    assert count == 9
    multi = next(c for c in CHANGES["changes"] if c["kind"] == "multi_part")
    bk.validate_change_set({"operations": multi["expected_operations"], "expected_snapshot": EXAMPLES["snapshot_pin"]}, known_rule_ids=RULE_IDS)
    # every part of the multi-part sentence is its own operation (nothing silently dropped)
    assert [o["kind"] for o in multi["expected_operations"]] == ["set_value", "set_actor", "set_exception"]


def test_target_does_not_overwrite_observed():
    observed = {r["rule_id"]: r for r in KNOWLEDGE["observed_rules"]}
    target = EXAMPLES["target_rule"]
    assert target["authority"] == "TARGET" and target["rule_id"] not in observed
    assert all(observed[i]["authority"] == "OBSERVED" for i in KNOWLEDGE["after_user_answers"]["expected_target_rule"]["observed_rules_unchanged"])
    # a TARGET without a human decision is refused, and an AI-inferred rule can never be CONFIRMED
    bad = copy.deepcopy(target)
    bad["provenance"] = [{"kind": "ai_inference", "model_run_id": "run-1"}]
    assert _code(bk.validate_business_rule, bad) == "forbidden_provenance"


# ---- condition grammar ----------------------------------------------------------------------------------------------------

def test_condition_equivalent_spellings_normalise_equal():
    a = {"all": [{"field": "a.x", "op": "eq", "value": "1"}, {"field": "a.y", "op": "in", "value": ["b", "a"]}]}
    b = {"all": [{"field": "a.y", "op": "in", "value": ["a", "b"]}, {"field": "a.x", "op": "eq", "value": "1"}]}
    assert bk.normalize_condition(a) == bk.normalize_condition(b)
    assert bk.normalize_condition(a) != bk.normalize_condition({"any": a["all"]})  # AND is not OR


@pytest.mark.parametrize("expr, code", [
    ({"field": "a.x", "op": "regex", "value": "x"}, "unsupported_expression"),              # operator outside the grammar
    ({"field": "a.x", "op": "gte", "value": 1.5}, "unsupported_expression"),                # floats refused
    ({"field": "a.x", "op": "gte", "value": True}, "unsupported_expression"),               # bool is not a number
    ({"field": "a.x", "op": "gte", "value": "abc"}, "unsupported_expression"),
    ({"field": "a.x", "op": "gte", "value": "NaN"}, "unsupported_expression"),
    ({"field": "a.x", "op": "in", "value": []}, "unsupported_expression"),
    ({"all": []}, "unsupported_expression"),
    ({"all": [{"field": "a.x", "op": "eq", "value": "1"}], "any": []}, "unsupported_expression"),
    ({"field": "a.x", "op": "eq", "value": "1", "python": "os.system('x')"}, "unsupported_expression"),  # no executable payloads
    ({"field": "A X", "op": "eq", "value": "1"}, "invalid_field"),
    ("amount >= 1000000", "invalid_field"),                                                  # free text is not an expression
])
def test_unsupported_conditions_are_refused_not_coerced(expr, code):
    assert _code(bk.validate_condition, expr) == code


def test_condition_limits():
    deep = {"field": "a.x", "op": "eq", "value": "1"}
    for _ in range(bk.MAX_CONDITION_DEPTH):
        deep = {"not": deep}
    assert _code(bk.validate_condition, deep) == "limit_exceeded"
    wide = {"all": [{"field": "a.x", "op": "eq", "value": str(i)} for i in range(bk.MAX_CONDITION_LEAVES + 1)]}
    assert _code(bk.validate_condition, wide) == "limit_exceeded"


# ---- rules / relations / gaps ---------------------------------------------------------------------------------------------

def test_rule_negative_cases():
    base = KNOWLEDGE["observed_rules"][0]
    def mutated(**changes):
        r = copy.deepcopy(base)
        r.update(changes)
        return r
    assert _code(bk.validate_business_rule, mutated(contract_version="fkl/9")) == "unsupported_contract_version"
    assert _code(bk.validate_business_rule, mutated(status="CONFIRMED")) == "forbidden_provenance"  # source text alone never confirms
    assert _code(bk.validate_business_rule, mutated(provenance=[])) == "missing_provenance"
    assert _code(bk.validate_business_rule, mutated(provenance=[{"kind": "generated_document", "artifact_id": "doc-1"}])) == "forbidden_provenance"
    assert _code(bk.validate_business_rule, mutated(rule_id="Rule 1")) == "invalid_field"
    assert _code(bk.validate_business_rule, mutated(implementation_status="done")) == "invalid_field"
    assert _code(bk.validate_business_rule, mutated(surprise=1)) == "invalid_field"
    extra = copy.deepcopy(base)
    del extra["actor_ref"]
    assert _code(bk.validate_business_rule, extra) == "invalid_field"


def test_unknown_condition_and_actor_are_allowed_but_explicit():
    emergency = next(r for r in KNOWLEDGE["observed_rules"] if r["rule_id"] == "rule-emergency-routing")
    assert emergency["condition"] is None and emergency["actor_ref"] is None and emergency["status"] != "CONFIRMED"
    bk.validate_business_rule(emergency)


def test_relation_and_gap_negative_cases():
    rel = dict(EXAMPLES["relation"], provenance_refs=[])
    assert _code(bk.validate_relation, rel) == "missing_provenance"
    assert _code(bk.validate_relation, dict(EXAMPLES["relation"], type="causes")) == "invalid_field"
    assert bk.validate_relation(dict(rel, status="PROPOSED")) is None  # an unproven edge may exist, only as PROPOSED
    assert _code(bk.validate_gap, dict(EXAMPLES["gap"], affected_rule_ids=[])) == "invalid_field"


def test_gap_ids_are_stable_and_distinguish_kinds():
    a = bk.gap_id_for("missing_actor", ["rule-b", "rule-a"], "emergency")
    assert a == bk.gap_id_for("missing_actor", ["rule-a", "rule-b"], "emergency")  # re-run: same question, not a duplicate
    assert a != bk.gap_id_for("missing_exception", ["rule-a", "rule-b"], "emergency")
    bk.validate_gap(dict(EXAMPLES["gap"], gap_id=a))


# ---- change operations ------------------------------------------------------------------------------------------------------

def _op(**changes):
    op = copy.deepcopy(CHANGES["changes"][2]["expected_operations"][0])
    op.update(changes)
    return op


def test_change_operation_negative_cases():
    assert _code(bk.validate_change_operation, _op(rule_id="rule-invented"), known_rule_ids=RULE_IDS) == "invented_rule_id"
    assert _code(bk.validate_change_operation, _op(kind="schedule_effective_date")) == "unsupported_operation"
    assert _code(bk.validate_change_operation, _op(kind="rewrite_everything")) == "invalid_field"
    assert _code(bk.validate_change_operation, _op(user_quote="  ")) == "invalid_field"
    assert _code(bk.validate_change_operation, _op(desired="Finance")) == "invalid_field"          # actor must be a role ref
    assert _code(bk.validate_change_operation, _op(kind="set_value", desired={"value": True, "unit": "JPY"})) == "unsupported_expression"
    assert _code(bk.validate_change_operation, _op(kind="set_value", desired={"value": 5, "unit": ""})) == "invalid_field"  # unit travels with the number
    assert _code(bk.validate_change_operation, _op(kind="add_or_change_condition", desired={"field": "a.x", "op": "like", "value": "x"})) == "unsupported_expression"


def test_ambiguous_rule_needs_clarification_with_candidates():
    assert _code(bk.validate_change_operation, _op(rule_id=None)) == "ambiguous_rule"
    assert _code(bk.validate_change_operation, _op(rule_id=None, status="needs_clarification", candidate_rule_ids=["rule-a"])) == "ambiguous_rule"
    bk.validate_change_operation(_op(rule_id=None, status="needs_clarification", candidate_rule_ids=["rule-capex-finance-config", "rule-capex-finance-policy"]))


def test_unsupported_status_requires_a_reason():
    assert _code(bk.validate_change_operation, _op(status="unsupported")) == "invalid_field"
    bk.validate_change_operation(_op(status="unsupported", reason="no executable mapping for this actor"))


def test_change_set_limits_duplicates_and_stale_pin():
    ops = [_op(op_id=f"op-{i}") for i in range(bk.MAX_OPERATIONS_PER_REQUEST)]
    pin = EXAMPLES["snapshot_pin"]
    bk.validate_change_set({"operations": ops, "expected_snapshot": pin}, known_rule_ids=RULE_IDS)
    assert _code(bk.validate_change_set, {"operations": ops + [_op(op_id="op-over")], "expected_snapshot": pin}) == "limit_exceeded"
    assert _code(bk.validate_change_set, {"operations": [_op(), _op()], "expected_snapshot": pin}) == "invalid_field"
    assert _code(bk.validate_change_set, {"operations": [], "expected_snapshot": pin}) == "invalid_field"
    assert _code(bk.check_snapshot_current, pin, dict(pin, decision_epoch=4)) == "stale_snapshot"
    assert _code(bk.check_snapshot_current, pin, dict(pin, source_revision_id="source-5")) == "stale_snapshot"
    assert bk.check_snapshot_current(pin, dict(pin)) is None


# ---- documents ----------------------------------------------------------------------------------------------------------------

def _key(**over):
    args = dict(tenant_id="t1", workspace_id="w1", snapshot_id="business-12", scope={"concept_ids": ["b", "a"]}, mode="business", locale="en",
                template_version="1", generator_version="1", schema_version="fkl/1")
    args.update(over)
    return bk.artifact_key(**args)


def test_artifact_key_depends_on_every_part_except_selection_order():
    base = _key()
    assert base == _key(scope={"concept_ids": ["a", "b", "a"]})  # order and duplicates never change the key
    for over in ({"tenant_id": "t2"}, {"workspace_id": "w2"}, {"snapshot_id": "business-13"}, {"scope": {"concept_ids": ["a"]}}, {"mode": "technical"},
                 {"locale": "ja"}, {"template_version": "2"}, {"generator_version": "2"}, {"model_version": "m"}, {"redaction_scope": "restricted"}):
        assert _key(**over) != base, over
    assert _code_value_error(lambda: _key(mode="markdown"))


def _code_value_error(fn):
    with pytest.raises(bk.ContractError):
        fn()
    return True


def test_document_negative_cases():
    req = EXAMPLES["document_request"]
    assert _code(bk.validate_document_request, dict(req, mode="pdf")) == "invalid_field"
    assert _code(bk.validate_document_request, {k: v for k, v in req.items() if k != "snapshot_id"}) == "invalid_field"
    art = EXAMPLES["document_artifact"]
    assert _code(bk.validate_document_artifact, dict(art, content_hash="md5:abc")) == "invalid_field"
    assert _code(bk.validate_document_artifact, dict(art, freshness="latest")) == "invalid_field"
    assert _code(bk.validate_document_artifact, dict(art, rule_count=bk.MAX_RULES_PER_ARTIFACT + 1)) == "limit_exceeded"
    assert _code(bk.validate_document_artifact, dict(art, size_bytes=bk.MAX_ARTIFACT_BYTES + 1)) == "limit_exceeded"


# ---- legacy candidate mapper (KNO-01 backfill, pure) ----------------------------------------------------------------------------

def _cand(**over):
    cand = {"candidate_id": "cand-AB12", "subject": "Manager approval", "predicate": "threshold", "value": 1000000, "unit": "JPY", "operator": ">=",
            "applicability": "RECOGNIZED", "modality": "must", "scope": {"order_type": "standard"}, "exceptions": ["emergency"], "outcome": None,
            "source_revision_id": "src-1", "artifact_revision_id": "art-1", "model_run_id": "run-1", "evidence": [{"locator": "k: 1", "quote": "q"}]}
    cand.update(over)
    return cand


def test_legacy_mapper_keeps_ids_and_evidence_and_never_promotes():
    rule = bk.observed_rule_from_candidate(_cand())
    bk.validate_business_rule(rule)
    assert rule["status"] == "VALIDATED" and rule["authority"] == "OBSERVED" and rule["candidate_ids"] == ["cand-AB12"]
    assert rule["condition"] == {"field": "manager_approval", "op": "gte", "value": 1000000, "unit": "JPY"}
    assert rule["unknown_fields"] == ["actor_ref"] and rule["actor_ref"] is None
    assert {"kind": "ai_inference", "model_run_id": "run-1"} in rule["provenance"]
    assert bk.legacy_rule_id("cand-ab12") == "rule-cand-ab12" and bk.legacy_rule_id("Cand AB12") != bk.legacy_rule_id("cand-ab12")


def test_legacy_mapper_refuses_to_invent():
    assert bk.observed_rule_from_candidate(_cand(operator=None))["condition"] is None
    assert bk.observed_rule_from_candidate(_cand(value={"x": 1}))["condition"] is None
    assert bk.observed_rule_from_candidate(_cand(applicability="UNKNOWN"))["status"] == "DRAFT"
    assert bk.observed_rule_from_candidate(_cand(value=1.5))["condition"]["value"] == "1.5"
    assert _code(bk.observed_rule_from_candidate, _cand(evidence=[])) == "missing_provenance"
    confirmed = bk.observed_rule_from_candidate(_cand(), confirmation_decision_id="decision-9")
    assert confirmed["status"] == "CONFIRMED"
    bk.validate_business_rule(confirmed)
