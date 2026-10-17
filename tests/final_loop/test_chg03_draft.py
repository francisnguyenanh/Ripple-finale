"""CHG-03: the structured draft of a confirmed business change (pure module + real routes, worker, job/budget contract and store).

The draft structure is the server's (rule before/after, relation updates, file actions, outcomes, carried coverage). The model only writes guidance; here the model is a STUB whose
answers are synthetic fixtures written by the test author: they prove the pipeline and the server's guards (refs, plain text, downgrade-only holds, hashes, permissions, failure
handling), never that a real model gives useful guidance. Live evaluation stays open. The test workspace is the Harbor import: the fixture knowledge binds `approval.yaml`
(present as harbor_demo/config/approval.yaml, re-bound by path) and `procurement-policy.md`/`routing.py`/`manager-guide.md` (absent from that import: UNRESOLVED, never "no impact").
"""
from __future__ import annotations

import copy
import json
import time

import psycopg2
import pytest

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2 import business_impact as bi
from infra.adapters.v2_identity import PostgresIdentityStore
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj
from tests.final_loop.test_chg02_impact import PIN, RULES, confirmed, impact, make, op
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import drain, job_body

LIMITS = json.loads(open("config/change_draft_guidance.json", encoding="utf-8").read())["limits"]
YAML_LOC = "routing_rules.standard_manager_threshold_jpy"
VALUE = op("set_value", "rule-standard-manager-review", {"value": 1000000, "unit": "JPY"}, {"value": 1500000, "unit": "JPY"})
ACTOR = op("set_actor", "rule-high-risk-director", "role-director", "role-finance-controller", 2)
COND = op("add_or_change_condition", "rule-standard-manager-review", {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"},
          {"any": [{"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"}, {"field": "supplier.is_new", "op": "eq", "value": "true"}]}, 3)
EXC = op("set_exception", "rule-capex-finance-config", [], [{"text": "Emergency CAPEX up to 500,000 JPY does not go to Finance.", "condition": None}], 4)
SCOPE = op("set_scope", "rule-high-risk-director", {}, {"process": ["STANDARD"]}, 5)
ARTS = {"a-yaml": "harbor_demo/config/approval.yaml", "a-md": "harbor_demo/docs/procurement-policy.md", "a-py": "harbor_demo/app/routing.py", "a-xlsx": "harbor_demo/data/matrix.xlsx"}
OBS = [{"artifact_id": "a-yaml", "kind": "yaml_value", "locator": {"type": "key_path", "key_path": YAML_LOC}, "raw_value": 1000000},
       {"artifact_id": "a-yaml", "kind": "yaml_value", "locator": {"type": "key_path", "key_path": "routing_rules.capex_finance_threshold_jpy"}, "raw_value": 2000000},
       {"artifact_id": "a-md", "kind": "text_chunk", "locator": {"type": "line_span", "line_start": 1, "line_end": 30},
        "raw_value": "# Policy\n### 2.1 Standard purchases\nPurchases of 1,000,000 JPY or more need manager approval.\n### 2.3 High-risk suppliers\nDirector approves."},
       {"artifact_id": "a-py", "kind": "code_block", "locator": {"type": "code_span", "qualname": "capex_finance_threshold_jpy", "line_start": 3, "line_end": 3}, "raw_value": "capex_finance_threshold_jpy = 2000000"},
       {"artifact_id": "a-xlsx", "kind": "cell", "locator": {"type": "cell", "sheet": "RoutingRules", "cell": "B3"}, "raw_value": 1000000}]


def intent_of(*ops, request="Change it."):
    return {"intent_id": "chg-test", "request": request, "operations": [dict(o) for o in ops], "write_scope": sorted({o["rule_id"] for o in ops}), "read_scope": []}


def draft(*ops, rules=None, artifacts=ARTS, observations=OBS, known=None, relations=None, **kw):
    rules = {r["rule_id"]: r for r in (rules if rules is not None else RULES.values())}
    rel = bg.derive_relations(rules.values()) if relations is None else relations
    it = intent_of(*ops)
    report = bi.build_report(it["operations"], rules, rel, pin=PIN, known_artifacts=set(artifacts) if known is None else known)
    return bd.build_draft(it, report, rules, rel, pin=PIN, artifacts=artifacts, observations=observations, **kw)


def by_ref(d):
    return {i["ref"]: i for i in d["file_actions"]}


# ---- rule before/after ---------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("one, field", [(VALUE, "condition"), (ACTOR, "actor_ref"), (COND, "condition"), (EXC, "exceptions"), (SCOPE, "scope")])
def test_each_operation_kind_changes_only_its_field_and_the_after_state_passes_the_contract(one, field):
    d = draft(one)
    rc = d["rule_changes"][0]
    assert rc["valid"] is True and rc["error"] is None and [x["field"] for x in rc["diff"]] == [field] and all(rc["invariants"].values())
    for k in ("rule_id", "provenance", "source_bindings", "status", "authority", "title", "outcome", "concept_ids"):
        assert rc["before"][k] == rc["after"][k]
    oc = d["operations"][0]
    assert oc["rule_change"] == "DRAFTED" and oc["op_id"] == one["op_id"] and rc["before_text"] != rc["after_text"]
    if one is VALUE:
        assert rc["after"]["condition"]["value"] == 1500000 and rc["after"]["condition"]["unit"] == "JPY"
    if one is EXC:
        assert rc["after"]["exceptions"][0]["exception_id"].startswith("exc-") and rc["after"]["exceptions"][0]["text"].startswith("Emergency CAPEX")


def test_removing_or_dropping_exceptions_is_flagged_and_other_operations_keep_them():
    with_exc = copy.deepcopy(RULES["rule-standard-manager-review"])
    with_exc["exceptions"] = [{"exception_id": "exc-1", "text": "Pre-approved frame contracts.", "condition": None}, {"exception_id": "exc-2", "text": "Internal transfers.", "condition": None}]
    rules = [with_exc] + [r for k, r in RULES.items() if k != "rule-standard-manager-review"]
    removed = draft(op("set_exception", "rule-standard-manager-review", with_exc["exceptions"], [], 1), rules=rules)
    assert any(f["flag"] == "removes_all_exceptions" and f["severity"] == "HIGH" for f in removed["operations"][0]["flags"])
    fewer = draft(op("set_exception", "rule-standard-manager-review", with_exc["exceptions"], [{"text": "Internal transfers.", "condition": None}], 1), rules=rules)
    assert any(f["flag"] == "drops_an_exception" for f in fewer["operations"][0]["flags"])
    assert fewer["rule_changes"][0]["after"]["exceptions"][0]["exception_id"] == "exc-2"                                            # a kept exception keeps its id
    value_only = draft(VALUE, rules=rules)
    assert value_only["rule_changes"][0]["after"]["exceptions"] == with_exc["exceptions"] and value_only["rule_changes"][0]["invariants"]["exceptions_kept"] is True


def test_an_after_state_the_contract_refuses_is_reported_as_invalid_never_dropped():
    bad = draft(op("set_actor", "rule-high-risk-director", "role-director", "role-Finance Controller", 1))               # not a role id: the contract validator refuses it
    rc, oc = bad["rule_changes"][0], bad["operations"][0]
    assert rc["valid"] is False and rc["error"].startswith("invalid_field") and oc["rule_change"] == "INVALID_AFTER_STATE" and oc["error"] == rc["error"]
    assert bad["status"] == "INVALID" and bad["coverage"]["complete"] is False and len(bad["operations"]) == 1
    with pytest.raises(ValueError, match="unknown_rule"):
        draft(op("set_value", "rule-nope", {"value": 1, "unit": "JPY"}, {"value": 2, "unit": "JPY"}))


def test_two_operations_on_one_rule_apply_in_order_and_each_keeps_its_outcome():
    d = draft(VALUE, op("set_actor", "rule-standard-manager-review", "role-manager", "role-finance", 2))
    rc = d["rule_changes"][0]
    assert rc["op_ids"] == ["op-1", "op-2"] and rc["after"]["condition"]["value"] == 1500000 and rc["after"]["actor_ref"] == "role-finance"
    assert [o["op_id"] for o in d["operations"]] == ["op-1", "op-2"] and all(o["rule_change"] == "DRAFTED" for o in d["operations"])


# ---- file actions ---------------------------------------------------------------------------------------------------------------------------------------

def test_file_actions_yaml_scalar_edit_no_op_differing_value_document_code_and_missing_file():
    d = draft(VALUE)
    acts = {(i["path"], i["op_ids"][0] if i["op_ids"] else None): i for i in d["file_actions"]}
    yaml_item = acts[("approval.yaml", "op-1")]
    assert yaml_item["action"] == "AI_EDIT_SUPPORTED" and yaml_item["proposal_edit"] == {"key_path": YAML_LOC, "expected_current": 1000000, "proposed": 1500000, "unit": "JPY"}
    assert yaml_item["rebound_by_path"] is True and yaml_item["resolved_artifact_id"] == "a-yaml" and yaml_item["artifact_id"] == "cfg-approval"
    doc = acts[("procurement-policy.md", "op-1")]
    assert doc["action"] == "MANUAL" and doc["reason"] == "wording_update" and doc["resolution"] == "heading" and doc["instruction"].startswith("Update the wording in procurement-policy.md at 2.1")
    already = draft(VALUE, observations=[dict(OBS[0], raw_value=1500000)] + OBS[1:])
    assert by_ref(already)["F001"]["action"] == "NO_OP" and by_ref(already)["F001"]["proposal_edit"] is None and "No edit needed" in by_ref(already)["F001"]["instruction"]
    other = draft(VALUE, observations=[dict(OBS[0], raw_value=1200000)] + OBS[1:])
    assert by_ref(other)["F001"]["action"] == "MANUAL" and by_ref(other)["F001"]["reason"] == "stored_value_differs_from_confirmed_before_value"
    code = draft(op("set_value", "rule-capex-finance-config", {"value": 2000000, "unit": "JPY"}, {"value": 3000000, "unit": "JPY"}))
    py = next(i for i in code["file_actions"] if i["path"] == "routing.py")
    assert py["action"] == "MANUAL" and py["reason"] == "code_recommendation_only" and py["code_recommendation"] is True and py["proposal_edit"] is None and "Recommendation only" in py["instruction"]
    assert next(i for i in code["file_actions"] if i["path"] == "approval.yaml" and i["op_ids"]) ["action"] == "AI_EDIT_SUPPORTED"
    gone = draft(VALUE, artifacts={"a-md": ARTS["a-md"]})
    missing = by_ref(gone)["F001"]
    assert missing["action"] == "UNRESOLVED" and missing["reason"] == "missing_file" and "has no such file" in missing["instruction"] and gone["operations"][0]["file_coverage"] == "partial"


def test_structured_non_value_edits_related_rule_checks_unknown_locations_and_unsupported_formats_get_manual_instructions():
    d = draft(EXC)
    yaml_item = next(i for i in d["file_actions"] if i["path"] == "approval.yaml" and i["op_ids"])
    assert yaml_item["action"] == "MANUAL" and yaml_item["reason"] == "structured_edit_not_auto_derived" and "set the exceptions of" in yaml_item["instruction"]
    check = [i for i in d["file_actions"] if i["role"] == "check"]
    assert check and all(i["action"] in ("MANUAL", "UNRESOLVED") and not i["op_ids"] for i in check)
    lost = draft(VALUE, observations=[OBS[0]])                                                            # the document exists but the section is not found
    doc = next(i for i in lost["file_actions"] if i["path"] == "procurement-policy.md" and i["op_ids"])
    assert doc["action"] == "MANUAL" and doc["reason"] == "location_not_found" and "recorded location was not found" in doc["instruction"]
    sheet = make("rule-sheet", "concept-x", value=1000000)
    sheet["source_bindings"] = [{"artifact_id": "a-xlsx", "revision_id": "rev-1", "evidence_ref": "matrix.xlsx#RoutingRules!B3"}]
    x = draft(op("set_value", "rule-sheet", {"value": 1000000, "unit": "JPY"}, {"value": 1500000, "unit": "JPY"}), rules=list(RULES.values()) + [sheet])
    cell = by_ref(x)["F001"]
    assert cell["action"] == "MANUAL" and cell["reason"] == "unsupported_format_manual" and cell["resolution"] == "label" and "not edited automatically" in cell["instruction"]
    assert cell["instruction"].count("matrix.xlsx at RoutingRules!B3") == 1


def test_ambiguous_path_rebinding_is_refused_and_an_exact_id_match_is_not_rebound():
    two = dict(ARTS, **{"b-yaml": "other/config/approval.yaml"})
    d = draft(VALUE, artifacts=two)
    assert by_ref(d)["F001"]["action"] == "UNRESOLVED" and by_ref(d)["F001"]["reason"] == "missing_file"                          # two candidates: nobody guesses
    exact = draft(VALUE, artifacts={"cfg-approval": "approval.yaml"}, observations=[dict(OBS[0], artifact_id="cfg-approval")])
    assert by_ref(exact)["F001"]["action"] == "AI_EDIT_SUPPORTED" and by_ref(exact)["F001"]["rebound_by_path"] is False


# ---- outcomes, coverage, carried items -------------------------------------------------------------------------------------------------------------------

def test_every_operation_has_an_outcome_and_unknown_is_never_no_impact():
    bare = make("rule-bare", "concept-c1", bindings=False)
    d = draft(VALUE, op("set_value", "rule-bare", {"value": 7, "unit": "JPY"}, {"value": 9, "unit": "JPY"}, 2), rules=list(RULES.values()) + [bare])
    assert [o["op_id"] for o in d["operations"]] == ["op-1", "op-2"]
    second = d["operations"][1]
    assert second["file_coverage"] == "unknown" and second["files"] == {a: 0 for a in bd.ACTIONS} and second["rule_change"] == "DRAFTED"
    u = next(x for x in d["unresolved"] if x["kind"] == "no_source_mapping")
    assert u["rule_id"] == "rule-bare" and u["action"] == "UNRESOLVED" and "Record the location" in u["instruction"]
    assert d["coverage"]["complete"] is False and d["status"] == "PARTIAL" and "not “no impact”" in d["statement"]
    assert all(o["simulation"]["status"] == "NOT_SIMULATED" for o in d["operations"]) and d["measured"] == []


def test_counterevidence_excluded_and_proposed_dependencies_are_carried_as_labels_and_never_change_the_write_scope_or_after_state():
    a, b = make("rule-a", "concept-c1", actor="role-a", value=5), make("rule-b", "concept-c1", actor="role-a", value=9)
    rules = [a, b]
    rel = bg.derive_relations({r["rule_id"]: r for r in rules}.values())
    deps = [{"dep_id": "dep-1", "from_rule_id": "rule-a", "to_rule_id": "rule-b", "relation": "depends_on", "rationale": "x", "evidence_refs": ["rule-b"], "status": "PROPOSED", "provenance": "ai_inference", "changes_write_scope": False},
            {"dep_id": "dep-2", "from_rule_id": "rule-a", "to_rule_id": "rule-b", "relation": "implements", "status": "CONFIRMED"}]
    d = draft(op("set_value", "rule-a", {"value": 5, "unit": "JPY"}, {"value": 6, "unit": "JPY"}), rules=rules, relations=rel, proposed_dependencies=deps, dependency_state={"status": "completed", "complete": True})
    assert d["write_scope"] == ["rule-a"] and [c["rule_id"] for c in d["counterevidence"]] == ["rule-b"] and "not changed by this draft" in d["counterevidence"][0]["label"]
    assert [x["dep_id"] for x in d["proposed_dependencies"]] == ["dep-1"] and d["proposed_dependencies"][0]["label"] == bd.PROPOSED_LABEL        # only PROPOSED leads are carried, labelled
    assert [c["rule_id"] for c in d["rule_changes"]] == ["rule-a"] and d["rule_changes"][0]["after"]["source_bindings"] == a["source_bindings"]
    assert any(r["type"] == "conflicts_with" and r["action"] == "keep_as_counterevidence" for r in d["relation_updates"])
    twin = make("rule-twin", "concept-other", value=1000000)
    e = draft(VALUE, rules=list(RULES.values()) + [twin])
    assert any("same number" in t["text"] for t in e["excluded"]) and "rule-twin" not in {c["rule_id"] for c in e["rule_changes"]}


def test_actor_change_moves_the_approving_relation_and_other_relations_are_rechecked():
    d = draft(ACTOR)
    moves = [r for r in d["relation_updates"] if r["action"] == "update_endpoint"]
    assert moves and all(r["status"] == "PROPOSED" and "role-finance-controller" in (r["after"]["from"], r["after"]["to"]) for r in moves)
    assert all(r["action"] in ("update_endpoint", "recheck", "keep_as_counterevidence") for r in d["relation_updates"])


def test_semantic_diff_hash_depends_on_the_operations_only_and_the_draft_hash_on_everything():
    one, again = draft(VALUE), draft(VALUE)
    other = draft(op("set_value", "rule-standard-manager-review", {"value": 1000000, "unit": "JPY"}, {"value": 1700000, "unit": "JPY"}))
    assert one["semantic_diff_hash"] == again["semantic_diff_hash"] != other["semantic_diff_hash"] and one["draft_hash"] == again["draft_hash"]
    moved = draft(VALUE, observations=[dict(OBS[0], raw_value=1500000)] + OBS[1:])
    assert moved["semantic_diff_hash"] == one["semantic_diff_hash"] and moved["draft_hash"] != one["draft_hash"]                         # a file state is not a semantic change


# ---- model guidance: text only, downgrade only ----------------------------------------------------------------------------------------------------------

def gobj(items=(), summary="Raise the threshold; update the policy wording by hand.", questions=()):
    return {"summary": summary, "items": list(items), "questions_for_human": list(questions)}


def gitem(ref="F001", instruction="Change the value in the approval settings and re-run the routing tests.", hold=False, rationale="plain"):
    return {"ref": ref, "instruction": instruction, "hold": hold, "rationale": rationale}


@pytest.mark.parametrize("obj, ok", [(gobj(), True), ({"summary": "s", "items": []}, False), ({**gobj(), "x": 1}, False), (gobj(items=["F001"]), False),
                                     (gobj(items=[{"ref": "F001", "instruction": "i", "hold": "yes", "rationale": ""}]), False), (gobj(items=[gitem()] * (LIMITS["max_items"] + 1)), False),
                                     (gobj(questions=["q"] * (LIMITS["max_questions"] + 1)), False), (gobj(questions=[1]), False)])
def test_the_guidance_object_must_have_the_exact_shape_and_size(obj, ok):
    if ok:
        assert bd.validate_guidance_object(obj, LIMITS) == obj
    else:
        with pytest.raises(ValueError):
            bd.validate_guidance_object(obj, LIMITS)


def test_guidance_cannot_add_drop_upgrade_or_resolve_items_and_a_hold_only_downgrades_an_edit():
    d = draft(VALUE)
    refs = {i["ref"]: i for i in d["file_actions"]}
    manual_ref = next(r for r, i in refs.items() if i["action"] == "MANUAL")
    g = bd.resolve_guidance(gobj(items=[gitem("F001", hold=True, rationale="The config also feeds the emergency path."), gitem(manual_ref, instruction="Rewrite section 2.1.", hold=True),
                                        gitem("F999"), gitem("F001", instruction="dup"), gitem(manual_ref, instruction="See [here](http://x)")], questions=["Is 1,500,000 inclusive?"]), d, LIMITS)
    assert [r["reason"] for r in g["rejected"]] == ["ref_not_in_draft", "duplicate", "duplicate"] and len(g["accepted"]) == 2 and g["questions"] == ["Is 1,500,000 inclusive?"]
    m = bd.merge_guidance(d, g, state={"status": "completed"})
    held = by_ref(m)["F001"]
    assert held["action"] == "MANUAL" and held["reason"] == "model_raised_a_concern" and held["server_action"] == "AI_EDIT_SUPPORTED" and held["held_edit"]["proposed"] == 1500000 and held["proposal_edit"] is None
    assert by_ref(m)[manual_ref]["action"] == "MANUAL" and by_ref(m)[manual_ref]["model_instruction"] == "Rewrite section 2.1."                  # a hold on a manual item changes nothing
    assert len(m["file_actions"]) == len(d["file_actions"]) and m["unresolved"] == d["unresolved"]                                               # nothing added, nothing dropped
    assert set(m["guidance"]["not_addressed"]) == (set(refs) | {u["ref"] for u in d["unresolved"]}) - {"F001", manual_ref} and m["status"] == "PARTIAL" and m["coverage"]["complete"] is False
    bad = bd.resolve_guidance(gobj(items=[gitem("F001", instruction="<b>bold</b>")], summary="`code`"), d, LIMITS)
    assert bad["accepted"] == [] and bad["rejected"][0]["reason"] == "text_not_plain" and bad["summary"] == ""
    assert m["semantic_diff_hash"] == d["semantic_diff_hash"] and m["draft_hash"] != d["draft_hash"]


def test_rendering_hides_paths_locators_and_instructions_without_evidence_rights_and_withholds_guidance_written_with_them():
    d = draft(VALUE)
    m = bd.merge_guidance(d, bd.resolve_guidance(gobj(items=[gitem("F001", instruction="Edit approval.yaml routing_rules")]), d, LIMITS), state={"status": "completed"})
    plain = bd.render(m, mode="business", can_read_evidence=False, guidance_written_with_evidence=True)
    blob = json.dumps(plain)
    assert plain["files"] == [] and plain["files_withheld"] == len(m["file_actions"]) and "rule-" not in blob and ".yaml" not in blob and ".md" not in blob and "routing_rules" not in blob
    assert plain["guidance"]["summary"] is None and plain["guidance"]["summary_withheld"] is True and "technical" not in plain
    assert plain["rule_changes"][0]["before"] != plain["rule_changes"][0]["after"] and plain["operations"][0]["files"]["AI_EDIT_SUPPORTED"] == 1
    full = bd.render(m, mode="technical", can_read_evidence=True, guidance_written_with_evidence=True)
    assert full["files"][0]["path"] == "approval.yaml" and full["files"][0]["model_instruction"] == "Edit approval.yaml routing_rules" and full["technical"]["semantic_diff_hash"] == m["semantic_diff_hash"]
    assert full["technical"]["file_actions"][0]["proposal_edit"]["key_path"] == YAML_LOC and full["technical"]["rule_changes_full"][0]["after"]["condition"]["value"] == 1500000
    tech_plain = bd.render(m, mode="technical", can_read_evidence=False, guidance_written_with_evidence=False)
    assert "file_actions" not in tech_plain["technical"] and tech_plain["files"] == [] and tech_plain["guidance"]["summary"]


# ---- real routes, worker, job/budget contract and store ---------------------------------------------------------------------------------------------------

def get_draft(q, client, cid, mode="business"):
    return client.get(f"/v2/business/changes/{cid}/draft", params={"workspace_id": q.env.ws, "mode": mode})


def ask_guidance(q, client, cid, **extra):
    return client.post(f"/v2/business/changes/{cid}/draft/guidance", json={"workspace_id": q.env.ws, **extra})


def settle_guidance(q, client, cid, mode="business"):
    for _ in range(60):
        drain(q.env)
        got = get_draft(q, client, cid, mode).json()
        if got["guidance"]["status"] in ("completed", "failed", "tampered", "stale"):
            return got
        time.sleep(0.5)
    return got


def good(prompt):
    return gobj(items=[gitem("F001", instruction="Set routing_rules.standard_manager_threshold_jpy to 1500000 and run the routing tests."), gitem("F002", instruction="Rewrite section 2.1 of the policy with the new amount.")],
                questions=["Does the new threshold also apply to framework contracts?"])


def prepared(q, fn=good, client=None):
    cid, _ = confirmed(q, NUM_REQ, [OP_VALUE], client=client)
    model = use(q, fn)
    return cid, model


def test_the_draft_is_built_without_a_model_for_a_confirmed_request_and_follows_permissions(qa_env):
    q = qa_env
    cid, model = prepared(q)
    r = get_draft(q, q.req, cid)
    assert r.status_code == 200 and model.calls == 0
    body = r.json()
    assert body["status"] == "PARTIAL" and body["guidance"]["status"] == "not_requested" and body["coverage"]["complete"] is False and body["knowledge_moved"] is False
    assert body["operations"][0]["rule_change"] == "DRAFTED" and body["operations"][0]["files"]["AI_EDIT_SUPPORTED"] == 1 and body["operations"][0]["files"]["UNRESOLVED"] == 1
    assert body["files"] == [] and body["files_withheld"] >= 2 and "rule-" not in json.dumps(body) and ".yaml" not in json.dumps(body)              # the requester holds no evidence rights
    tech = get_draft(q, q.admin, cid, "technical").json()
    files = {f["path"]: f for f in tech["files"] if f["role"] == "to_change"}                                                                        # approval.yaml is also a `check` item of a related rule
    assert any(f["role"] == "check" and f["path"] == "approval.yaml" and f["reason"] == "related_rule_check" for f in tech["files"])
    assert files["approval.yaml"]["action"] == "AI_EDIT_SUPPORTED" and files["approval.yaml"]["rebound_by_path"] is True and files["approval.yaml"]["proposal_edit"]["expected_current"] == 1000000, files["approval.yaml"]
    assert files["procurement-policy.md"]["action"] == "UNRESOLVED" and files["procurement-policy.md"]["reason"] == "missing_file" and "has no such file" in files["procurement-policy.md"]["instruction"]
    assert tech["technical"]["write_scope"] == ["rule-standard-manager-review"] and tech["technical"]["report_hash"] == impact(q, q.admin, cid, "technical").json()["technical"]["report_hash"]
    assert tech["technical"]["semantic_diff_hash"] == get_draft(q, q.admin, cid, "technical").json()["technical"]["semantic_diff_hash"]
    assert tech["rule_changes"][0]["changes"][0].startswith("when it applies:") and tech["dependency_state"]["status"] == "not_requested"
    from tests.final_loop.test_chg01_change_request import understand
    open_req, _ = understand(q, "Please " + NUM_REQ[0].lower() + NUM_REQ[1:], model_obj(ops=[OP_VALUE]))
    assert get_draft(q, q.req, open_req["change_id"]).status_code == 409 and get_draft(q, q.req, open_req["change_id"]).json()["code"] == "not_confirmed"
    assert get_draft(q, q.req, "chg-nonexistent").status_code == 404 and get_draft(q, q.req, cid, "x").status_code == 400
    anonymous = q.env.admin_client()
    anonymous.cookies.clear()
    assert anonymous.get(f"/v2/business/changes/{cid}/draft", params={"workspace_id": q.env.ws}).status_code in (401, 403)
    assert model.calls == 0


def test_guidance_success_merges_text_downgrade_only_keeps_every_item_and_changes_no_knowledge(qa_env):
    q = qa_env
    cid, model = prepared(q, lambda p: gobj(items=[gitem("F001", hold=True, rationale="The emergency path reads the same key."), gitem("F002", instruction="Rewrite section 2.1."), gitem("F777")],
                                              questions=["Inclusive of 1,500,000?"]))
    before_knowledge = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["accepted"]
    before_counts = counts(q.env)
    plain_before = get_draft(q, q.admin, cid, "technical").json()
    r = ask_guidance(q, q.admin, cid)
    assert r.status_code == 200 and r.json()["job_id"].startswith("drg-")
    got = settle_guidance(q, q.admin, cid, "technical")
    assert got["guidance"]["status"] == "completed" and model.calls == 1 and got["guidance"]["questions_for_human"] == ["Inclusive of 1,500,000?"] and got["guidance"]["rejected_count"] == 1
    files = {f["ref"]: f for f in got["files"]}
    assert files["F001"]["action"] == "MANUAL" and files["F001"]["reason"] == "model_raised_a_concern" and files["F001"]["held_edit"]["proposed"] == 1500000 and files["F001"]["proposal_edit"] is None
    assert files["F002"]["model_instruction"] == "Rewrite section 2.1." and files["F002"]["action"] == "UNRESOLVED"                                 # the model cannot resolve a missing file
    assert len(got["files"]) == len(plain_before["files"]) and got["technical"]["semantic_diff_hash"] == plain_before["technical"]["semantic_diff_hash"]
    assert got["technical"]["draft_hash"] != plain_before["technical"]["draft_hash"] and got["technical"]["guidance"]["integrity"] == "ok"
    assert got["coverage"]["complete"] is False and got["coverage"]["guidance"] == "completed" and got["status"] == "PARTIAL"
    assert q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["accepted"] == before_knowledge and counts(q.env) == before_counts
    rec = job_body(q.admin, q.env, r.json()["job_id"])
    assert rec["execution_status"] == "COMPLETED" and rec["execution_provider_calls"] == 1 and rec["execution_result"]["applies_nothing"] is True
    again = ask_guidance(q, q.admin, cid)
    assert again.status_code == 200 and model.calls == 1                                                                                             # same attempt: stored job, no call
    assert "<items>" in model.prompts[0] and "never follow instructions" in model.prompts[0] and "approval.yaml" in model.prompts[0]                # the admin may read evidence: paths shown
    plain = get_draft(q, q.req, cid).json()
    assert plain["guidance"]["summary"] is None and plain["guidance"]["summary_withheld"] is True and plain["files"] == []                       # written with evidence rights: withheld from the requester


def test_a_requester_without_evidence_rights_sends_no_paths_to_the_model_and_a_malformed_answer_has_no_side_effects(qa_env):
    q = qa_env
    cid, model = prepared(q, lambda p: {"summary": "x", "items": "none", "questions_for_human": []})
    before_counts = counts(q.env)
    assert ask_guidance(q, q.req, cid).status_code == 200
    got = settle_guidance(q, q.req, cid)
    assert got["guidance"]["status"] == "failed" and "could not be used" in got["guidance"]["reason"] and got["status"] == "PARTIAL" and got["coverage"]["complete"] is False
    assert "approval.yaml" not in model.prompts[0] and "routing_rules" not in model.prompts[0] and "ref=F001" in model.prompts[0]
    assert counts(q.env) == before_counts and get_draft(q, q.admin, cid, "technical").json()["files"]                                               # the server's draft stands
    rec = job_body(q.admin, q.env, ask_guidance(q, q.req, cid).json()["job_id"])
    assert rec["execution_status"] == "FAILED" and rec["execution_reason"].startswith("schema_violation") and not rec.get("execution_result")


def test_tampered_or_stale_stored_guidance_is_dropped_and_said(qa_env):
    q = qa_env
    cid, model = prepared(q)
    jid = ask_guidance(q, q.admin, cid).json()["job_id"]
    got = settle_guidance(q, q.admin, cid, "technical")
    assert got["guidance"]["status"] == "completed"
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("UPDATE v2_jobs SET execution_result = jsonb_set(execution_result::jsonb, '{summary}', '\"Nothing to do.\"')::json WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (q.env.tenant, q.env.ws, jid))
        assert cur.rowcount == 1
    bad = get_draft(q, q.admin, cid, "technical").json()
    assert bad["guidance"]["status"] == "tampered" and bad["guidance"]["summary"] is None and all("model_instruction" not in f or f["model_instruction"] is None for f in bad["files"])
    assert bad["technical"]["guidance"]["integrity"] == "hash_mismatch" and bad["coverage"]["complete"] is False and bad["files"][0]["action"] == "AI_EDIT_SUPPORTED"


def test_authority_is_rechecked_when_the_job_runs_and_roles_without_change_prepare_are_refused(qa_env, monkeypatch):
    q = qa_env
    cid, model = prepared(q)
    assert ask_guidance(q, q.req, cid).status_code == 200
    PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, q.env.req)
    got = settle_guidance(q, q.admin, cid)
    assert got["guidance"]["status"] == "failed" and model.calls == 0 and "no longer has permission" in got["guidance"]["reason"]
    assert ask_guidance(q, q.admin, cid, attempt=0).status_code == 400 and ask_guidance(q, q.admin, cid, attempt=True).status_code == 400
    ids = PostgresIdentityStore(q.env.dsn)
    for role, status in (("viewer", 403), ("knowledge_owner", 403)):
        acc = f"{role}-g-{q.env.nonce}"
        ids.create_account(acc, "cv")
        ids.grant_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, acc, (role,))
        from fastapi.testclient import TestClient
        from infra import judge
        c = TestClient(judge.app, base_url="https://judge.test")
        c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        assert c.post("/v2/auth/login", json={"account_id": acc, "credential": "cv"}).status_code == 200
        assert ask_guidance(q, c, cid, attempt=2).status_code == status
        assert get_draft(q, c, cid).status_code == (200 if role == "knowledge_owner" else 404)                                                        # a confirmer may read; a stranger sees nothing
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    r = ask_guidance(q, q.admin, cid, attempt=3)
    assert r.status_code == 409 and "provider_disarmed" in r.text and model.calls == 0
