"""Job kind "knowledge.mining" (extension AIX-K1): one rule group of one knowledge version compared by the pinned role `knowledge.mining` (1 provider call).

The route plans the groups deterministically over the snapshot and creates one job per group (id = hash(snapshot, group, attempt)); the worker re-plans from the same snapshot,
refuses a group that no longer exists (`group_moved`), rechecks the requester's authority, calls the model once and keeps only the hypotheses the server can support (see
`core/change_intelligence/v2/business_mining.py`). Nothing is applied to the knowledge: hypotheses are read on the mining view and dismissed or raised by a person.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_mining as bm
from infra.adapters import v2_business
from infra.worker import extraction
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "knowledge.mining.v1"
ROLE = "knowledge.mining"
KIND = "knowledge.mining"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_mining.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def asked_pairs(snap) -> list:
    return [g["affected_rule_ids"] for g in snap["gaps"] if len(g["affected_rule_ids"]) >= 2]


class KnowledgeMiningExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        p = ctx.job["payload"]
        for key in ("snapshot_id", "group_id", "attempt", "config_sha256"):
            if key not in p:
                raise NonRetryableExecutionError("payload_invalid", key)
        frozen, sha = load_config()
        if sha != p["config_sha256"]:
            raise NonRetryableExecutionError("config_moved", "the frozen mining config changed after the request")
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        record = ctx.store.job_record(tenant, ws, ctx.job["job_id"])
        asker = record.get("requested_by")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, asker)
        if not {"knowledge.confirm", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        _, snap = v2_business._latest_view(ctx.store, tenant, ws)
        if snap is None or snap["snapshot"]["snapshot_id"] != p["snapshot_id"]:
            raise NonRetryableExecutionError("snapshot_moved", "the business knowledge changed after the mining was requested")
        plan = bm.plan_groups(snap["rules"], frozen["limits"])
        group = next((g for g in plan["groups"] if g["group_id"] == p["group_id"]), None)
        if group is None:
            raise NonRetryableExecutionError("group_moved", "this rule group is no longer planned for the knowledge version")
        rules = {r["rule_id"]: r for r in bm.live_rules(snap["rules"]) if r["rule_id"] in group["rule_ids"]}
        asked = [f"- {g['kind']} about {', '.join(g['affected_rule_ids'])} [{g['status']}]: {g['question']}" for g in snap["gaps"] if set(g["affected_rule_ids"]) & set(group["rule_ids"])]
        try:
            prompt = bm.build_prompt(frozen, [rules[r] for r in group["rule_ids"]], asked, bm.allowed_fields(rules.values()))
        except ValueError as exc:
            raise NonRetryableExecutionError("context_too_large", str(exc)) from exc
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the rule group exceeds the input ceiling")
        meta = snap["snapshot"]
        ctx.request_context.update(km_frozen=frozen, km_sha=sha, km_rules=rules, km_group=group, km_asked=asked_pairs(snap),
                                   km_pin={"snapshot_id": meta["snapshot_id"], "business_revision": meta["business_revision"], "content_hash": meta["content_hash"]})
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc, p = ctx.request_context, ctx.job["payload"]
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, bm.MODEL_KEYS)
        try:
            model = bm.validate_model_object(obj, rc["km_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = bm.resolve(model, rc["km_rules"], rc["km_asked"], snapshot_id=p["snapshot_id"], group_id=p["group_id"])
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["km_sha"], "pin": rc["km_pin"], "group_id": p["group_id"], "rule_ids": rc["km_group"]["rule_ids"], "attempt": p["attempt"],
                  "model_status": model["status"], "hypotheses": out["hypotheses"], "rejected": out["rejected"], "notes": out["notes"], "provider_calls": 1, "applies_nothing": True,
                  "origin": bm.ORIGIN, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
