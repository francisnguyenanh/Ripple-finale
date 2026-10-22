"""Job kind "knowledge.gap_proposals", executor_id "knowledge.gap_proposals.v1", role "knowledge.gap_proposals": gaps and relations a model suggests over one snapshot (one call).

The model only suggests. The server keeps a suggestion only under the rules of core/change_intelligence/v2/business_proposals.py (kind a person can act on, rules shown,
evidence references of the named rules readable by the asker, plain text, not already asked). Kept items live on this job's result and are shown in the gap queue and the
graph labelled as suggested by a model; they enter the knowledge only when a person answers through the existing answer path. The asker's rights are rechecked when the
job runs; a moved snapshot fails the job visibly. Role `knowledge.gap_proposals` must be pinned in the budget config.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_proposals as bp
from infra.adapters import v2_business
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "knowledge.gap_proposals.v1"
ROLE = "knowledge.gap_proposals"
KIND = "knowledge.gap_proposals"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_gap_proposals.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


MODEL_OUTPUT_LIMIT = 60000                       # characters of the model's own proposal object kept with the result (JOURNEY-V7-J2: a refused suggestion left no text to read)


def job_id_for(snapshot_id: str, attempt: int) -> str:
    from infra.worker import job_binding
    return "gpr-" + hashlib.sha256(json.dumps([snapshot_id, attempt, job_binding.fingerprint(KIND)]).encode("utf-8")).hexdigest()[:24]


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot_id"), str) or not payload["snapshot_id"]:
        return "snapshot_required"
    if not isinstance(payload.get("evidence_access"), bool) or not isinstance(payload.get("attempt"), int) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    return None


class GapProposalExecutor:
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
        if not {"knowledge.confirm", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        can_evidence = bool(payload["evidence_access"]) and "evidence.read" in perms
        view, snap = v2_business._latest_view(ctx.store, tenant, ws)
        if snap is None or snap["snapshot"]["snapshot_id"] != payload["snapshot_id"]:
            raise NonRetryableExecutionError("snapshot_moved", "the business knowledge changed after the suggestions were requested")
        meta, head = snap["snapshot"], view["workspace"]
        if (meta["source_revision_id"], meta["km_revision_id"], meta["decision_epoch"]) != (head["source_revision_id"], head["knowledge_revision_id"], head["decision_epoch"]):
            raise NonRetryableExecutionError("snapshot_moved", "the source or decision head moved after this knowledge version")
        live = sorted((r for r in snap["rules"] if r["status"] not in ("SUPERSEDED", "REJECTED")), key=lambda r: (r["authority"] != "TARGET", r["rule_id"]))
        limit = frozen["limits"]["max_known_rules"]
        shown = live[:limit]
        rules = {r["rule_id"]: r for r in shown}
        asked = [f"- {g['kind']} about {', '.join(g['affected_rule_ids'])}: {g['question']}" for g in snap["gaps"]]
        edges = [f"- {e['from']} {e['type']} {e['to']}" for e in snap["relations"] if e["status"] != "REJECTED"]
        prompt, coverage = bp.build_prompt_with_coverage(frozen, shown, asked, edges, can_read_evidence=can_evidence, max_chars=frozen["limits"]["max_prompt_chars"])
        from infra.worker import extraction
        bound = extraction._input_token_bound(prompt, frozen["output_schema"])
        # three numbers kept apart in the record: characters of the prompt, the byte-based admission bound (what the ceiling is checked against), and the
        # per-call reservation, which is the configured caps priced at the ceiling, never the prompt's size (job_quota)
        coverage = {**coverage, "token_bound": bound, "token_ceiling": config.max_input_tokens_per_call}
        if bound > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the rules exceed the input ceiling")
        pin = {"snapshot_id": meta["snapshot_id"], "business_revision": meta["business_revision"], "source_revision_id": meta["source_revision_id"],
               "km_revision_id": meta["km_revision_id"], "decision_epoch": meta["decision_epoch"], "content_hash": meta["content_hash"]}
        ctx.request_context.update(gp_frozen=frozen, gp_sha=sha, gp_rules=rules, gp_gaps=snap["gaps"], gp_relations=snap["relations"], gp_pin=pin, gp_coverage=coverage,
                                   gp_evidence=can_evidence, gp_not_shown=max(0, len(live) - len(shown)))
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, bp.PROPOSAL_KEYS)
        try:
            model = bp.validate_proposal_object(obj, rc["gp_frozen"]["limits"])
            model_output = json.dumps(obj, ensure_ascii=False, default=str)                  # what the model said, before validation: kept (bounded) for diagnosis
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = bp.resolve_proposals(model, rc["gp_rules"], rc["gp_gaps"], rc["gp_relations"], limits=rc["gp_frozen"]["limits"], can_read_evidence=rc["gp_evidence"])
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["gp_sha"], "pin": rc["gp_pin"], "model_status": model["status"], "gaps": out["gaps"],
                  "relations": out["relations"], "rejected": out["rejected"], "notes": out["notes"], "questions_trimmed": out.get("questions_trimmed") or [], "rules_shown": len(rc["gp_rules"]), "rules_not_shown": rc["gp_not_shown"],
                  "evidence_access": rc["gp_evidence"], "provider_calls": 1, "applies_nothing": True, "origin": bp.ORIGIN, "model": {"pinned": model_id, "served": actual},
                  "prompt_coverage": rc["gp_coverage"],
                  "model_output": model_output[:MODEL_OUTPUT_LIMIT], "model_output_truncated": len(model_output) > MODEL_OUTPUT_LIMIT}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
