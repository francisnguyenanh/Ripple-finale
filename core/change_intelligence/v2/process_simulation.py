# -*- coding: utf-8 -*-
"""Process simulation of ONE approval process (AIX-S1 / MA-S1 engine): pure, seeded, deterministic; no model, no I/O.

Computational actors are states plus declared behaviour policies, never model calls: requests arrive on working days at a declared rate with a declared mix (amount bands,
CAPEX share, high-risk share, emergency share); each request is routed by the deterministic policy evaluator (MON-POL) over the rule set of the scenario (baseline or target);
every approver role is a queue with a declared capacity per working day; a request that needs several approvals goes through them in order. KPIs per replication and across
replications: cycle time (mean, median, p95), SLA breach share, served count and utilisation per role, route mix, unsupported (INDETERMINATE) share; conservation and event-order
checks. Same inputs and seed -> same result. Everything is a simulation of the DECLARED assumptions: no calibration, no behavioural response, no forecast.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.policy_decision import decide
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-process-simulation/1"
ENGINE_VERSION = "process-simulation/1"
BOUNDS = {"horizon_days": (1, 90), "replications": (1, 20), "arrival_rate_per_day": (0.0, 200.0), "capacity_per_day": (0.01, 500.0), "sla_days": (0.1, 60.0), "max_requests": 20000}
SCENARIO_KEYS = {"label", "policy", "arrival_rate_per_day", "horizon_days", "replications", "capacity", "request_mix", "sla_days"}
FACT_KEYS = ("process", "request.net_amount_jpy", "request.category", "supplier.risk")
MIX_KEYS = {"amounts", "capex_share", "high_risk_share", "emergency_share"}
DEFAULT_MIX = {"amounts": [{"share": 0.5, "amount_jpy": 400000}, {"share": 0.3, "amount_jpy": 1200000}, {"share": 0.15, "amount_jpy": 2500000}, {"share": 0.05, "amount_jpy": 6000000}],
               "capex_share": 0.2, "high_risk_share": 0.1, "emergency_share": 0.05}


def validate_scenario(s: Mapping, *, known_roles: Iterable[str] = ()) -> dict:
    """A typed scenario inside the bounds; raises ValueError(reason). Capacity keys must be known approver roles (or `manual_review`)."""
    if not isinstance(s, Mapping) or set(s) != SCENARIO_KEYS:
        raise ValueError("keys")
    if not isinstance(s["label"], str) or not s["label"].strip() or len(s["label"]) > 80:
        raise ValueError("label")
    if s["policy"] not in ("baseline", "target"):
        raise ValueError("policy")
    out = {"label": " ".join(s["label"].split()), "policy": s["policy"]}
    for key in ("arrival_rate_per_day", "sla_days"):
        v = s[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (BOUNDS[key][0] <= float(v) <= BOUNDS[key][1]):
            raise ValueError(key)
        out[key] = float(v)
    for key in ("horizon_days", "replications"):
        v = s[key]
        if isinstance(v, bool) or not isinstance(v, int) or not (BOUNDS[key][0] <= v <= BOUNDS[key][1]):
            raise ValueError(key)
        out[key] = v
    cap = s["capacity"]
    if not isinstance(cap, Mapping) or not cap or len(cap) > 32:
        raise ValueError("capacity")
    roles = set(known_roles) | {"manual_review"}
    out["capacity"] = {}
    for role, v in cap.items():
        if not isinstance(role, str) or (roles and role not in roles) or isinstance(v, bool) or not isinstance(v, (int, float)) or not (BOUNDS["capacity_per_day"][0] <= float(v) <= BOUNDS["capacity_per_day"][1]):
            raise ValueError(f"capacity:{role}")
        out["capacity"][role] = float(v)
    mix = s["request_mix"] if s["request_mix"] is not None else DEFAULT_MIX
    if not isinstance(mix, Mapping) or set(mix) != MIX_KEYS or not isinstance(mix["amounts"], list) or not (1 <= len(mix["amounts"]) <= 8):
        raise ValueError("request_mix")
    total = 0.0
    amounts = []
    for a in mix["amounts"]:
        if not isinstance(a, Mapping) or set(a) != {"share", "amount_jpy"} or isinstance(a["share"], bool) or not isinstance(a["share"], (int, float)) or a["share"] < 0 \
                or isinstance(a["amount_jpy"], bool) or not isinstance(a["amount_jpy"], (int, float)) or a["amount_jpy"] < 0:
            raise ValueError("request_mix.amounts")
        total += float(a["share"])
        amounts.append({"share": float(a["share"]), "amount_jpy": int(a["amount_jpy"])})
    if not (0.999 <= total <= 1.001):
        raise ValueError("request_mix.amounts_shares_sum")
    for key in ("capex_share", "high_risk_share", "emergency_share"):
        v = mix[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
            raise ValueError(f"request_mix.{key}")
    out["request_mix"] = {"amounts": amounts, "capex_share": float(mix["capex_share"]), "high_risk_share": float(mix["high_risk_share"]), "emergency_share": float(mix["emergency_share"])}
    if out["arrival_rate_per_day"] * out["horizon_days"] * out["replications"] > BOUNDS["max_requests"]:
        raise ValueError("too_many_requests")
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def _working(day: float) -> float:
    """The next working-day time at or after `day` (days 0..4 of each week work; 5, 6 do not)."""
    d = int(math.floor(day))
    while d % 7 >= 5:
        d += 1
        day = float(d)
    return day


def _route(rules, gaps, req, pin) -> dict:
    facts = {"process": req["process"], "request.net_amount_jpy": req["amount_jpy"], "request.category": req["category"], "supplier.risk": req["supplier_risk"]}
    d = decide(rules, gaps, facts, pin=pin)
    actors = sorted({o.get("by") or "unknown" for o in d.get("obligations") or []})
    return {"decision": d["decision"], "approvers": actors}


def _replication(rules, gaps, scenario: Mapping, seed: int, rep: int) -> dict:
    rng = random.Random(seed * 1009 + rep)
    mix = scenario["request_mix"]
    cap = scenario["capacity"]
    horizon = scenario["horizon_days"]
    free: dict = {}
    served: dict = {}
    waited: dict = {}
    arrivals_list = []
    for day in range(horizon):
        if day % 7 >= 5:
            continue
        for _ in range(_poisson(rng, scenario["arrival_rate_per_day"])):
            u, acc = rng.random(), 0.0
            amount = mix["amounts"][-1]["amount_jpy"]
            for a in mix["amounts"]:
                acc += a["share"]
                if u <= acc:
                    amount = a["amount_jpy"]
                    break
            arrivals_list.append({"arrival": day + rng.random(), "amount_jpy": amount, "category": "CAPEX" if rng.random() < mix["capex_share"] else "OPEX",
                             "supplier_risk": "HIGH" if rng.random() < mix["high_risk_share"] else "LOW", "process": "EMERGENCY" if rng.random() < mix["emergency_share"] else "STANDARD"})
    arrivals_list.sort(key=lambda r: r["arrival"])
    cycles, routes, denied, unsupported, in_system = [], {}, 0, 0, 0
    pin = {"scenario": scenario["label"]}
    for req in arrivals_list:
        r = _route(rules, gaps, req, pin)
        key = "|".join(r["approvers"]) if r["approvers"] else r["decision"].lower()
        routes[key] = routes.get(key, 0) + 1
        if r["decision"] == "INDETERMINATE":
            unsupported += 1
            continue
        if r["decision"] == "DENY":
            denied += 1
            continue
        t = req["arrival"]
        for actor in r["approvers"]:
            c = cap.get(actor)
            if c is None:
                c = cap.get("manual_review", 1.0)
            start = _working(max(t, free.get(actor, 0.0)))
            if start > t:
                waited[actor] = waited.get(actor, 0) + 1
            t = start + 1.0 / c
            free[actor] = t
            served[actor] = served.get(actor, 0) + 1
        if t > horizon:
            in_system += 1
        else:
            cycles.append(t - req["arrival"])
    working_days = sum(1 for d in range(horizon) if d % 7 < 5)
    kpis = {"arrivals": len(arrivals_list), "completed": len(cycles), "denied": denied, "unsupported": unsupported, "in_system_at_end": in_system,
            "cycle_time_mean_days": round(statistics.fmean(cycles), 3) if cycles else None, "cycle_time_median_days": round(statistics.median(cycles), 3) if cycles else None,
            "cycle_time_p95_days": round(sorted(cycles)[max(0, int(math.ceil(0.95 * len(cycles))) - 1)], 3) if cycles else None,
            "sla_breach_share": round(sum(1 for c in cycles if c > scenario["sla_days"]) / len(cycles), 4) if cycles else None,
            "roles": {a: {"served": served.get(a, 0), "waited": waited.get(a, 0), "utilisation": round(served.get(a, 0) / (cap.get(a, cap.get("manual_review", 1.0)) * working_days), 4) if working_days else None}
                      for a in sorted(set(served) | set(cap))},
            "routes": dict(sorted(routes.items())), "working_days": working_days}
    kpis["conservation_ok"] = kpis["arrivals"] == kpis["completed"] + denied + unsupported + in_system
    return kpis


def _summary(values: list) -> Optional[dict]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"mean": round(statistics.fmean(vals), 4), "min": round(min(vals), 4), "max": round(max(vals), 4), "n": len(vals)}


def simulate(rules: Iterable[Mapping], gaps: Iterable[Mapping], scenario: Mapping, *, seed: int) -> dict:
    """One scenario over its replications. The scenario must already be validated."""
    from core.change_intelligence.v2.roi_replay import evaluable_rules
    rules, not_evaluated = evaluable_rules(list(rules), FACT_KEYS)                  # only rules the engine's facts can decide route anything; the rest is coverage, never a silent clear
    reps = [_replication(rules, list(gaps), scenario, seed, r) for r in range(scenario["replications"])]
    roles = sorted({a for k in reps for a in k["roles"]})
    out = {"label": scenario["label"], "policy": scenario["policy"], "seed": seed, "replications": len(reps),
           "kpis": {"cycle_time_mean_days": _summary([k["cycle_time_mean_days"] for k in reps]), "cycle_time_median_days": _summary([k["cycle_time_median_days"] for k in reps]),
                    "cycle_time_p95_days": _summary([k["cycle_time_p95_days"] for k in reps]), "sla_breach_share": _summary([k["sla_breach_share"] for k in reps]),
                    "arrivals": _summary([k["arrivals"] for k in reps]), "completed": _summary([k["completed"] for k in reps]), "unsupported_share": _summary([(k["unsupported"] / k["arrivals"]) if k["arrivals"] else None for k in reps]),
                    "roles": {a: {"served": _summary([k["roles"].get(a, {}).get("served") for k in reps]), "utilisation": _summary([k["roles"].get(a, {}).get("utilisation") for k in reps])} for a in roles},
                    "routes": sorted({r for k in reps for r in k["routes"]})},
           "checks": {"conservation_ok": all(k["conservation_ok"] for k in reps), "replications_agree_on_routes": len({tuple(sorted(k["routes"])) for k in reps}) == 1},
           "scenario": dict(scenario), "rules_hash": canonical_hash(rules), "engine_version": ENGINE_VERSION,
           "coverage": {"rules_evaluated": sorted(r["rule_id"] for r in rules), "rules_not_evaluated": not_evaluated, "facts": list(FACT_KEYS)}}
    out["result_hash"] = canonical_hash({k: v for k, v in out.items() if k != "result_hash"})
    return out


CALIBRATION_KPIS = ("cycle_time_mean_days", "cycle_time_p95_days", "sla_breach_share")


def convergence(results: Iterable[Mapping], *, tolerance_rel: float = 0.1) -> dict:
    """Do independent seeds agree? For each headline KPI: spread of the per-seed means relative to their mean; `converged` when every spread is within `tolerance_rel`.
    Statistical convergence of the engine says nothing about the realism of the assumptions (and nothing about any agents' agreement)."""
    results = list(results)
    if len(results) < 2:
        return {"status": "insufficient_seeds", "seeds": [r.get("seed") for r in results], "kpis": {}, "tolerance_rel": tolerance_rel, "converged": None}
    kpis = {}
    ok = True
    for k in CALIBRATION_KPIS:
        means = [((r["kpis"].get(k) or {}).get("mean")) for r in results]
        means = [m for m in means if m is not None]
        if len(means) < 2:
            kpis[k] = {"status": "not_available"}
            ok = False
            continue
        centre = statistics.fmean(means)
        spread = (max(means) - min(means)) / centre if centre else (0.0 if max(means) == min(means) else float("inf"))
        within = spread <= tolerance_rel
        ok = ok and within
        kpis[k] = {"means": [round(m, 4) for m in means], "spread_rel": round(spread, 4), "within_tolerance": within}
    return {"status": "converged" if ok else "not_converged", "seeds": [r.get("seed") for r in results], "kpis": kpis, "tolerance_rel": tolerance_rel, "converged": ok}


def calibrate(baseline: Mapping, observed: Mapping, *, tolerance_rel: Mapping | None = None) -> dict:
    """Backtest of the BASELINE scenario against an observed holdout `{cycle_time_mean_days?, cycle_time_p95_days?, sla_breach_share?, source, n}` with a tolerance per KPI the domain
    owner declared. Returns per-KPI relative error and a status: `uncalibrated` (no observed data), `within_tolerance`, `outside_tolerance`. Only a status of `within_tolerance` on
    data the owner accepted may turn 'exploratory' into 'calibrated'; this function never does that by itself."""
    if not observed or not any(observed.get(k) is not None for k in CALIBRATION_KPIS):
        return {"status": "uncalibrated", "kpis": {}, "observed": None, "statement": "No observed holdout was provided: the simulation is exploratory and not a forecast."}
    tol = dict(tolerance_rel or {})
    kpis, ok, compared = {}, True, 0
    for k in CALIBRATION_KPIS:
        obs = observed.get(k)
        sim = (baseline["kpis"].get(k) or {}).get("mean")
        if obs is None or sim is None:
            kpis[k] = {"status": "not_compared"}
            continue
        t = float(tol.get(k, tol.get("default", 0.2)))
        err = abs(sim - obs) / abs(obs) if obs else (0.0 if sim == obs else float("inf"))
        within = err <= t
        ok = ok and within
        compared += 1
        kpis[k] = {"observed": obs, "simulated": round(sim, 4), "abs_rel_error": round(err, 4), "tolerance_rel": t, "within_tolerance": within}
    status = "within_tolerance" if ok and compared else "outside_tolerance" if compared else "uncalibrated"
    return {"status": status, "kpis": kpis, "observed": {"source": observed.get("source"), "n": observed.get("n")}, "compared": compared,
            "statement": {"within_tolerance": "The baseline reproduces the observed holdout within the declared tolerance on the compared KPIs; the data owner's acceptance is still required before any forecast claim.",
                          "outside_tolerance": "The baseline does not reproduce the observed holdout within the declared tolerance: results stay exploratory.",
                          "uncalibrated": "No comparable KPI in the observed holdout: exploratory."}[status]}


def compare(baseline: Mapping, scenarios: Iterable[Mapping]) -> dict:
    """Baseline vs scenarios on the headline KPIs, with the limits every reader must see."""
    rows = []
    for s in scenarios:
        row = {"label": s["label"], "policy": s["policy"]}
        for k in ("cycle_time_mean_days", "cycle_time_p95_days", "sla_breach_share", "unsupported_share"):
            b, t = (baseline["kpis"].get(k) or {}).get("mean"), (s["kpis"].get(k) or {}).get("mean")
            row[k] = {"baseline": b, "scenario": t, "delta": round(t - b, 4) if (b is not None and t is not None) else None}
        rows.append(row)
    return {"schema": SCHEMA, "engine_version": ENGINE_VERSION, "baseline": baseline["label"], "rows": rows,
            "limitations": ["Simulation of declared assumptions (arrival rate, mix, capacities, calendar); no calibration against observed data.",
                            "No behavioural response is modelled (people and vendors do not react to the rule change).",
                            "Routing uses the deterministic policy evaluator; INDETERMINATE requests are counted as unsupported, never as cleared.",
                            "Exploratory results, not a forecast; intervals are min/max over the declared replications, not confidence intervals."]}
