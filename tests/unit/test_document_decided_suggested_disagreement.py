"""A disagreement a model suggested and a person settled (a RESOLVED `conflicting_facts` question with no stored `conflicts_with` relation) is shown by the documents
like any other: the business document names both readings, says a person decided and that the sources themselves are unchanged; the technical document names the
deciding rule and the question id. Nobody's decision is invented: without the decision (the question still OPEN, or the decided rule revoked) the pair is listed as
open. Pure (JOURNEY-V4-J3 A19 / documents, 2026-10-22)."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import document_ast as da

ROOT = pathlib.Path(__file__).resolve().parents[2]


def rule(rid, title, value, actor, authority="OBSERVED", status="VALIDATED", provenance=()):
    return {"rule_id": rid, "title": title, "authority": authority, "status": status, "actor_ref": "role-" + actor.lower().replace(" ", "-"),
            "condition": {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": value, "unit": "JPY"}]},
            "outcome": {"kind": "refer", "predicate": f"referred to {actor}"}, "exceptions": [], "scope": {}, "implementation_status": "not_required", "unknown_fields": [],
            "provenance": list(provenance), "source_bindings": [{"artifact_id": "art-" + rid, "revision_id": "1", "evidence_ref": "ev-" + rid}], "concept_ids": []}


POLICY = rule("rule-policy-capex", "Capital expenditure - is referred", 5000000, "Finance Controller")
CODE = rule("rule-code-capex", "CAPEX request - needs Finance", 2000000, "Finance")
DECIDED = rule("rule-code-capex-target", "CAPEX: Finance reviews from 2,000,000 JPY", 2000000, "Finance", authority="TARGET", status="CONFIRMED",
               provenance=[{"kind": "human_decision", "decision_id": "dec-1"}])
META = {"snapshot_id": "snap-1", "lifecycle_state": "ACCEPTED", "business_revision": 1, "source_revision_id": "src-1", "km_revision_id": "km-1", "decision_epoch": 1, "content_hash": "h"}
DECISION = {"decision_id": "dec-1", "kind": "answer_gap", "quote": "The code rule (2,000,000) is the business rule.", "body": {"gap_id": "gap-suggested", "summary": ["chose the code rule"]}}


def gap(status):
    return {"gap_id": "gap-suggested", "kind": "conflicting_facts", "status": status, "affected_rule_ids": ["rule-policy-capex", "rule-code-capex"],
            "question": "Do the two CAPEX thresholds conflict?", "priority": "critical", "evidence_refs": ["ev-rule-policy-capex", "ev-rule-code-capex"]}


def templates():
    return {m: da.template_from_text((ROOT / "config" / "document_templates" / f"{m}.v1.json").read_text(encoding="utf-8")) for m in da.MODES}


def test_a_settled_suggested_disagreement_shows_both_readings_the_choice_and_that_the_sources_are_unchanged():
    snap = {"snapshot": META, "rules": [POLICY, CODE, DECIDED], "relations": [], "gaps": [gap("RESOLVED")]}
    tpl = templates()
    business = da.build_document(snap, [DECISION], mode="business")
    assert [(c["edge_id"], c["decided_by"]) for c in business["conflicts"]] == [("gap-suggested", "rule-code-capex-target")]      # no relation: the question is the pair
    md = da.render_markdown(business, tpl["business"])
    assert "“CAPEX request - needs Finance” and “Capital expenditure - is referred” disagree. A person decided the business rule: “CAPEX: Finance reviews from 2,000,000 JPY”. The sources themselves are unchanged and still disagree." in md
    assert "5000000" in json.dumps(business["facts"]) and "2000000" in json.dumps(business["facts"])                               # both readings stay in the facts
    technical = da.render_markdown(da.build_document(snap, [DECISION], mode="technical"), tpl["technical"])
    assert "`gap-suggested`: `rule-code-capex` conflicts with `rule-policy-capex`; decided by `rule-code-capex-target`" in technical


def test_without_the_decision_or_before_the_answer_nobody_is_said_to_have_decided():
    tpl = templates()["business"]
    revoked = {"snapshot": META, "rules": [POLICY, CODE, dict(DECIDED, status="SUPERSEDED")], "relations": [], "gaps": [gap("RESOLVED")]}
    doc = da.build_document(revoked, [DECISION], mode="business")
    assert doc["conflicts"] and "decided_by" not in doc["conflicts"][0] and "A person has to decide which one is the business rule." in da.render_markdown(doc, tpl)
    still_open = {"snapshot": META, "rules": [POLICY, CODE], "relations": [], "gaps": [gap("OPEN")]}
    doc = da.build_document(still_open, [], mode="business")
    assert doc["conflicts"] == [] and [q["gap_id"] for q in doc["questions"]] == ["gap-suggested"]                                 # an unanswered suggestion is a question, not a disagreement record
    scoped = da.build_document({"snapshot": META, "rules": [POLICY, CODE, DECIDED], "relations": [], "gaps": [gap("RESOLVED")]}, [DECISION], mode="business", scope={"rule_ids": ["rule-policy-capex"]})
    assert [c["edge_id"] for c in scoped["conflicts"]] == ["gap-suggested"]                                                        # a scope naming one side still shows the pair
