"""KNO-04: a person answers gaps (queue -> preview -> confirm) through the real routes and store. No provider, no model call."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import business_answers as ba
from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters.v2_business import analyse_workspace, business_view, gap_queue
from infra.adapters.v2_identity import PostgresIdentityStore
from tests.final_loop.test_kno01_business_store import counts, observed, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno03_graph_gaps import seed

QUOTE = "Finance reviews CAPEX from 2,000,000 JPY."


@pytest.fixture
def analysed(env, ws):
    head = seed(env, ws)
    analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    return head


def find(env, kind, rule_id):
    return next(g for g in gap_queue(env.store, env.tenant, env.ws)["gaps"] if g["kind"] == kind and rule_id in g["affected_rule_ids"])


def post(client, env, path, **body):
    return client.post(f"/v2/business/gaps/{path}", json={"workspace_id": env.ws, **body})


def test_queue_preview_confirm_resolves_the_conflict_with_a_target_and_a_recorded_decision(env, analysed):
    admin = env.admin_client()
    gap = find(env, "conflicting_facts", "rule-capex-finance-config")
    answer = {"choose_rule_id": "rule-capex-finance-config"}
    before = counts(env)
    p = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer=answer)
    assert p.status_code == 200, p.text
    pv = p.json()
    assert counts(env) == before                                                     # a preview writes nothing: a comment or preview is never authority
    assert pv["resulting_rule"]["authority"] == "TARGET" and pv["resulting_rule"]["rule_id"] == "rule-capex-finance-config-target"
    assert pv["gap_updates"] == {gap["gap_id"]: "RESOLVED"} and any("source files are not changed" in s for s in pv["summary"])
    c = post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote=QUOTE, preview_hash=pv["preview_hash"])
    assert c.status_code == 200, c.text
    out = c.json()
    view = business_view(env.store, env.tenant, env.ws)
    pending = view["pending"]
    assert pending["snapshot"]["snapshot_id"] == out["snapshot_id"] and view["accepted"] is None             # still a proposal; acceptance is KNO-05
    target = next(r for r in pending["rules"] if r["rule_id"] == "rule-capex-finance-config-target")
    assert target["status"] == "CONFIRMED" and {"kind": "human_decision", "decision_id": out["decision_id"]} in target["provenance"]
    d = pending["decisions"][0]
    assert d["quote"] == QUOTE and d["actor_id"] == env.admin and d["body"]["gap_id"] == gap["gap_id"] and d["body"]["answer"] == answer
    assert {r["rule_id"]: r for r in pending["rules"]}["rule-capex-finance-config"]["authority"] == "OBSERVED"       # the observed rules are untouched
    assert gap["gap_id"] not in {g["gap_id"] for g in gap_queue(env.store, env.tenant, env.ws)["gaps"]}              # not asked again
    again = analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a2")
    assert again["replayed"] is True and again["snapshot_id"] == out["snapshot_id"]                  # analysing the answered state adds nothing and asks nothing again
    assert gap["gap_id"] not in {g["gap_id"] for g in gap_queue(env.store, env.tenant, env.ws)["gaps"]}


def test_actor_answer_fills_a_missing_actor_on_a_target_not_on_the_observed_rule(env, analysed):
    admin = env.admin_client()
    gap = find(env, "missing_actor", "rule-emergency-routing")
    answer = {"actor": "Plant Manager"}
    pv = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer=answer).json()
    assert pv["resulting_rule"]["actor_ref"] == "role-plant-manager" and "actor_ref" not in pv["resulting_rule"]["unknown_fields"]
    post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote="The plant manager approves emergencies.", preview_hash=pv["preview_hash"])
    rules = {r["rule_id"]: r for r in business_view(env.store, env.tenant, env.ws)["pending"]["rules"]}
    assert rules["rule-emergency-routing"]["actor_ref"] is None and rules["rule-emergency-routing-target"]["actor_ref"] == "role-plant-manager"


def test_duplicate_submit_is_a_replay(env, analysed):
    admin = env.admin_client()
    gap = find(env, "conflicting_facts", "rule-capex-finance-config")
    args = dict(gap_id=gap["gap_id"], answer={"choose_rule_id": "rule-capex-finance-policy"}, quote="Policy wins.")
    pv = post(admin, env, "answer-preview", gap_id=args["gap_id"], answer=args["answer"]).json()
    first = post(admin, env, "answer", preview_hash=pv["preview_hash"], **args).json()
    n = counts(env)
    # the same request again: the preview is stale (the knowledge moved), which is refused; a fresh preview then reports it as already recorded
    stale = post(admin, env, "answer", preview_hash=pv["preview_hash"], **args)
    assert stale.status_code == 409 and stale.json()["code"] == "preview_stale" and counts(env) == n
    pv2 = post(admin, env, "answer-preview", gap_id=args["gap_id"], answer=args["answer"]).json()
    assert pv2["noop"] is True
    second = post(admin, env, "answer", preview_hash=pv2["preview_hash"], **args).json()
    assert second["noop"] is True and counts(env) == n and first["decision_id"]


def test_contradictory_answer_reopens_and_asks_instead_of_overwriting(env, analysed):
    admin = env.admin_client()
    gap = find(env, "conflicting_facts", "rule-capex-finance-config")
    for choice, quote in (("rule-capex-finance-config", "2 million."), ("rule-capex-finance-policy", "No, 5 million.")):
        answer = {"choose_rule_id": choice}
        pv = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer=answer).json()
        assert post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote=quote, preview_hash=pv["preview_hash"]).status_code == 200
    queue = gap_queue(env.store, env.tenant, env.ws)
    statuses = {g["gap_id"]: g["status"] for g in queue["gaps"]}
    assert statuses[gap["gap_id"]] == "REOPENED" and any(g["kind"] == "conflicting_facts" and g["priority"] == "critical" and g["gap_id"] != gap["gap_id"] for g in queue["gaps"])
    rules = {r["rule_id"]: r for r in business_view(env.store, env.tenant, env.ws)["pending"]["rules"]}
    assert "rule-capex-finance-policy-target" not in rules                            # the second answer did not silently replace the first
    assert "rule-capex-finance-config-target" in rules


def test_silent_accept_unknown_cannot_close_a_critical_gap(env, analysed):
    admin = env.admin_client()
    gap = find(env, "conflicting_facts", "rule-capex-finance-config")
    r = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer={"accept_unknown": True})
    assert r.status_code == 422 and "silent accept_unknown" in r.text
    pv = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer={"accept_unknown": True, "reason": "Finance will decide next quarter."}).json()
    assert post(admin, env, "answer", gap_id=gap["gap_id"], answer={"accept_unknown": True, "reason": "Finance will decide next quarter."},
                quote="Leave it open.", preview_hash=pv["preview_hash"]).status_code == 200
    q = gap_queue(env.store, env.tenant, env.ws)
    assert gap["gap_id"] not in {g["gap_id"] for g in q["gaps"]} and q["critical_accepted_unknown"] == 1      # visible as an accepted unknown, counted as critical


def test_bad_answers_are_refused_and_write_nothing(env, analysed):
    admin = env.admin_client()
    gap = find(env, "conflicting_facts", "rule-capex-finance-config")
    n = counts(env)
    assert post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer={"choose_rule_id": "rule-invented"}).json()["code"] == "invented_rule_id"
    assert post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer={"actor": "x"}).status_code == 422
    assert post(admin, env, "answer-preview", gap_id="gap-nope", answer={"actor": "x"}).status_code == 422
    actor_gap = find(env, "missing_actor", "rule-emergency-routing")
    assert post(admin, env, "answer-preview", gap_id=actor_gap["gap_id"], answer={"condition": {"field": "a.x", "op": "regex", "value": "x"}}).status_code == 422
    answer = {"choose_rule_id": "rule-capex-finance-config"}
    assert post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote="x", preview_hash="not-the-preview").json()["code"] == "preview_stale"
    pv = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer=answer).json()
    assert post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote="  ", preview_hash=pv["preview_hash"]).status_code == 422   # their own words are required
    assert counts(env) == n


def test_wrong_role_revoked_user_and_stale_source_are_refused(env, analysed):
    admin, req = env.admin_client(), env.req_client()
    gap = find(env, "conflicting_facts", "rule-capex-finance-config")
    answer = {"choose_rule_id": "rule-capex-finance-config"}
    pv = post(admin, env, "answer-preview", gap_id=gap["gap_id"], answer=answer).json()
    n = counts(env)
    assert post(req, env, "answer-preview", gap_id=gap["gap_id"], answer=answer).status_code == 403            # requester: no knowledge.confirm
    assert post(req, env, "answer", gap_id=gap["gap_id"], answer=answer, quote="x", preview_hash=pv["preview_hash"]).status_code == 403
    anonymous = env.admin_client()
    anonymous.cookies.clear()
    assert post(anonymous, env, "answer", gap_id=gap["gap_id"], answer=answer, quote="x", preview_hash=pv["preview_hash"]).status_code in (401, 403)
    head = env.store.head(env.tenant, env.ws)
    digest = env.store.stage_blob(b"moved")
    env.store.publish_source(env.tenant, env.ws, "src-moved", {"artifact-new": digest}, expected_version=head.version,
                             operation="source.import.commit", idempotency_key="moved")
    r = post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote=QUOTE, preview_hash=pv["preview_hash"])
    assert r.status_code == 409 and r.json()["code"] == "stale_base"                                           # source moved after the preview
    PostgresIdentityStore(env.dsn).revoke_membership(env.tenant, f"p-{env.nonce}", env.ws, env.admin)
    assert post(admin, env, "answer", gap_id=gap["gap_id"], answer=answer, quote=QUOTE, preview_hash=pv["preview_hash"]).status_code in (401, 403, 409)
    assert counts(env) == n


def test_plan_is_pure_and_answers_never_change_observed_rules():
    rules = observed_rules()
    gaps = __import__("core.change_intelligence.v2.business_graph", fromlist=["x"]).derive_gaps(rules)
    gap = next(g for g in gaps if g["kind"] == "missing_condition")
    plan = ba.plan_answer(rules, gaps, gap["gap_id"], {"condition": {"field": "request.process", "op": "eq", "value": "EMERGENCY"}}, decision_id="decision-x")
    assert plan["target_rule"]["authority"] == "TARGET" and plan["target_rule"]["condition"]["op"] == "eq"
    assert rules == observed_rules()                                                      # inputs not mutated
    bk.validate_business_rule(plan["target_rule"])


def observed_rules():
    import json
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "final_knowledge_loop" / "expected_knowledge.json"
    return json.loads(p.read_text(encoding="utf-8"))["observed_rules"]
