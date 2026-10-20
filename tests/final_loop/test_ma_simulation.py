"""MA-S1 / AIX-S1 (LOCAL): the seeded process-simulation engine (pure: reproducibility, conservation, bounds, capacity effect) and the simulation workflow through the multi-agent
coordinator (synthetic designer and critic, deterministic engine child over baseline and target rule sets). 0 real provider calls; results are exploratory, never a forecast."""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from core.change_intelligence.v2 import process_simulation as ps
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import KNOWLEDGE, ws  # noqa: F401  (fixture)
from tests.final_loop.test_ma_review_board import _refs_in, finding, ma_env, obj, settle, start  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (fixture)
from tests.pitch_brief.conftest import drain

RULES = KNOWLEDGE["observed_rules"]
ROLES = sorted({r["actor_ref"] for r in RULES if r.get("actor_ref")})


def scenario(**over):
    base = {"label": "test", "policy": "baseline", "arrival_rate_per_day": 8.0, "horizon_days": 20, "replications": 3, "capacity": {r: 6.0 for r in ROLES} | {"manual_review": 2.0},
            "request_mix": None, "sla_days": 3.0}
    base.update(over)
    return base


# ---- pure engine ----------------------------------------------------------------------------------------------------------------------------------------------------

def test_same_inputs_and_seed_reproduce_the_result_and_conservation_holds():
    s = ps.validate_scenario(scenario(), known_roles=ROLES)
    a, b = ps.simulate(RULES, [], s, seed=7), ps.simulate(RULES, [], s, seed=7)
    assert a["result_hash"] == b["result_hash"] and a["checks"]["conservation_ok"] is True and a["kpis"]["arrivals"]["n"] == 3
    c = ps.simulate(RULES, [], s, seed=8)
    assert c["result_hash"] != a["result_hash"]                                                                    # another seed, another sample; still conserved
    assert c["checks"]["conservation_ok"] is True


def test_capacity_and_threshold_change_the_kpis_in_the_expected_direction():
    s_fast = ps.validate_scenario(scenario(), known_roles=ROLES)
    s_slow = ps.validate_scenario(scenario(label="slow manager", capacity={r: 6.0 for r in ROLES} | {"role-manager": 0.5, "manual_review": 2.0}), known_roles=ROLES)
    fast, slow = ps.simulate(RULES, [], s_fast, seed=1), ps.simulate(RULES, [], s_slow, seed=1)
    assert slow["kpis"]["cycle_time_mean_days"]["mean"] > fast["kpis"]["cycle_time_mean_days"]["mean"] and slow["kpis"]["sla_breach_share"]["mean"] >= fast["kpis"]["sla_breach_share"]["mean"]
    target = copy.deepcopy(RULES)
    for r in target:
        if r["rule_id"] == "rule-standard-manager-review":
            r["condition"] = dict(r["condition"], value=1500000)
    t = ps.simulate(target, [], s_fast, seed=1)
    served_base = fast["kpis"]["roles"]["role-manager"]["served"]["mean"]
    served_target = t["kpis"]["roles"]["role-manager"]["served"]["mean"]
    assert served_target <= served_base                                                                              # a higher manager threshold never adds manager approvals
    cmp = ps.compare(fast, [slow, t])
    assert cmp["rows"][0]["cycle_time_mean_days"]["delta"] > 0 and len(cmp["limitations"]) >= 3 and "forecast" in " ".join(cmp["limitations"])


@pytest.mark.parametrize("bad", [scenario(horizon_days=500), scenario(replications=0), scenario(capacity={"role-ceo": 1.0}), scenario(policy="future"),
                                 scenario(request_mix={"amounts": [{"share": 0.5, "amount_jpy": 1}], "capex_share": 0.1, "high_risk_share": 0.1, "emergency_share": 0.1}),
                                 scenario(arrival_rate_per_day=200, horizon_days=90, replications=20)])
def test_scenarios_outside_the_bounds_are_refused(bad):
    with pytest.raises(ValueError):
        ps.validate_scenario(bad, known_roles=ROLES)


# ---- the workflow through the coordinator ------------------------------------------------------------------------------------------------------------------------------

def sim_stub(prompt: str):
    refs = _refs_in(prompt)
    roles = [r for r in refs if r.startswith("role-")]
    if "SCENARIO DESIGNER" in prompt:
        good = {"label": "target under baseline load", "policy": "target", "arrival_rate_per_day": 8, "horizon_days": 20, "replications": 3, "capacity": {r: 6.0 for r in roles} | {"manual_review": 2},
                "request_mix": None, "sla_days": 3}
        bad = dict(good, label="too long", horizon_days=500)
        return {"status": "OK", "scenarios": [good, dict(good, label="slower manager", capacity={r: 6.0 for r in roles} | {"role-manager": 1.0, "manual_review": 2}), bad], "notes": ["Load kept equal to the baseline."]}
    if "ASSUMPTION CRITIC" in prompt:
        rule = next(r for r in refs if r.startswith("rule-"))
        return obj(findings=[finding("operational", "medium", "Arrivals are assumed uniform over working days; month-end peaks are not modelled.", [rule])])
    if "RESULT INTERPRETER" in prompt:                                                                   # LLM-S1, SYNTHETIC: cites the engine's metric lines
        import re as _re
        metrics = dict((m.group(1), float(m.group(2))) for m in _re.finditer(r"metric (\S+) = (-?[0-9.]+)", prompt))
        base = next(k for k in metrics if k.endswith("cycle_time_mean_days.baseline"))
        scen = base.replace(".baseline", ".scenario")
        return {"status": "OK", "notes": [], "statements": [
            {"text": f"Mean cycle time moves from {metrics[base]:.2f} to {metrics[scen]:.2f} days under this scenario.", "metric_refs": [base, scen], "kind": "trade_off"},
            {"text": "Mean cycle time triples to 99 days.", "metric_refs": [scen], "kind": "risk"},
            {"text": "This change will reduce approval costs.", "metric_refs": [scen], "kind": "conditional_recommendation"}]}
    raise AssertionError("unknown rubric")


def test_simulation_workflow_runs_designer_and_critic_then_the_engine_over_baseline_and_target(ma_env):
    q = ma_env
    cid, approver = prepared(q)
    use(q, sim_stub)
    r = start(q, q.admin, cid, workflow="simulation")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    st = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert st["caps"]["max_children"] == 3 and {c["role"] for c in st["children"]} == {"scenario_designer", "assumption_critic"}            # + the result interpreter (LLM-S1)
    got = settle(q, q.admin, run_id)
    assert got["status"] == "REVIEW_READY", json.dumps(got["events"])[:600]
    sim = got["report"]["simulation"]
    assert sim["proposed"] == 2 and sim["rejected_scenarios"][0]["reason"].startswith("scenario_invalid:horizon_days") and sim["scenarios_proposed"] == 2
    assert sim["baseline"]["checks"]["conservation_ok"] is True and len(sim["scenarios"]) == 2 and {s["policy"] for s in sim["scenarios"]} == {"target"}
    assert sim["comparison"]["rows"][0]["label"] == "target under baseline load" and "forecast" in " ".join(sim["comparison"]["limitations"])
    slower = next(s for s in sim["scenarios"] if s["label"] == "slower manager")
    assert slower["kpis"]["cycle_time_mean_days"]["mean"] >= sim["scenarios"][0]["kpis"]["cycle_time_mean_days"]["mean"]
    assert sim["approver_roles"] and sim["seed"] == 20261017
    engine = next(c for c in got["children"] if c["role"] == "engine")
    assert engine["status"] == "COMPLETED" and (engine["cost_usd"] in (None, "0", "0.0") or float(engine["cost_usd"]) == 0)
    assert any(f["category"] == "operational" for f in got["report"]["findings"]) and got["budget"]["holds"] is True
    assert "exploratory" in " ".join(got["report"]["limitations"]).lower()
    inter = sim["interpretation"]                                                                                   # LLM-S1: only statements grounded in the cited metrics survive
    assert [x["kind"] for x in inter["statements"]] == ["trade_off"] and sorted(r["reason"] for r in inter["rejected"]) == ["number_not_in_cited_metrics", "overstates_certainty"]
    assert "not a forecast" in inter["label"] and next(c for c in got["children"] if c["role"] == "result_interpreter")["status"] == "COMPLETED"


def test_a_failed_interpreter_leaves_the_engine_report_intact_and_labelled(ma_env):
    q = ma_env
    cid, approver = prepared(q)

    def stub(prompt):
        if "RESULT INTERPRETER" in prompt:
            return {"status": "OK", "statements": "not a list", "notes": []}                                       # schema violation: the optional role fails
        return sim_stub(prompt)
    use(q, stub)
    got = settle(q, q.admin, start(q, q.admin, cid, workflow="simulation").json()["run_id"])
    assert got["status"] == "REVIEW_READY" and "interpretation" not in got["report"]["simulation"] and got["report"]["simulation"]["comparison"]["rows"]
    assert next(c for c in got["children"] if c["role"] == "result_interpreter")["status"] == "FAILED"


# ---- AIX-S1 calibration harness (local): convergence across seeds and a backtest against a caller-supplied holdout; real calibration data stays OPEN ---------------------

def test_convergence_and_calibration_are_pure_deterministic_and_never_turn_exploratory_into_a_forecast():
    base = {**scenario(), "label": "baseline", "policy": "baseline"}
    runs = [ps.simulate(RULES, [], ps.validate_scenario(base, known_roles=[k for k in base["capacity"] if k != "manual_review"]), seed=s) for s in (1, 2, 3)]
    conv = ps.convergence(runs, tolerance_rel=0.5)
    assert conv["seeds"] == [1, 2, 3] and conv["status"] in ("converged", "not_converged") and set(conv["kpis"]) == set(ps.CALIBRATION_KPIS)
    assert ps.convergence(runs[:1])["status"] == "insufficient_seeds"
    tight = ps.convergence(runs, tolerance_rel=0.0)
    assert tight["converged"] is False or all(k["spread_rel"] == 0 for k in tight["kpis"].values() if "spread_rel" in k)
    assert ps.calibrate(runs[0], {})["status"] == "uncalibrated"
    obs_mean = runs[0]["kpis"]["cycle_time_mean_days"]["mean"]
    good = ps.calibrate(runs[0], {"source": "synthetic holdout", "n": 50, "cycle_time_mean_days": obs_mean}, tolerance_rel={"default": 0.1})
    assert good["status"] == "within_tolerance" and good["compared"] == 1 and good["kpis"]["cycle_time_mean_days"]["abs_rel_error"] == 0 and "acceptance is still required" in good["statement"]
    bad = ps.calibrate(runs[0], {"source": "synthetic holdout", "n": 50, "cycle_time_mean_days": obs_mean * 3}, tolerance_rel={"cycle_time_mean_days": 0.1})
    assert bad["status"] == "outside_tolerance" and bad["kpis"]["cycle_time_mean_days"]["within_tolerance"] is False and "exploratory" in bad["statement"]


def test_the_simulation_run_carries_the_convergence_check_and_an_owner_supplied_holdout_backtest(ma_env):
    q = ma_env
    cid, approver = prepared(q)
    use(q, sim_stub)
    bad = q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "simulation", "calibration": {"observed": {"cycle_time_mean_days": 1}}})
    assert bad.status_code == 409 and bad.json()["code"] == "calibration_invalid"                                      # a holdout without source and n is refused
    r = q.admin.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": "simulation",
                                          "calibration": {"observed": {"source": "synthetic holdout written for the test", "n": 40, "cycle_time_mean_days": 0.9, "sla_breach_share": 0.05},
                                                          "tolerance_rel": {"default": 0.5}, "seeds": 3}})
    assert r.status_code == 200, r.text
    got = settle(q, q.admin, r.json()["run_id"])
    assert got["status"] == "REVIEW_READY"
    sim = got["report"]["simulation"]
    assert sim["convergence"]["seeds"] == [20261017, 20261018, 20261019] and sim["convergence"]["status"] in ("converged", "not_converged")
    assert sim["calibration"]["status"] in ("within_tolerance", "outside_tolerance") and sim["calibration"]["compared"] == 2 and sim["calibration"]["observed"]["n"] == 40
    assert "forecast" in " ".join(sim["comparison"]["limitations"])                                                     # the comparison stays labelled exploratory regardless of the backtest


def test_an_interpretation_keeps_only_statements_grounded_in_cited_engine_metrics():
    """LLM-S1 (pure): a share may be written as a percentage; an unknown metric, an unknown kind, unsafe text, an uncited number or a certainty are rejected with a reason."""
    comp = {"rows": [{"label": "target load", "policy": "target", "sla_breach_share": {"baseline": 0.1, "scenario": 0.25, "delta": 0.15}}]}
    mt = ma.metric_table(comp)
    assert mt == {"s1.sla_breach_share.baseline": 0.1, "s1.sla_breach_share.scenario": 0.25, "s1.sla_breach_share.delta": 0.15}
    assert ma.scenario_labels(comp) == {"s1": {"label": "target load", "policy": "target"}}
    out = ma.validate_interpretation_object({"status": "OK", "notes": ["n"], "statements": [
        {"text": "SLA breaches go from 10% to 25%.", "metric_refs": ["s1.sla_breach_share.baseline", "s1.sla_breach_share.scenario"], "kind": "trade_off"},
        {"text": "Breaches rise.", "metric_refs": ["s9.sla_breach_share.delta"], "kind": "risk"},
        {"text": "Breaches rise.", "metric_refs": ["s1.sla_breach_share.delta"], "kind": "prophecy"},
        {"text": "See <script>x</script>", "metric_refs": ["s1.sla_breach_share.delta"], "kind": "risk"},
        {"text": "The forecast is clear.", "metric_refs": ["s1.sla_breach_share.delta"], "kind": "risk"}]}, mt)
    assert [s["text"] for s in out["statements"]] == ["SLA breaches go from 10% to 25%."]
    assert [r["reason"] for r in out["rejected"]] == ["metric_not_in_engine_results", "kind_unknown", "unsafe_or_empty_text", "overstates_certainty"]
    with pytest.raises(ValueError):
        ma.validate_interpretation_object({"status": "OK", "statements": [{"text": "x"}], "notes": []}, mt)


def test_capacity_sent_as_role_pairs_is_validated_like_the_map_and_no_open_object_is_left_in_the_schema():
    """Live smoke LHA-SMOKE-20261018-01: every designer scenario was refused (`scenario_invalid:capacity`) because the capacity map could only arrive empty."""
    import json as _json
    from core.change_intelligence.v2 import ma_orchestration as _ma
    good = {"label": "target", "policy": "target", "arrival_rate_per_day": 8, "horizon_days": 20, "replications": 3, "request_mix": None, "sla_days": 3,
            "capacity": [{"role": "role-manager", "per_day": 6}, {"role": "manual_review", "per_day": 2}]}
    dup = dict(good, label="dup", capacity=[{"role": "role-manager", "per_day": 6}, {"role": "role-manager", "per_day": 1}])
    out = _ma.validate_scenarios_object({"status": "OK", "scenarios": [good, dup], "notes": []}, ["role-manager"])
    assert out["scenarios"][0]["capacity"] == {"role-manager": 6.0, "manual_review": 2.0} and [r["reason"] for r in out["rejected"]] == ["scenario_invalid:pairs"]
    cfg = _json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "ma_simulation.json").read_text(encoding="utf-8"))
    props = cfg["output_schemas"]["scenarios"]["properties"]["scenarios"]["items"]["properties"]
    assert props["capacity"]["type"] == "array" and props["request_mix"]["properties"]["amounts"]["items"]["properties"]


def test_a_changed_workflow_configuration_starts_a_new_run_instead_of_replaying_the_old_one(ma_env, monkeypatch):
    """Live smoke LHA-SMOKE-20261018-06: after the scenario schema changed, the same request returned the run made under the old configuration."""
    from infra.adapters import v2_ma
    q = ma_env
    cid, _ = prepared(q)
    use(q, sim_stub)
    first = start(q, q.admin, cid, workflow="simulation").json()["run_id"]
    assert start(q, q.admin, cid, workflow="simulation").json()["run_id"] == first                       # same configuration: the same run (idempotent)
    settle(q, q.admin, first)
    real = v2_ma.load_config
    monkeypatch.setattr(v2_ma, "load_config", lambda workflow="review_board": (real(workflow)[0], "0" * 64))
    second = start(q, q.admin, cid, workflow="simulation")
    assert second.status_code == 200 and second.json()["run_id"] != first                                 # a changed configuration: a new run; the old one stays readable
    assert q.admin.get(f"/v2/ma/runs/{first}", params={"workspace_id": q.env.ws}).json()["status"] == "REVIEW_READY"


def test_a_capacity_role_written_by_name_resolves_only_to_the_one_known_id_it_names():
    """Live smoke LHA-SMOKE-20261018-07: the designer wrote `manager` for `role-manager` in every scenario."""
    from core.change_intelligence.v2 import ma_orchestration as _ma
    base = {"label": "t", "policy": "target", "arrival_rate_per_day": 8, "horizon_days": 20, "replications": 3, "request_mix": None, "sla_days": 3}
    out = _ma.validate_scenarios_object({"status": "OK", "notes": [], "scenarios": [
        dict(base, label="by name", capacity=[{"role": "manager", "per_day": 6}, {"role": "Finance Director", "per_day": 2}]),
        dict(base, label="unknown", capacity=[{"role": "ceo", "per_day": 1}]),
        dict(base, label="twice", capacity=[{"role": "manager", "per_day": 6}, {"role": "role-manager", "per_day": 1}])]}, ["role-manager", "role-finance-director"])
    assert out["scenarios"][0]["capacity"] == {"role-manager": 6.0, "role-finance-director": 2.0}
    assert [r["reason"] for r in out["rejected"]] == ["scenario_invalid:capacity:ceo", "scenario_invalid:pairs"]


# ---- finding (e) (gallery part B): a simulation after other work in the same workspace. Diagnosed on the test DB with the product routes and live worker: the run was never
# stuck; after an interview CONFIRMED an answer the knowledge moved and the start was refused (409 knowledge_moved), and while other jobs held the workspace's slots the
# children waited without the run saying so. These pin the explained states; no timeout was added and nothing is reported as success.

def test_a_run_waiting_for_the_workspace_slots_says_so_and_continues_when_they_free(ma_env):
    q = ma_env
    cid, _ = prepared(q)
    use(q, sim_stub)
    with q.env.store._tx() as cur:                                                                                  # two other jobs of the workspace hold both slots (limit 2)
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND execution_status='COMPLETED' ORDER BY job_id LIMIT 2", (q.env.tenant, q.env.ws))
        held = [r[0] for r in cur.fetchall()]
        assert len(held) == 2
        cur.execute("UPDATE v2_jobs SET execution_status='RUNNING' WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)", (q.env.tenant, q.env.ws, held))
    r = start(q, q.admin, cid, workflow="simulation")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    drain(q.env)                                                                                                    # the real worker: claim-time admission defers the children
    st = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    p = st["progress"]
    assert st["status"] == "RUNNING" and p["phase"] == "waiting_for_slot" and p["children"]["running"] == 0 and p["children"]["queued"] == 2, p
    assert p["workspace_limit"] == 2 and sum(p["workspace_running_other"].values()) == 2 and "Nothing failed" in p["reason"] and "limit of 2" in p["reason"]
    with q.env.store._tx() as cur:
        cur.execute("UPDATE v2_jobs SET execution_status='COMPLETED' WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)", (q.env.tenant, q.env.ws, held))
    got = settle(q, q.admin, run_id)
    assert got["status"] == "REVIEW_READY" and got["progress"] is None


def test_a_simulation_after_the_knowledge_moved_is_refused_with_its_reason_and_starts_nothing(ma_env):
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    q = ma_env
    cid, _ = prepared(q)
    model = use(q, sim_stub)
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")      # e.g. an interview confirmed: the knowledge moves
    r = start(q, q.admin, cid, workflow="simulation")
    assert r.status_code == 409 and r.json()["code"] == "knowledge_moved", r.text
    runs = q.admin.get(f"/v2/business/changes/{cid}/ma", params={"workspace_id": q.env.ws}).json()["runs"]
    assert [x for x in runs if x["workflow"] == "simulation"] == [] and model.calls == 0
