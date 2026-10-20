"""Job kind "roi.analyst" (SUR-07), executor_id "roi.analyst.v1", role "roi.analyst": an analyst memo over the MON-ROI replay of a dataset labelled SYNTHETIC (one model call).

The replay is recomputed in the job by the deterministic engine (`roi_replay.replay`) from the SYNTHETIC records the request carried and the two ACCEPTED business revisions it
named; the model reads only the metric table and the declared assumptions. Each kept sensitivity scenario is computed again by the same engine with the one assumption
changed: the model never produces a number. Real data is refused at the request (no data owner grant); nothing is written except the job result (no records are echoed).
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import roi_analyst as ra
from core.change_intelligence.v2 import roi_replay
from infra.adapters import v2_business
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "roi.analyst.v1"
KIND = ROLE = "roi.analyst"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "roi_analyst.json"
SYNTHETIC_PREFIX = "SYNTHETIC"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def is_synthetic(payload) -> bool:
    return payload.get("synthetic") is True and str(payload.get("label") or "").upper().startswith(SYNTHETIC_PREFIX)


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list) or not payload["records"] or not isinstance(payload.get("assumptions"), dict):
        return "payload_invalid"
    if not is_synthetic(payload):
        return "real_data_not_granted"
    if not all(isinstance(payload.get(k), int) and not isinstance(payload.get(k), bool) for k in ("baseline_revision", "target_revision", "attempt")):
        return "payload_invalid"
    return None


def replay_for(store, tenant, ws, payload, assumptions) -> dict:
    with v2_business.read_snapshot(store) as cur:
        base, target = (v2_business._accepted_by_revision(cur, tenant, ws, n) for n in (payload["baseline_revision"], payload["target_revision"]))
    if base is None or target is None:
        raise NonRetryableExecutionError("revision_not_accepted", "a named business revision is not accepted knowledge of this workspace")
    policies = [{"label": f"business revision {n}", "rules": s["rules"], "gaps": s["gaps"]} for n, s in ((payload["baseline_revision"], base), (payload["target_revision"], target))]
    try:
        return roi_replay.replay(payload["records"], policies[0], policies[1], assumptions=assumptions, window_from=payload.get("window_from"),
                                 window_to=payload.get("window_to"), dataset_label=str(payload["label"]))
    except ValueError as exc:
        raise NonRetryableExecutionError("replay_refused", str(exc)) from exc


def scenario_metrics(report: dict) -> dict:
    keep = ("effort.baseline_hours", "effort.target_hours", "effort.delta_hours", "cost.delta_cost_range[0]", "cost.delta_cost_range[1]", "changes.approvals_delta")
    table = ra.metric_table(report)
    return {k: table[k] for k in keep if k in table}


class RoiAnalystExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        payload = ctx.job["payload"]
        err = validate_payload(payload)
        if err:
            raise NonRetryableExecutionError(err)
        frozen, sha = load_config()
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        asker = ctx.store.job_record(tenant, ws, ctx.job["job_id"]).get("requested_by")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, asker)
        if not {"project.read", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        report = replay_for(ctx.store, tenant, ws, payload, payload["assumptions"])
        table = ra.metric_table(report)
        declared = sorted(k for k in ra.ASSUMPTION_KEYS if payload["assumptions"].get(k) is not None) + ["minutes_per_approval_range"]
        assumptions = "\n".join(f"- {k}: {report['assumptions'].get(k)}" for k in ("minutes_per_approval", "minutes_per_approval_range", "hourly_cost"))
        prompt = frozen["prompt_template"].replace("{metrics}", "\n".join(f"- {k} = {v}" for k, v in sorted(table.items()))).replace("{assumptions}", assumptions)
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the metric pack exceeds the input ceiling")
        ctx.request_context.update(ra_frozen=frozen, ra_sha=sha, ra_report=report, ra_table=table, ra_declared=declared)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        payload = ctx.job["payload"]
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, ra.MODEL_KEYS)
        try:
            model = ra.validate_model_object(obj, rc["ra_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = ra.check(model, rc["ra_table"], rc["ra_declared"], rc["ra_frozen"]["limits"])
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        for sc in out["scenarios"]:                                                               # the engine computes every scenario; the model's value is only the input
            changed = dict(payload["assumptions"], **{sc["assumption"]: sc["value"]})
            if sc["assumption"] == "minutes_per_approval":
                changed.pop("minutes_per_approval_range", None)                                  # a new point value replaces the declared range for this scenario
            rep = replay_for(ctx.store, tenant, ws, payload, changed)
            sc["engine"] = {"report_hash": rep["report_hash"], "metrics": scenario_metrics(rep), "computed_by": "roi_replay engine"}
        report = rc["ra_report"]
        result = {"schema": ra.SCHEMA, "executor_id": EXECUTOR_ID, "config_sha256": rc["ra_sha"], "synthetic": True, "dataset": {k: report["dataset"].get(k) for k in ("label", "hash")},
                  "baseline_revision": payload["baseline_revision"], "target_revision": payload["target_revision"], "report_hash": report["report_hash"],
                  "metrics": rc["ra_table"], "model_status": model["status"], "statements": out["kept"], "assumption_questions": out["questions"],
                  "scenarios": out["scenarios"], "rejected": out["rejected"], "label": ra.LABEL, "provider_calls": 1, "applies_nothing": True,
                  "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
