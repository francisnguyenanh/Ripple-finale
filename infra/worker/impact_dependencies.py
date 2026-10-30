"""Job kind "change.impact_deps", executor_id "change.impact_deps.v1", role "change.impact_deps": dependencies of a CONFIRMED change that the stored graph does not show (one model call).

The model only proposes. The server keeps a proposal only if both rules were shown, one of them is a changed rule, the relation type is allowed, the rationale is plain text and every
evidence reference belongs to one of the two rules in the pinned snapshot and may be read by the person who asked (core/change_intelligence/v2/business_impact.py). A kept proposal
is stored as PROPOSED with provenance ai_inference: it never becomes a confirmed relation and never widens the write scope. A failed, refused or unfinished job leaves the impact
report's coverage incomplete; it is never read as "no dependency". The asker's rights are rechecked when the job runs. Role `change.impact_deps` must be pinned in the budget config.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_impact as bim
from infra.adapters import v2_business, v2_business_change
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "change.impact_deps.v1"
ROLE = "change.impact_deps"
KIND = "change.impact_deps"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "change_impact_deps.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("intent_id"), str) or not payload["intent_id"].startswith("chg-"):
        return "intent_required"
    if not isinstance(payload.get("evidence_access"), bool) or not isinstance(payload.get("attempt"), int) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    return None


class ImpactDependencyExecutor:
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
        if not {"project.read", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        can_evidence = bool(payload["evidence_access"]) and "evidence.read" in perms        # rights are the lower of what was asked with and what is held now
        got = v2_business_change.pinned_for_intent(ctx.store, tenant, ws, payload["intent_id"])
        if got is None:
            raise NonRetryableExecutionError("intent_not_confirmed", "the change request is not confirmed")
        intent, snap, artifacts, pin = got
        rules = {r["rule_id"]: r for r in snap["rules"] if r["status"] not in ("SUPERSEDED", "REJECTED")}
        write_ids = [rid for rid in intent["write_scope"] if rid in rules]
        report = bim.build_report(intent["operations"], {r["rule_id"]: r for r in snap["rules"]}, snap["relations"], pin=pin, known_artifacts=artifacts)
        found_ids = [i["rule_id"] for i in report["affected_rules"]]
        order = list(dict.fromkeys(write_ids + [i for i in found_ids if i in rules] + sorted(rules)))
        limit = frozen["limits"]["max_known_rules"]
        shown = [rules[i] for i in order[:limit]]
        changes = [f"- changed rule id={o['rule_id']} ({o['kind']}): {report_text}" for o in intent["operations"]
                   for report_text in [bim._action(o, rules[o["rule_id"]]["title"]) if o["rule_id"] in rules else o["kind"]]]
        found = [f"- id={i['rule_id']} ({i['link']})" for i in report["affected_rules"]]
        same = bim.same_measure(write_ids, {r["rule_id"]: r for r in shown}, found_ids)
        prompt = bim.build_dep_prompt(frozen, changes, shown, found, can_read_evidence=can_evidence, max_chars=frozen["limits"]["max_prompt_chars"], same=same)
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the change and the rules exceed the input ceiling")
        ctx.request_context.update(di_frozen=frozen, di_sha=sha, di_rules={r["rule_id"]: r for r in shown}, di_write=write_ids, di_pin=pin, di_evidence=can_evidence,
                                   di_not_shown=max(0, len(rules) - len(shown)), di_report_hash=report["report_hash"])
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, bim.DEP_KEYS)
        try:
            model = bim.validate_dep_object(obj, rc["di_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = bim.resolve_dependencies(model, rc["di_rules"], rc["di_write"], limits=rc["di_frozen"]["limits"], can_read_evidence=rc["di_evidence"])
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["di_sha"], "intent_id": ctx.job["payload"]["intent_id"], "pin": rc["di_pin"], "report_hash": rc["di_report_hash"],
                  "model_status": model["status"], "accepted": out["accepted"], "rejected": out["rejected"], "notes": out["notes"], "rules_not_shown": rc["di_not_shown"],
                  "evidence_access": rc["di_evidence"], "provider_calls": 1, "applies_nothing": True, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
