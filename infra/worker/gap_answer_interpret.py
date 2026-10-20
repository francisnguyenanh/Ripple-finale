"""Job kind "gap.answer_interpret", executor_id "gap.answer_interpret.v1", role "gap.answer_interpret": a person's free-text answer to one gap -> a structured reading (one call).

The model proposes; the server keeps the reading only under core/change_intelligence/v2/business_answer_interpret.py (quote in the text, fits the gap kind, values from the
person's words or the shown rules' fields, Final grammar, nothing uncovered). The result carries the `answer` object of the existing preview route and the preview itself
(computed read-only at run time) so the person confirms through `POST /v2/business/gaps/answer` exactly as with the structured controls. Nothing is recorded by this job.
The asker's rights are rechecked when it runs; a moved snapshot fails it visibly. Role `gap.answer_interpret` must be pinned in the budget config.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_answer_interpret as bai
from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.business_change import _norm
from infra.adapters import v2_business
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "gap.answer_interpret.v1"
ROLE = "gap.answer_interpret"
KIND = "gap.answer_interpret"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "gap_answer_interpret.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def job_id_for(snapshot_id: str, gap_id: str, text: str) -> str:
    from infra.worker import job_binding
    return "gai-" + hashlib.sha256(json.dumps([snapshot_id, gap_id, _norm(text), job_binding.fingerprint(KIND), bai.COMPLETENESS_VERSION]).encode("utf-8")).hexdigest()[:24]   # a changed prompt/model or completeness rule is a new reading


def validate_payload(payload) -> str | None:
    frozen, _ = load_config()
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) or not payload["text"].strip() or len(payload["text"]) > frozen["limits"]["max_text_chars"]:
        return "text_invalid"
    if not isinstance(payload.get("gap_id"), str) or not payload["gap_id"] or not isinstance(payload.get("snapshot_id"), str) or not payload["snapshot_id"]:
        return "gap_and_snapshot_required"
    return None


class GapAnswerInterpretExecutor:
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
        try:
            head, base = v2_business._fresh_base(ctx.store, tenant, ws)
        except v2_business.BusinessKnowledgeRefused as exc:
            raise NonRetryableExecutionError("snapshot_moved", str(exc)) from exc
        if base["snapshot"]["snapshot_id"] != payload["snapshot_id"]:
            raise NonRetryableExecutionError("snapshot_moved", "the business knowledge changed after the answer was written")
        gap = next((g for g in base["gaps"] if g["gap_id"] == payload["gap_id"]), None)
        if gap is None or gap["status"] not in ("OPEN", "REOPENED"):
            raise NonRetryableExecutionError("gap_not_open", "this question is not open in the current knowledge version")
        rules = {r["rule_id"]: r for r in base["rules"] if r["status"] not in ("SUPERSEDED", "REJECTED")}
        shown, fields, coverage = bai.select_context(gap, rules, payload["text"], frozen["limits"])    # relevance-ranked, every cut reported (V4-IF-04)
        prompt = bai.build_prompt(frozen, gap, shown, payload["text"], max_chars=frozen["limits"]["max_prompt_chars"], fields=fields, coverage=coverage)
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the answer and the rules exceed the input ceiling")
        ctx.request_context.update(ai_frozen=frozen, ai_sha=sha, ai_gap=gap, ai_rules={r["rule_id"]: r for r in shown}, ai_fields=sorted(fields), ai_coverage=coverage,
                                   ai_base_hash=base["snapshot"]["content_hash"])
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, bai.MODEL_KEYS, bai.OPTIONAL_MODEL_KEYS)
        try:
            model = bai.validate_model_object(obj, rc["ai_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        payload = ctx.job["payload"]
        out = bai.resolve(model, payload["text"], rc["ai_gap"], rc["ai_rules"], rc["ai_frozen"]["limits"], known_fields=set(rc["ai_fields"]))
        coverage = rc["ai_coverage"]
        if out["status"] != "READY" and (coverage["rules_truncated"] or coverage["fields_truncated"]):
            out = dict(out, reasons=list(out["reasons"]) + ["context_truncated"])        # the model saw part of the knowledge: said, never hidden
        out = bai.completeness(out, payload["text"])                       # server completeness validation, separate from the model's reading (DEC-0006)
        preview = None
        if out["answer"] is not None:
            try:                                                     # the same preview the structured controls would show; read-only
                preview = v2_business.preview_answer(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], gap_id=payload["gap_id"], answer=out["answer"])
            except (bk.ContractError, v2_business.BusinessKnowledgeRefused) as exc:
                out = dict(out, status="NEEDS_CLARIFICATION", answer=None, quote=None, reasons=out["reasons"] + [f"preview_refused:{exc.code}"],
                           message="The reading could not be previewed against the current knowledge; please answer with the controls.")
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["ai_sha"], "gap_id": payload["gap_id"], "snapshot_id": payload["snapshot_id"], "text": payload["text"],
                  "status": out["status"], "proposed_answer": out["answer"], "quote": out["quote"], "uncovered": out["uncovered"], "reasons": out["reasons"], "message": out["message"],
                  "ruled_out": out.get("ruled_out", []), "rejected_reading": out.get("rejected_reading"), "completeness": out.get("completeness"), "model_uncertainty": out.get("model_uncertainty"), "context": coverage,
                  "preview": preview, "model_status": model["status"], "provider_calls": 1, "applies_nothing": True, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
