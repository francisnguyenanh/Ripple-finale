"""SYN-02 (+ review findings F-01/F-02): the decided knowledge survives a source refresh and the knowledge loop has a product entry point.

Pure module (`business_rebase`) and real routes: a committed change (CHG-05 TARGET rule), a user edit of the Harbor YAML outside Ripple, `POST /v2/sources/refresh/prepare|commit`,
the worker, then `POST /v2/business/analyse` derives a proposal that carries the target with its implementation status read back from the new sources; acceptance through the
existing KNO-05 route. No model except the CHG-01 STUB understanding and the KNO-02 STUB extraction (entry-point test). Not human acceptance.
"""
from __future__ import annotations

import base64
import copy
from uuid import uuid4

from core.change_intelligence.v2 import business_rebase as br
from infra.adapters.v2_business import business_view
from tests.final_loop.test_chg04_review import review
from tests.final_loop.test_chg05_commit import TARGET, approved, commit
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno02_extraction import SIX_KINDS, run_extraction
from tests.final_loop.test_kno05_accept import accept
from tests.final_loop.test_qry02_retrieval import KNOWLEDGE
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import PREFIX, drain, harbor_files

RULES = {r["rule_id"]: r for r in KNOWLEDGE["observed_rules"]}
YAML_PATH = f"{PREFIX}/config/approval.yaml"


def target_of(rule, **over):
    t = copy.deepcopy(rule)
    t.update(rule_id=rule["rule_id"] + "-target", authority="TARGET", status="CONFIRMED", implementation_status="pending", provenance=[{"kind": "human_decision", "decision_id": "d-1"}])
    t.update(over)
    return t


# ---- pure ------------------------------------------------------------------------------------------------------------------------------------------------

def test_implementation_status_is_verified_partial_divergent_unverifiable_or_kept():
    obs = RULES["rule-standard-manager-review"]
    same = target_of(obs)
    assert br.implementation_status_for(same, obs)["status"] == "verified"
    higher = target_of(obs, condition=dict(obs["condition"], value=1500000))
    assert br.implementation_status_for(higher, obs)["status"] == "divergent"
    other_actor = target_of(obs, actor_ref="role-finance")
    v = br.implementation_status_for(other_actor, obs)
    assert v["status"] == "partial" and v["differs"] == ["actor"]
    assert br.implementation_status_for(higher, None)["status"] == "unverifiable"
    kept = br.implementation_status_for(higher, obs, previous_observed=obs)                                      # the observed rule did not change: nothing new was read
    assert kept["status"] == "pending" and "did not change" in kept["basis"]
    assert br.implementation_status_for(higher, dict(obs, condition=dict(obs["condition"], value=1500000)), previous_observed=obs)["status"] == "verified"


def test_reobserve_updates_only_the_single_numeric_leaf_of_a_readable_scalar_binding_and_reports_the_rest():
    def reader(rule, binding):
        ref = binding["evidence_ref"]
        if ref.endswith("standard_manager_threshold_jpy"):
            return 1500000
        if ref.endswith("capex_finance_threshold_jpy"):
            return 2000000                                                                                       # unchanged
        return None                                                                                              # documents and code are not read back
    rules = [RULES["rule-standard-manager-review"], RULES["rule-capex-finance-config"], RULES["rule-high-risk-director"], target_of(RULES["rule-standard-manager-review"])]
    out, report = br.reobserve_rules(rules, reader, new_revision_id="rev-2")
    by = {r["rule_id"]: r for r in out}
    assert by["rule-standard-manager-review"]["condition"]["value"] == 1500000 and by["rule-standard-manager-review"]["source_bindings"][0]["revision_id"] == "rev-2"
    assert br._numeric_leaves(by["rule-capex-finance-config"]["condition"])[0]["value"] == 2000000 and by["rule-high-risk-director"] == RULES["rule-high-risk-director"]
    assert by[TARGET]["condition"]["value"] == 1000000 and by[TARGET]["implementation_status"] == "pending"            # targets pass through untouched here
    rep = {e["rule_id"]: e for e in report}
    assert rep["rule-standard-manager-review"]["status"] == "reobserved" and rep["rule-standard-manager-review"]["before"] == 1000000 and rep["rule-standard-manager-review"]["after"] == 1500000
    assert rep["rule-capex-finance-config"]["status"] == "unchanged" and rep["rule-high-risk-director"]["status"] == "not_reread"
    conflicting, rep2 = br.reobserve_rules([RULES["rule-standard-manager-review"]], lambda r, b: 1500000 if "yaml" in b["evidence_ref"] else 999, new_revision_id="rev-2")
    assert conflicting[0]["condition"]["value"] == 1000000 and rep2[0]["status"] == "conflicting_readings"             # two readings disagree: nothing is guessed


def test_rebase_keeps_targets_replaces_observed_by_id_and_lists_missing_and_added():
    accepted = list(RULES.values()) + [target_of(RULES["rule-standard-manager-review"], condition=dict(RULES["rule-standard-manager-review"]["condition"], value=1500000))]
    new_observed = [dict(RULES["rule-standard-manager-review"], condition=dict(RULES["rule-standard-manager-review"]["condition"], value=1500000)),
                    dict(RULES["rule-capex-finance-config"], rule_id="rule-capex-finance-config"), dict(copy.deepcopy(RULES["rule-high-risk-director"]), rule_id="rule-brand-new")]
    out = br.rebase(accepted, new_observed, previous_observed=list(RULES.values()))
    by = {r["rule_id"]: r for r in out["rules"]}
    assert by[TARGET]["implementation_status"] == "verified" and by[TARGET]["condition"]["value"] == 1500000 and out["carried_targets"] == [TARGET]
    assert by["rule-standard-manager-review"]["condition"]["value"] == 1500000 and "rule-brand-new" in by
    assert set(out["missing_observed"]) == {"rule-capex-finance-policy", "rule-high-risk-director", "rule-emergency-routing"} and all(r in by for r in out["missing_observed"])
    assert out["statuses"][0]["target_rule_id"] == TARGET and out["statuses"][0]["status"] == "verified"


# ---- real refresh, worker, analyse, accept -----------------------------------------------------------------------------------------------------------------

def refresh_yaml(q, value, extra=b""):
    """The user edited approval.yaml outside Ripple and refreshes the whole source set (a refresh with a missing file would be a deletion)."""
    files = []
    for path, content in harbor_files():
        if path == YAML_PATH:
            content = content.replace(b"standard_manager_threshold_jpy: 1000000", f"standard_manager_threshold_jpy: {value}".encode()) + extra
        files.append({"path": path, "content_b64": base64.b64encode(content).decode()})
    prep = q.admin.post("/v2/sources/refresh/prepare", json={"workspace_id": q.env.ws, "idempotency_key": uuid4().hex, "files": files})
    assert prep.status_code == 200, prep.text
    p = prep.json()
    r = q.admin.post("/v2/sources/refresh/commit", json={"workspace_id": q.env.ws, "refresh_id": p["refresh_id"], "expected_version": p["base"]["version"],
                                                        "expected_refresh_digest": p["refresh_digest"], "acknowledged_deletions": []})
    assert r.status_code == 200, r.text
    drain(q.env)
    return r.json()


def analyse(q):
    r = q.admin.post("/v2/business/analyse", json={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def committed_change(q):
    cid, approver, binding = approved(q)
    assert commit(q, q.admin, cid, binding).status_code == 200
    drain(q.env)
    return cid


def test_a_correct_edit_is_read_back_the_target_survives_the_refresh_as_verified_and_the_person_accepts(qa_env):
    q = qa_env
    committed_change(q)
    before = business_view(q.env.store, q.env.tenant, q.env.ws)
    assert before["business_head"]["business_revision"] == 2
    refresh_yaml(q, 1500000)
    assert business_view(q.env.store, q.env.tenant, q.env.ws)["observed_stale"] is True
    assert q.req.post("/v2/business/changes", json={"workspace_id": q.env.ws, "request": "Raise it to 1,600,000 JPY."}).status_code in (200, 409)      # the loop is not locked (a stale pin is refused later, honestly)
    out = analyse(q)
    assert out["derived"] and out["derived"].startswith("rebase-")
    view = business_view(q.env.store, q.env.tenant, q.env.ws)
    pending = view["pending"]
    cov = pending["snapshot"]["coverage"]
    assert cov["origin"] == "rebase" and cov["observed_from"] == "structured_read_back" and TARGET in cov["carried_targets"]
    rules = {r["rule_id"]: r for r in pending["rules"]}
    assert rules["rule-standard-manager-review"]["condition"]["value"] == 1500000 and rules[TARGET]["implementation_status"] == "verified" and rules[TARGET]["condition"]["value"] == 1500000
    statuses = {s["target_rule_id"]: s for s in cov["statuses"]}
    assert statuses[TARGET]["status"] == "verified"
    assert statuses["rule-capex-finance-config-target"]["status"] == "pending" and "did not change" in statuses["rule-capex-finance-config-target"]["basis"]   # the answered-gap target: nothing new read
    assert pending["snapshot"]["source_revision_id"] == q.env.store.head(q.env.tenant, q.env.ws).source_revision_id
    again = analyse(q)
    assert again["snapshot_id"] == out["snapshot_id"] and again["replayed"] is True                                   # idempotent
    r = accept(q.admin, q.env, acknowledged_open_gaps=True)
    assert r.status_code == 200, r.text
    after = business_view(q.env.store, q.env.tenant, q.env.ws)
    assert after["business_head"]["business_revision"] == 3 and after["observed_stale"] is False
    accepted = {r["rule_id"]: r for r in after["accepted"]["rules"]}
    assert accepted[TARGET]["implementation_status"] == "verified" and accepted["rule-standard-manager-review"]["condition"]["value"] == 1500000
    assert any(g["status"] == "RESOLVED" for g in after["accepted"]["gaps"])                                          # the answered gap is carried, not reopened
    pack = q.admin.post("/v2/business/context", json={"workspace_id": q.env.ws, "query": "standard manager approval threshold", "view": "observed", "semantic": False}).json()
    assert any("1500000" in i["text"] for i in pack["items"]) and pack["pin"]["business_revision"] == 3


def test_a_divergent_edit_never_overwrites_the_target_and_an_unrelated_refresh_keeps_the_status_pending(qa_env):
    q = qa_env
    committed_change(q)
    refresh_yaml(q, 1700000)
    analyse(q)
    pending = business_view(q.env.store, q.env.tenant, q.env.ws)["pending"]
    rules = {r["rule_id"]: r for r in pending["rules"]}
    assert rules["rule-standard-manager-review"]["condition"]["value"] == 1700000 and rules[TARGET]["condition"]["value"] == 1500000
    status_of = lambda snap: {s["target_rule_id"]: s for s in snap["snapshot"]["coverage"]["statuses"]}[TARGET]   # noqa: E731
    assert rules[TARGET]["implementation_status"] == "divergent" and status_of(pending)["basis"].startswith("the sources state a different condition")
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    after = {r["rule_id"]: r for r in business_view(q.env.store, q.env.tenant, q.env.ws)["accepted"]["rules"]}
    assert after[TARGET]["condition"]["value"] == 1500000 and after[TARGET]["implementation_status"] == "divergent"
    refresh_yaml(q, 1700000, extra=b"\n# reviewed outside Ripple\n")                                                 # the file changed, the value did not
    analyse(q)
    pending = business_view(q.env.store, q.env.tenant, q.env.ws)["pending"]
    assert {r["rule_id"]: r for r in pending["rules"]}[TARGET]["implementation_status"] == "divergent"                 # previous status kept: nothing new was read
    assert status_of(pending)["basis"].startswith("the observed rule did not change")


def test_a_fresh_workspace_gets_its_first_snapshot_from_the_extraction_candidates_and_nothing_without_them(env, monkeypatch):
    admin = env.admin_client()
    r = admin.post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 409 and r.json()["code"] == "no_snapshot"                                                 # no sources, no extraction: no business knowledge
    run_extraction(env, monkeypatch, SIX_KINDS)                                                                       # imports the Harbor files and runs the stub extraction
    r = admin.post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    out = r.json()
    view = business_view(env.store, env.tenant, env.ws)
    pending = view["pending"]
    assert pending["snapshot"]["coverage"]["origin"] == "backfill" and pending["snapshot"]["coverage"]["observed_from"] == "extraction_candidates"
    assert len(pending["rules"]) >= 5 and all(r["authority"] == "OBSERVED" for r in pending["rules"]) and out["gaps"] >= 1
    gaps = admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()
    assert gaps["gaps"] and gaps["snapshot_id"] == out["snapshot_id"]
    again = admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).json()
    assert again["snapshot_id"] == out["snapshot_id"] and again["replayed"] is True


# ---- revalidation of dependencies (review F-02 addendum) ----------------------------------------------------------------------------------------------------

def test_confirmed_relations_and_answered_gaps_whose_dependencies_moved_are_reopened_and_a_lost_mapping_asks_a_question():
    rels = [{"edge_id": "edge-c1", "from": "rule-standard-manager-review", "to": "concept-purchase-approval", "type": "defines", "status": "CONFIRMED", "provenance_refs": ["decision-1"]},
            {"edge_id": "edge-c2", "from": "rule-capex-finance-config", "to": "role-finance", "type": "approves", "status": "CONFIRMED", "provenance_refs": ["decision-1"]},
            {"edge_id": "edge-p1", "from": "rule-standard-manager-review", "to": "role-manager", "type": "approves", "status": "PROPOSED", "provenance_refs": []}]
    gaps = [{"gap_id": "gap-aaaaaaaaaaaaaaaa", "kind": "missing_actor", "status": "RESOLVED", "affected_rule_ids": ["rule-standard-manager-review"], "question": "Who?",
             "evidence_refs": ["approval.yaml#routing_rules.standard_manager_threshold_jpy", "decision-1"], "requires_permission": "knowledge.confirm", "priority": "normal"},
            {"gap_id": "gap-bbbbbbbbbbbbbbbb", "kind": "missing_actor", "status": "RESOLVED", "affected_rule_ids": ["rule-capex-finance-config"], "question": "Who?",
             "evidence_refs": ["decision-2"], "requires_permission": "knowledge.confirm", "priority": "normal"}]
    target = target_of(RULES["rule-high-risk-director"])
    out = br.revalidate(rels, gaps, changed_observed={"rule-standard-manager-review"}, missing_observed={"rule-high-risk-director"},
                        changed_refs={"approval.yaml#routing_rules.standard_manager_threshold_jpy"}, lost_mapping_targets=[target])
    by = {e["edge_id"]: e for e in out["relations"]}
    assert by["edge-c1"]["status"] == "PENDING_HUMAN" and by["edge-c1"]["provenance_refs"] == ["decision-1"] and by["edge-c2"]["status"] == "CONFIRMED" and by["edge-p1"]["status"] == "PROPOSED"
    assert out["relations_recheck"] == [{"edge_id": "edge-c1", "reason": "endpoint_changed", "ends": ["rule-standard-manager-review"]}]
    g = {x["gap_id"]: x for x in out["gaps"]}
    assert g["gap-aaaaaaaaaaaaaaaa"]["status"] == "REOPENED" and g["gap-bbbbbbbbbbbbbbbb"]["status"] == "RESOLVED"          # a decision-only answer is not evidence that moved
    assert out["gaps_reopened"][0]["reason"] == "evidence_changed"
    link = out["uncertain_links"][0]
    assert link["target_rule_id"] == target["rule_id"] and g[link["gap_id"]]["kind"] == "uncertain_link" and g[link["gap_id"]]["status"] == "OPEN"
    again = br.revalidate(out["relations"], out["gaps"], changed_observed=set(), missing_observed=set(), changed_refs=set(), lost_mapping_targets=[target])
    assert again["uncertain_links"] == [] and len(again["gaps"]) == len(out["gaps"])                                      # stable id: never asked twice


def test_reobserve_tells_a_lost_file_apart_from_an_unreadable_value():
    rule = copy.deepcopy(RULES["rule-standard-manager-review"])
    out, report = br.reobserve_rules([rule], lambda r, b: br.MISSING_ARTIFACT, new_revision_id="rev-3")
    assert report[0]["mapping_lost"] is True and report[0]["status"] == "not_reread" and len(report[0]["missing_bindings"]) == 2 and out[0]["condition"] == rule["condition"]
    out, report = br.reobserve_rules([rule], lambda r, b: None, new_revision_id="rev-3")
    assert report[0]["mapping_lost"] is False and report[0]["missing_bindings"] == []


def refresh_without_yaml(q):
    """The user deleted approval.yaml outside Ripple: the refresh must acknowledge the deletion."""
    files = [{"path": path, "content_b64": base64.b64encode(content).decode()} for path, content in harbor_files() if path != YAML_PATH]
    prep = q.admin.post("/v2/sources/refresh/prepare", json={"workspace_id": q.env.ws, "idempotency_key": uuid4().hex, "files": files})
    assert prep.status_code == 200, prep.text
    p = prep.json()
    r = q.admin.post("/v2/sources/refresh/commit", json={"workspace_id": q.env.ws, "refresh_id": p["refresh_id"], "expected_version": p["base"]["version"],
                                                        "expected_refresh_digest": p["refresh_digest"], "acknowledged_deletions": [YAML_PATH]})
    assert r.status_code == 200, r.text
    drain(q.env)


def test_a_deleted_source_file_makes_the_target_unverifiable_asks_where_it_lives_and_never_marks_behaviour_verified(qa_env):
    q = qa_env
    cid = committed_change(q)
    refresh_without_yaml(q)
    analyse(q)
    pending = business_view(q.env.store, q.env.tenant, q.env.ws)["pending"]
    cov = pending["snapshot"]["coverage"]
    rules = {r["rule_id"]: r for r in pending["rules"]}
    assert "rule-standard-manager-review" in cov["mapping_lost"] and rules[TARGET]["implementation_status"] == "unverifiable" and rules[TARGET]["condition"]["value"] == 1500000
    assert any(g["kind"] == "uncertain_link" and g["affected_rule_ids"] == [TARGET] and g["status"] == "OPEN" for g in pending["gaps"])
    assert cov["behavioral_verification"] == "not_tested" and any(l["target_rule_id"] == TARGET for l in cov["uncertain_links"])        # the CAPEX target lost its mapping too
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    rv = review(q, q.admin, cid).json()
    assert rv["completion"]["behavioral_verification"] == "not_tested" and rv["completion"]["knowledge_result"] == "committed"             # source alignment never becomes behaviour
    code_rec = q.admin.get(f"/v2/business/changes/{cid}/code", params={"workspace_id": q.env.ws}).json()
    assert code_rec["stale"] is True                                                                                                 # SYN-01 recommendation says the sources moved
