"""Job kind "document.generate" (DOC-02): render one requested Business or Technical document of an accepted business snapshot. Deterministic, zero provider calls,
no model: the AST comes from stored rows, the Markdown from the versioned template, the bytes go to the immutable blob store and one metadata row. A request the
contract refuses (unknown snapshot, over the rule/size ceiling, bad template) fails the job and writes nothing. The job id is the artifact id (cache key)."""
from __future__ import annotations

from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters import v2_business_documents as docs
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget

EXECUTOR_ID = "document.generate.v1"
KIND = docs.KIND


class DocumentGenerateExecutor:
    external = False
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        for key in ("artifact_id", "snapshot_id", "mode", "scope", "locale", "template_version", "redaction_scope"):
            if key not in payload:
                raise NonRetryableExecutionError("payload_invalid", f"missing {key}")
        if payload["artifact_id"] != ctx.job["job_id"]:
            raise NonRetryableExecutionError("payload_invalid", "the job id must be the artifact id")
        return ExecutionRequest(prompt=None, schema={"type": "object", "executor": EXECUTOR_ID}, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        try:
            out = docs.generate(ctx.store, job["tenant_id"], job["workspace_id"], job["payload"], job_id=job["job_id"])
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("snapshot_unavailable", str(exc)) from exc
        except bk.ContractError as exc:
            raise NonRetryableExecutionError(exc.code, str(exc)) from exc
        except BusinessKnowledgeRefused as exc:
            raise NonRetryableExecutionError(exc.code, str(exc)) from exc
        result = {"executor_id": EXECUTOR_ID, "provider_calls": 0, "applies_nothing": True, "artifact_id": out["artifact_id"], "snapshot_id": out["snapshot_id"], "mode": out["mode"],
                  "content_hash": out["content_hash"], "rule_count": out["rule_count"], "size_bytes": out["size_bytes"], "replayed": out["replayed"]}
        return ExecutionOutcome(result=result, model_id=None)
