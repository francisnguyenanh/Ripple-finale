"""Job kind "gap.interview_question" (extension AIX-K2 model questioner; role `gap.interview` pinned in the budget config): one provider call that proposes the next question
of an interview session. The server grounds it (plain text, names the rule, an uncovered part, a contradicted field or the matter of the deterministic question) or falls back to
the deterministic question; the interview adapter reads the result on its next refresh. Nothing here writes knowledge or the session."""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import gap_interview as gi
from infra.adapters import v2_business
from infra.worker import extraction
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "gap.interview_question.v1"
ROLE = "gap.interview"
KIND = "gap.interview_question"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "gap_interview.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def job_id_for(session_id: str, turn: int) -> str:
    return "giq-" + hashlib.sha256(json.dumps([session_id, int(turn)]).encode("utf-8")).hexdigest()[:24]


class GapInterviewQuestionExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        p = ctx.job["payload"]
        for key in ("session_id", "turn", "gap_id", "snapshot_id", "deterministic", "transcript", "requested_by"):
            if key not in p:
                raise NonRetryableExecutionError("payload_invalid", key)
        frozen, sha = load_config()
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        perms = v2_business.current_permissions(ctx.store, tenant, ws, p["requested_by"])
        if not {"knowledge.confirm", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, p["requested_by"]):
            raise NonRetryableExecutionError("authority_revoked", "the person who started the interview no longer holds the permission")
        _, snap = v2_business._latest_view(ctx.store, tenant, ws)
        if snap is None or snap["snapshot"]["snapshot_id"] != p["snapshot_id"]:
            raise NonRetryableExecutionError("snapshot_moved", "the business knowledge changed during the interview")
        gap = next((g for g in v2_business.gap_queue(ctx.store, tenant, ws)["gaps"] if g["gap_id"] == p["gap_id"]), None)
        if gap is None:
            raise NonRetryableExecutionError("gap_not_open", "the question is no longer open")
        rules = {r["rule_id"]: r for r in snap["rules"]}
        shown = [rules[r] for r in gap.get("affected_rule_ids", []) if r in rules][: frozen["limits"]["max_rules"]]
        prompt = gi.build_question_prompt(frozen, gap, shown, p["transcript"], p["deterministic"])
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the interview transcript exceeds the input ceiling")
        ctx.request_context.update(gi_frozen=frozen, gi_sha=sha, gi_gap=gap, gi_rules=shown)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc, p = ctx.request_context, ctx.job["payload"]
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, gi.MODEL_KEYS)
        try:
            model = gi.validate_question_object(obj, rc["gi_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = gi.ground_question(model, rc["gi_gap"], rc["gi_rules"], p["transcript"], p["deterministic"])
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["gi_sha"], "session_id": p["session_id"], "turn": p["turn"], **out, "model_status": model["status"],
                  "model": {"pinned": model_id, "served": actual}, "provider_calls": 1, "applies_nothing": True}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
