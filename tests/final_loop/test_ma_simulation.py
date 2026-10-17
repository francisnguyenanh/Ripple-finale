"""MA-S1 / AIX-S1 (LOCAL): the seeded process-simulation engine (pure: reproducibility, conservation, bounds, capacity effect) and the simulation workflow through the multi-agent
coordinator (synthetic designer and critic, deterministic engine child over baseline and target rule sets). 0 real provider calls; results are exploratory, never a forecast."""
from __future__ import annotations

import copy
import json

import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from core.change_intelligence.v2 import process_simulation as ps
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import KNOWLEDGE, ws  # noqa: F401  (fixture)
from tests.final_loop.test_ma_review_board import _refs_in, finding, ma_env, obj, settle, start  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (fixture)

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
    raise AssertionError("unknown rubric")


def test_simulation_workflow_runs_designer_and_critic_then_the_engine_over_baseline_and_target(ma_env):
    q = ma_env
    cid, approver = prepared(q)
    use(q, sim_stub)
    r = start(q, q.admin, cid, workflow="simulation")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    st = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert st["caps"]["max_children"] == 2 and {c["role"] for c in st["children"]} == {"scenario_designer", "assumption_critic"}
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
