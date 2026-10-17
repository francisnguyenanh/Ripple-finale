"""Job kind "ma.simulate" (MA-S1, deterministic, 0 provider calls): runs the seeded process engine for the baseline rule set (the accepted knowledge the change was confirmed
against) and the target rule set (the accepted rules with the change's `after` rules), for a default baseline scenario and every validated scenario the designer proposed, then
records the KPI comparison into the child row before the join job is created. No model-generated code is executed; the engine is the product's own."""
from __future__ import annotations

from core.change_intelligence.v2 import ma_orchestration as ma
from core.change_intelligence.v2 import process_simulation as ps
from infra.adapters import v2_ma
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget

EXECUTOR_ID = "ma.simulate.v1"
DEFAULT_SEED = 20261017


def default_baseline(roles: list) -> dict:
    return ps.validate_scenario({"label": "baseline (default load)", "policy": "baseline", "arrival_rate_per_day": 8.0, "horizon_days": 20, "replications": 5,
                                 "capacity": {r: 6.0 for r in roles} | {"manual_review": 2.0}, "request_mix": None, "sla_days": 3.0}, known_roles=roles)


class MaSimulateExecutor:
    external = False
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        for key in ("run_id", "child_id", "role", "round"):
            if key not in p:
                raise NonRetryableExecutionError("payload_invalid", key)
        return ExecutionRequest(prompt="", schema=None, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        p = ctx.job["payload"]
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        try:
            result = self._run(ctx, p)
        except BaseException as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            v2_ma.record_child_result(ctx.store, tenant, ws, p["child_id"], status="FAILED", result={"failed": str(reason)[:120], "role": p["role"], "round": p["round"]})
            self._enqueue_join(ctx.store, tenant, ws, p["run_id"], p["child_id"])
            raise
        v2_ma.record_child_result(ctx.store, tenant, ws, p["child_id"], status="COMPLETED", result=result)
        self._enqueue_join(ctx.store, tenant, ws, p["run_id"], p["child_id"])
        return ExecutionOutcome(result=result, model_id=None)

    def _run(self, ctx, p) -> dict:
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        run = v2_ma._run(ctx.store, tenant, ws, p["run_id"])
        if run is None or run["status"] not in ("RUNNING", "JOINING"):
            raise NonRetryableExecutionError("run_not_running", run["status"] if run else "missing")
        from infra.adapters import v2_business
        view = v2_business.business_view(ctx.store, tenant, ws)
        acc = view["accepted"]
        if acc is None or acc["snapshot"]["snapshot_id"] != (run["binding"].get("pin") or {}).get("snapshot_id"):
            raise NonRetryableExecutionError("pin_moved", "the accepted knowledge is not the one the run was started against")
        baseline_rules = list(acc["rules"])
        after = {r["rule_id"]: r["after"] for r in run["context"].get("rule_after", [])}
        target_rules = [after.get(r["rule_id"], r) for r in baseline_rules] + [a for rid, a in after.items() if rid not in {r["rule_id"] for r in baseline_rules}]
        from core.change_intelligence.v2.roi_replay import evaluable_rules
        evaluable, _ = evaluable_rules(baseline_rules + list(after.values()), ps.FACT_KEYS)
        roles = sorted({r.get("actor_ref") for r in evaluable if r.get("actor_ref")})
        scenarios = []
        for a in p.get("synthesis_inputs") or []:
            scenarios += a.get("scenarios") or []
        base_scn = default_baseline(roles)
        base = ps.simulate(baseline_rules, acc["gaps"], base_scn, seed=DEFAULT_SEED)
        calibration_cfg = run["context"].get("calibration") or {}
        seeds = [DEFAULT_SEED + i for i in range(int(calibration_cfg.get("seeds", 3)))]
        conv = ps.convergence([base] + [ps.simulate(baseline_rules, acc["gaps"], base_scn, seed=s) for s in seeds[1:]], tolerance_rel=float(calibration_cfg.get("convergence_tolerance_rel", 0.1)))
        calib = ps.calibrate(base, calibration_cfg.get("observed") or {}, tolerance_rel=calibration_cfg.get("tolerance_rel") or {})
        results = []
        for s in scenarios[: 4]:
            rules = target_rules if s["policy"] == "target" else baseline_rules
            results.append(ps.simulate(rules, acc["gaps"], s, seed=DEFAULT_SEED))
        comparison = ps.compare(base, results)
        return {"executor_id": EXECUTOR_ID, "run_id": p["run_id"], "child_id": p["child_id"], "role": p["role"], "round": p["round"], "kind": "simulation",
                "baseline": base, "scenarios": results, "comparison": comparison, "approver_roles": roles, "seed": DEFAULT_SEED, "scenarios_proposed": len(scenarios),
                "convergence": conv, "calibration": calib,
                "findings": [], "rejected": [], "disputes": [], "rejected_disputes": [], "notes": [], "status": "OK", "applies_nothing": True}

    @staticmethod
    def on_terminal_without_execution(store, tenant_id, workspace_id, job_id, reason) -> None:
        from infra.worker.ma_specialist import _child_ended_unrun
        _child_ended_unrun(store, tenant_id, workspace_id, job_id, reason)

    @staticmethod
    def _enqueue_join(store, tenant, ws, run_id, child_id) -> None:
        from infra.worker.verification import ensure_job
        try:
            ensure_job(store, tenant, ws, ma.join_job_id(run_id, child_id), v2_ma.KIND_JOIN, {"run_id": run_id, "after": child_id})
        except Exception:
            pass
