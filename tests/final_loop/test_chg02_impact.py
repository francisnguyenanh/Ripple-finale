"""CHG-02: impact of a confirmed business change, from the pinned accepted knowledge only (pure module + real route/store). No model is used or called.

The rule fixtures are the author-written scenario S1 files plus small synthetic rules built here; nothing proves that the graph is complete for a real repository.
"""
from __future__ import annotations

import copy
import json

import pytest

from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2 import business_impact as bi
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_ACTOR, OP_VALUE, model_obj, post, settle, understand
from tests.final_loop.test_qry02_retrieval import KNOWLEDGE
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)

PIN = {"snapshot_id": "s", "business_revision": 1, "source_revision_id": "r", "km_revision_id": "k", "decision_epoch": 0}
RULES = {r["rule_id"]: r for r in KNOWLEDGE["observed_rules"]}


def op(kind, rule_id, before, desired, n=1):
    return {"op_id": f"op-{n}", "kind": kind, "rule_id": rule_id, "before": before, "desired": desired, "user_quote": "q", "evidence_refs": [], "status": "confirmed"}


def make(rule_id, concept, *, actor="role-x", value=None, bindings=True, status="VALIDATED", authority="OBSERVED"):
    r = copy.deepcopy(RULES["rule-standard-manager-review"])
    r.update(rule_id=rule_id, concept_ids=[concept], actor_ref=actor, title=f"Title of {rule_id}", status=status, authority=authority)
    r["condition"] = {"field": "request.net_amount_jpy", "op": "gte", "value": value if value is not None else 7, "unit": "JPY"}
    r["source_bindings"] = [{"artifact_id": f"art-{rule_id}", "revision_id": "rev-1", "evidence_ref": f"{rule_id}.yaml#x"}] if bindings else []
    return r


def report(ops, rules, relations=None, **kw):
    rules = {r["rule_id"]: r for r in rules}
    return bi.build_report(ops, rules, bg.derive_relations(rules.values()) if relations is None else relations, pin=PIN, **kw)


VALUE = op("set_value", "rule-standard-manager-review", {"value": 1000000, "unit": "JPY"}, {"value": 1500000, "unit": "JPY"})
ACTOR = op("set_actor", "rule-high-risk-director", "role-director", "role-finance-controller")


def test_actor_change_finds_the_changed_rule_related_rules_files_and_code_recommendations():
    rep = report([ACTOR], list(RULES.values()))
    by = {i["rule_id"]: i for i in rep["affected_rules"]}
    assert by["rule-high-risk-director"]["link"] == "direct" and by["rule-high-risk-director"]["severity"] == "HIGH" and rep["write_scope"] == ["rule-high-risk-director"]
    assert any(i["link"] == "inferred_dependency" and i["severity"] == "LOW" for i in rep["affected_rules"])              # shared concept is a lead, not a fact
    paths = {f["path"]: f for f in rep["file_inventory"]}
    assert paths and all(f["status"] == "not_checked" for f in rep["file_inventory"])                                      # no artifact list was given: not checked, not "missing"
    assert any(a["kind"].startswith("recommend_") and "nothing is changed or run" in a["text"] for a in rep["proposed_actions"])
    assert rep["measured"] == [] and rep["ai_dependencies"]["status"] == "not_run" and all(s["status"] == "NOT_SIMULATED" for s in rep["simulation"])
    assert "not “no impact”" in rep["statement"]


def test_file_kinds_decide_the_recommendation_and_only_changed_rules_files_are_to_change():
    rep = report([op("set_value", "rule-capex-finance-config", {"value": 2000000, "unit": "JPY"}, {"value": 3000000, "unit": "JPY"})], list(RULES.values()))
    kinds = {f["path"]: f["kind"] for f in rep["file_inventory"]}
    assert kinds["routing.py"] == "code" and kinds["approval.yaml"] == "config" and kinds["procurement-policy.md"] == "document"
    to_change = {f["path"] for f in rep["file_inventory"] if f["role"] == "to_change"}
    assert to_change == {"approval.yaml", "routing.py"}
    texts = [a["text"] for a in rep["proposed_actions"] if a["kind"] == "recommend_code"]
    assert texts and texts[0].startswith("Review the code at routing.py")


def test_unrelated_rule_with_the_same_number_is_listed_as_not_changed():
    twin = make("rule-twin", "concept-other", value=1000000)
    rep = report([VALUE], list(RULES.values()) + [twin])
    assert "rule-twin" not in rep["write_scope"] and "rule-twin" not in {i["rule_id"] for i in rep["affected_rules"]}
    assert any("same number" in e["text"] and "not changed" in e["text"] for e in rep["excluded"])
    assert rep["write_scope"] == ["rule-standard-manager-review"]


def test_confirmed_links_are_told_apart_from_inferred_ones():
    a, b = make("rule-a", "concept-c1", actor="role-a"), make("rule-b", "concept-c1", actor="role-b")
    rules = {r["rule_id"]: r for r in (a, b)}
    edges = bg.derive_relations(rules.values())
    confirmed = [dict(e, status="CONFIRMED") for e in edges]
    inferred = report([op("set_value", "rule-a", {"value": 7, "unit": "JPY"}, {"value": 9, "unit": "JPY"})], [a, b], edges)
    sure = report([op("set_value", "rule-a", {"value": 7, "unit": "JPY"}, {"value": 9, "unit": "JPY"})], [a, b], confirmed)
    assert {i["rule_id"]: i["link"] for i in inferred["affected_rules"]}["rule-b"] == "inferred_dependency"
    assert {i["rule_id"]: (i["link"], i["severity"]) for i in sure["affected_rules"]}["rule-b"] == ("confirmed_link", "MEDIUM")


def test_a_conflicting_rule_is_counterevidence_in_the_read_scope_never_written():
    a, b = make("rule-a", "concept-c1", actor="role-a", value=5), make("rule-b", "concept-c1", actor="role-a", value=9)
    rules = {r["rule_id"]: r for r in (a, b)}
    edges = bg.derive_relations(rules.values())
    assert any(e["type"] == "conflicts_with" for e in edges)
    rep = report([op("set_value", "rule-a", {"value": 5, "unit": "JPY"}, {"value": 6, "unit": "JPY"})], [a, b], edges)
    assert rep["write_scope"] == ["rule-a"] and rep["counterevidence_rule_ids"] == ["rule-b"]
    assert {i["rule_id"]: (i["link"], i["severity"]) for i in rep["affected_rules"]}["rule-b"] == ("counterevidence", "HIGH")


def test_unknown_is_never_reported_as_no_impact():
    bare = make("rule-bare", "concept-c1", bindings=False)
    rep = report([op("set_value", "rule-bare", {"value": 7, "unit": "JPY"}, {"value": 9, "unit": "JPY"})], [bare])
    assert rep["file_inventory"] == [] and rep["coverage"]["complete"] is False and rep["coverage"]["unknown_items"] == 1
    assert rep["gaps"][0]["kind"] == "no_source_mapping" and "unknown (not “none”)" in rep["gaps"][0]["text"]
    assert rep["affected_rules"][0]["unknown"] is True


def test_missing_file_stale_edge_cycle_and_fanout_cap():
    a, b = make("rule-a", "concept-c1"), make("rule-b", "concept-c1")
    one = op("set_value", "rule-a", {"value": 7, "unit": "JPY"}, {"value": 9, "unit": "JPY"})
    rep = report([one], [a, b], known_artifacts={"some-other-artifact"})
    assert {f["status"] for f in rep["file_inventory"]} == {"missing_file"} and any(g["kind"] == "missing_file" for g in rep["gaps"])
    edges = bg.derive_relations({r["rule_id"]: r for r in (a, b)}.values())
    ghost = {"edge_id": "edge-ghost", "from": "concept-c1", "to": "rule-gone", "type": "defines", "status": "PROPOSED", "provenance_refs": []}
    cyc = [{"edge_id": "edge-c1", "from": "rule-a", "to": "rule-b", "type": "depends_on", "status": "PROPOSED", "provenance_refs": []},
           {"edge_id": "edge-c2", "from": "rule-b", "to": "rule-a", "type": "depends_on", "status": "PROPOSED", "provenance_refs": []}]
    rep = report([one], [a, b], edges + [ghost] + cyc)
    assert any(g["kind"] == "stale_edge" and g["ref"] == "rule-gone" for g in rep["gaps"]) and {i["rule_id"] for i in rep["affected_rules"]} == {"rule-a", "rule-b"}   # a cycle ends
    many = [make(f"rule-m{n:03d}", "concept-c1", actor=f"role-m{n}") for n in range(bi.LIMITS["max_nodes"] + 20)]
    rep = report([one], [a] + many)
    assert any(g["kind"] == "graph_traversal_limited" for g in rep["gaps"]) and rep["coverage"]["complete"] is False
    assert len(rep["affected_rules"]) <= bi.LIMITS["max_nodes"]


def test_unknown_rule_in_an_operation_is_refused():
    with pytest.raises(ValueError, match="unknown_rule"):
        report([op("set_value", "rule-nope", {"value": 1, "unit": "JPY"}, {"value": 2, "unit": "JPY"})], [make("rule-a", "c")])


def test_same_input_gives_the_same_report_hash_and_a_changed_operation_changes_it():
    one = report([ACTOR], list(RULES.values()))
    again = report([ACTOR], list(RULES.values()))
    other = report([op("set_actor", "rule-high-risk-director", "role-director", "role-finance")], list(RULES.values()))
    assert one["report_hash"] == again["report_hash"] != other["report_hash"]


def test_rendering_hides_files_and_ids_without_evidence_rights():
    rep = report([ACTOR], list(RULES.values()))
    plain = bi.render(rep, mode="business", can_read_evidence=False)
    blob = json.dumps(plain)
    assert plain["files"] == [] and plain["files_withheld"] == len(rep["file_inventory"]) > 0 and "rule-" not in blob and ".yaml" not in blob and ".py" not in blob
    assert all(not t.startswith(("Review the code", "Update the setting")) for t in plain["proposed_actions"]) and "technical" not in plain
    full = bi.render(rep, mode="technical", can_read_evidence=True)
    assert full["files"] and full["technical"]["write_scope"] == ["rule-high-risk-director"] and full["technical"]["files"] and full["technical"]["report_hash"] == rep["report_hash"]
    tech_plain = bi.render(rep, mode="technical", can_read_evidence=False)
    assert "files" not in tech_plain["technical"] and tech_plain["files"] == []


# ---- real route and store ---------------------------------------------------------------------------------------------------------------------------

def confirmed(q, text, ops, client=None):
    client = client or q.req
    got, model = understand(q, text, model_obj(ops=ops), client=client, mode="business")
    r = client.post(f"/v2/business/changes/{got['change_id']}/confirm", json={"workspace_id": q.env.ws, "preview_hash": got["understanding"]["preview_hash"]})
    assert r.status_code == 200, r.text
    return got["change_id"], model


def impact(q, client, cid, mode="business"):
    return client.get(f"/v2/business/changes/{cid}/impact", params={"workspace_id": q.env.ws, "mode": mode})


def test_impact_route_needs_a_confirmed_request_uses_no_model_and_follows_permissions(qa_env):
    q = qa_env
    got, model = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]))
    assert impact(q, q.req, got["change_id"]).status_code == 409 and impact(q, q.req, got["change_id"]).json()["code"] == "not_confirmed"
    r = q.req.post(f"/v2/business/changes/{got['change_id']}/confirm", json={"workspace_id": q.env.ws, "preview_hash": got["understanding"]["preview_hash"]})
    assert r.status_code == 200
    calls = model.calls
    ok = impact(q, q.req, got["change_id"])
    assert ok.status_code == 200 and model.calls == calls                                                              # computed, never asked of a model
    body = ok.json()
    assert body["knowledge_moved"] is False and body["affected_rules"][0]["link"] == "direct" and body["measured"] == [] and "rule-" not in json.dumps(body)
    tech = impact(q, q.admin, got["change_id"], "technical").json()
    assert tech["technical"]["write_scope"] == ["rule-standard-manager-review"] and tech["technical"]["pin"]["business_revision"] == 1
    assert tech["technical"]["report_hash"] == impact(q, q.admin, got["change_id"], "technical").json()["technical"]["report_hash"]
    assert impact(q, q.req, "chg-nonexistent").status_code == 404 and impact(q, q.req, got["change_id"], "x").status_code == 400
    anonymous = q.env.admin_client()
    anonymous.cookies.clear()
    assert anonymous.get(f"/v2/business/changes/{got['change_id']}/impact", params={"workspace_id": q.env.ws}).status_code in (401, 403)


def test_impact_of_an_actor_change_through_the_route_lists_related_rules_and_marks_moved_knowledge(qa_env):
    q = qa_env
    cid, _ = confirmed(q, "Purchases from high-risk suppliers should be approved by the Finance Controller instead of the Operations Director.", [OP_ACTOR])
    tech = impact(q, q.admin, cid, "technical").json()
    links = {a["rule_id"]: a["link"] for a in tech["technical"]["affected"]}
    assert links["rule-high-risk-director"] == "direct" and len(links) > 1 and tech["coverage"]["write_rules"] == 1
    with q.env.store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    moved = impact(q, q.admin, cid, "technical").json()
    assert moved["knowledge_moved"] is True and moved["technical"]["report_hash"] == tech["technical"]["report_hash"]     # still the report of the knowledge it was confirmed against


def test_condition_and_exception_changes_find_their_rules_files_and_recommendations():
    cond = op("add_or_change_condition", "rule-standard-manager-review", {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000},
              {"any": [{"field": "request.net_amount_jpy", "op": "gte", "value": 1000000}, {"field": "supplier.is_new", "op": "eq", "value": "true"}]})
    exc = op("set_exception", "rule-capex-finance-config", [], [{"text": "Emergency CAPEX up to 500,000 JPY does not go to Finance.", "condition": None}])
    for one, rule, expect_to_change in ((cond, "rule-standard-manager-review", {"approval.yaml", "procurement-policy.md"}), (exc, "rule-capex-finance-config", {"approval.yaml", "routing.py"})):
        rep = report([one], list(RULES.values()))
        assert rep["write_scope"] == [rule] and {i["rule_id"]: i for i in rep["affected_rules"]}[rule]["link"] == "direct"
        assert {f["path"] for f in rep["file_inventory"] if f["role"] == "to_change"} == expect_to_change
        assert any(a["kind"] == "update_rule" and a["rule_id"] == rule for a in rep["proposed_actions"]) and any(a["kind"].startswith("recommend_") for a in rep["proposed_actions"])
        assert {s["status"] for s in rep["simulation"]} == {"NOT_SIMULATED"} and "no simulation adapter supports" in rep["simulation"][0]["reason"]
    assert any(a["kind"] == "recommend_code" and "routing.py" in a["path"] for a in report([exc], list(RULES.values()))["proposed_actions"])
