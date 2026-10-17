"""Job kind "document.variant" (extension AIX-D1): one audience variant of one generated document through the pinned role `document.wording` (1 provider call).

prepare: the base document row and its knowledge version must still exist, the requester must still hold the base permission (+ `evidence.read` for an evidence-scoped base) and
`ai.use`; the fact pack is built from the same AST as the base (same mode, scope, redaction) and the pinned prompt. execute: the model object is validated for shape, then every
paragraph is checked against the pack; a missing mandatory section or UNSUPPORTED makes a labelled deterministic fallback (the base bytes). The bytes go to the blob store and
one immutable row. Nothing is applied to the knowledge.
"""
from __future__ import annotations

import datetime

from core.change_intelligence.v2 import business_knowledge as bk, document_ast as da, document_variant as dv
from infra.adapters import v2_business, v2_business_documents as docs, v2_document_variants as variants
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import extraction
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "document.variant.v1"
KIND = variants.KIND


class DocumentVariantExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        p = ctx.job["payload"]
        for key in ("variant_id", "artifact_id", "audience", "locale", "snapshot_id", "config_sha256", "model_pinned", "requested_by"):
            if key not in p:
                raise NonRetryableExecutionError("payload_invalid", f"missing {key}")
        if p["variant_id"] != ctx.job["job_id"]:
            raise NonRetryableExecutionError("payload_invalid", "the job id must be the variant id")
        if p["audience"] not in dv.AUDIENCES:
            raise NonRetryableExecutionError("payload_invalid", "audience")
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        frozen, sha = variants.load_config()
        if sha != p["config_sha256"]:
            raise NonRetryableExecutionError("config_moved", "the frozen wording config changed after the request; request the variant again")
        base = docs._row(ctx.store, tenant, ws, p["artifact_id"])
        if base is None or base["snapshot_id"] != p["snapshot_id"]:
            raise NonRetryableExecutionError("base_unavailable", "the base document is not available")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, p["requested_by"])
        needed = {docs.required_permission(base["mode"]), "ai.use"} | ({"evidence.read"} if base["redaction_scope"] == "evidence" else set())
        if not needed <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, p["requested_by"]):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        try:
            snap = docs.snapshot_for(ctx.store, tenant, ws, base["snapshot_id"])
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("snapshot_unavailable", str(exc)) from exc
        try:
            doc = da.build_document(snap, snap["decisions"], mode=base["mode"], scope=base["scope"], can_read_evidence=base["redaction_scope"] == "evidence", locale=base["locale"],
                                    template_version=base["template_version"])
        except bk.ContractError as exc:
            raise NonRetryableExecutionError(exc.code, str(exc)) from exc
        pack = dv.fact_pack(doc, max_facts=frozen["limits"]["max_facts"])
        try:
            prompt = dv.build_prompt(frozen, p["audience"], pack)
        except ValueError as exc:
            raise NonRetryableExecutionError("context_too_large", str(exc)) from exc
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the facts exceed the input ceiling")
        ctx.request_context.update(dv_frozen=frozen, dv_sha=sha, dv_doc=doc, dv_pack=pack, dv_base=base)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc, p = ctx.request_context, ctx.job["payload"]
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, variants.ROLE, dv.MODEL_KEYS)
        frozen = rc["dv_frozen"]
        try:
            model = dv.validate_model_object(obj, frozen["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        cfg = frozen["audiences"][p["audience"]]
        checked = dv.check(model, rc["dv_pack"], cfg)
        from core.change_intelligence.v2.serialize import canonical_hash
        checked["fact_coverage"]["facts_hash"] = canonical_hash(da.facts_view(rc["dv_doc"]))          # every audience variant of one base is checked against the SAME facts (consistency across audiences)
        base_text = None
        if checked["fallback"]:
            content, _ = docs.read_content(ctx.store, tenant, ws, p["artifact_id"])
            base_text = content.decode("utf-8")
        label = {"audience": p["audience"], "artifact_id": p["artifact_id"], "snapshot_id": p["snapshot_id"], "role": variants.ROLE, "model_pinned": model_id, "config_sha256": rc["dv_sha"],
                 "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")}
        markdown = dv.render(rc["dv_doc"], p["audience"], cfg, checked, base_markdown=base_text, label=label)
        try:
            row = variants.generate(ctx.store, tenant, ws, p, job_id=ctx.job["job_id"], checked=checked, markdown=markdown, model_served=actual)
        except BusinessKnowledgeRefused as exc:
            raise NonRetryableExecutionError(exc.code, str(exc)) from exc
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["dv_sha"], "variant_id": p["variant_id"], "artifact_id": p["artifact_id"], "audience": p["audience"], "status": row["status"],
                  "content_hash": row["content_hash"], "fact_coverage": checked["fact_coverage"], "rejected": checked["rejected"], "missing_mandatory": checked["missing_mandatory"],
                  "omitted": checked["omitted"], "model_status": model["status"], "replayed": row["replayed"], "model": {"pinned": model_id, "served": actual}, "provider_calls": 1, "applies_nothing": True}
        return ExecutionOutcome(result=result, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
