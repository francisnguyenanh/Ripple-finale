"""Job kind "change.draft_guidance", executor_id "change.draft_guidance.v1", role "change.draft_guidance": a model writes GUIDANCE over the server-built draft of a
CONFIRMED business change (one model call).

The draft itself (rule before/after, relation updates, file actions, outcomes, carried coverage) is built by the server from the pinned data
(infra/adapters/v2_business_change.py::draft_for_intent, core/change_intelligence/v2/business_draft.py) and is readable without this job. The model may only add a summary,
a manual instruction per server-numbered item, questions for a person, and a `hold` that downgrades an automatic edit to MANUAL. It cannot add, drop or upgrade an item,
name a value or a path, or resolve an unresolved item: unknown refs and non-plain text are rejected with a reason, unmentioned items keep the server's action.
The asker's rights (`change.prepare`, `ai.use`; `evidence.read` decides whether file paths and stored text are shown to the model) are rechecked when the job runs.
A failed, refused or malformed job leaves the draft's guidance incomplete and writes nothing but the job row. Role `change.draft_guidance` must be pinned in the budget config.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_draft as bd
from infra.adapters import v2_business, v2_business_change
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "change.draft_guidance.v1"
ROLE = "change.draft_guidance"
KIND = "change.draft_guidance"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "change_draft_guidance.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("intent_id"), str) or not payload["intent_id"].startswith("chg-"):
        return "intent_required"
    if not isinstance(payload.get("evidence_access"), bool) or not isinstance(payload.get("attempt"), int) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    return None


def result_hash(result: dict) -> str:
    body = {k: v for k, v in result.items() if k not in ("result_hash", "ai_outcome")}          # ai_outcome is added by the worker after the hash: counts, not content
    return hashlib.sha256(_canon_json(body).encode()).hexdigest()


class DraftGuidanceExecutor:
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
        record = ctx.store.job_record(tenant, ws, ctx.job["job_id"])
        asker = record.get("requested_by")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, asker)
        if not {"change.prepare", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        can_evidence = bool(payload["evidence_access"]) and "evidence.read" in perms          # the lower of what was asked with and what is held now
        probe = v2_business_change.impact_for_intent(ctx.store, tenant, ws, payload["intent_id"])
        if probe is None:
            raise NonRetryableExecutionError("intent_not_confirmed", "the change request is not confirmed")
        from infra import v2_business_routes as routes
        deps, dep_state = routes._dependency_input(ctx.store, tenant, ws, payload["intent_id"], probe)
        gate = routes.dependency_gate(probe, dep_state)
        if gate is not None:                                              # never run on its own: the dependency job is a paid, admitted job of its own
            raise NonRetryableExecutionError("dependencies_incomplete", gate["next_action"])
        got = v2_business_change.draft_for_intent(ctx.store, tenant, ws, payload["intent_id"], proposed_dependencies=deps, dependency_state=dep_state)
        if got is None:
            raise NonRetryableExecutionError("intent_not_confirmed", "the change request is not confirmed")
        draft = got["draft"]
        prompt = bd.build_guidance_prompt(frozen, draft, can_read_evidence=can_evidence, max_chars=frozen["limits"]["max_prompt_chars"])
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the draft exceeds the input ceiling")
        ctx.request_context.update(dg_frozen=frozen, dg_sha=sha, dg_draft=draft, dg_evidence=can_evidence)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, bd.GUIDE_KEYS)
        try:
            model = bd.validate_guidance_object(obj, rc["dg_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = bd.resolve_guidance(model, rc["dg_draft"], rc["dg_frozen"]["limits"])
        draft = rc["dg_draft"]
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["dg_sha"], "intent_id": ctx.job["payload"]["intent_id"], "pin": draft["pin"], "report_hash": draft["report_hash"],
                  "semantic_diff_hash": draft["semantic_diff_hash"], "summary": out["summary"], "accepted": out["accepted"], "rejected": out["rejected"], "questions": out["questions"], "advice": out["advice"], "question_items": out["question_items"],
                  "not_addressed": out["not_addressed"], "effect_statements": out["effect_statements"], "effects_coverage": out["effects_coverage"], "removed": out["removed"],
                  "evidence_access": rc["dg_evidence"], "provider_calls": 1, "applies_nothing": True, "model": {"pinned": model_id, "served": actual}}
        result = json.loads(_canon_json(result))
        result["result_hash"] = result_hash(result)
        return ExecutionOutcome(result=result, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
