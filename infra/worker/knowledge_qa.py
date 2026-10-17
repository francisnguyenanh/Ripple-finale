"""Job kind "knowledge.qa", executor_id "knowledge.qa.v1", role "knowledge.qa": a business question answered from a snapshot-pinned context pack (one model call).

The pack is built HERE, on the server, from the stored snapshot the question was asked against; the model sees business item text only (source text only if the asker could
read it when the job runs), phrases an answer and cites ids. The server validates every claim (core/change_intelligence/v2/business_qa.py) and stores the validated answer
with its pin; reading it never calls a model. Authority is rechecked when the job runs: a revoked asker gets no answer. If the knowledge moved since the question was
asked the job fails visibly with `snapshot_moved` instead of answering about another head. Role `knowledge.qa` must be pinned in the budget config (no fallback), the
endpoint must be the Vertex global one and the served model must equal the pinned one. Nothing here edits knowledge, files or code.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_qa as qa
from infra.adapters import v2_business, v2_business_index
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import extraction
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "knowledge.qa.v1"
ROLE = "knowledge.qa"
KIND = "knowledge.qa"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_qa.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict):
        return "payload_required"
    frozen, _ = load_config()
    q = payload.get("question")
    if not isinstance(q, str) or not q.strip() or len(q) > frozen["limits"]["max_question_chars"]:
        return "question_invalid"
    if payload.get("view") not in ("target", "observed") or not isinstance(payload.get("snapshot_id"), str) or not isinstance(payload.get("evidence_access"), bool):
        return "view_snapshot_or_access_invalid"
    return None


class KnowledgeQaExecutor:
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
        perms = v2_business.current_permissions(ctx.store, tenant, ws, record.get("requested_by"))
        if not {"knowledge.answer", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, record.get("requested_by")):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        can_evidence = payload["evidence_access"] and "evidence.read" in perms
        previous = None
        if payload.get("follow_up_of"):
            try:
                prev = ctx.store.job_record(tenant, ws, payload["follow_up_of"])
            except TenantScopeError as exc:
                raise NonRetryableExecutionError("follow_up_not_valid", "the earlier question does not exist in this workspace") from exc
            pres = prev.get("execution_result") or {}
            if prev.get("kind") != KIND or prev.get("execution_status") != "COMPLETED" or (pres.get("pack") or {}).get("pin", {}).get("snapshot_id") != payload["snapshot_id"]:
                raise NonRetryableExecutionError("follow_up_not_valid", "the earlier answer is missing or belongs to another snapshot")
            previous = {"question": pres["question"], "answer": pres["answer"]}
        try:
            pack = v2_business_index.context_pack(ctx.store, tenant, ws, payload["question"], view=payload["view"], can_read_evidence=can_evidence,
                                                  expected_snapshot_id=payload["snapshot_id"], semantic=False)          # deterministic here; the paid part runs in execute under the call budget
        except v2_business.BusinessKnowledgeRefused as exc:
            raise NonRetryableExecutionError("snapshot_moved" if exc.code == "stale_snapshot" else exc.code, str(exc)) from exc
        ctx.request_context.update(qa_pack=pack, qa_frozen=frozen, qa_sha=sha, qa_can_evidence=can_evidence, qa_previous=previous, qa_pre=None, qa_shown={"items_left_out": 0})
        state = retrieval_state(pack)
        if state["status"] == "no_evidence":
            # The search completed over the whole pinned snapshot and found nothing to answer from: the question is ABSTAINED without a model call and without a reservation
            # being used. A failed or half-built retrieval is NOT this case (see `retrieval_state`): it fails the job as retrieval_unavailable so nothing reads as "no evidence".
            ctx.request_context["qa_pre"] = state
            return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0, external=False)
        if state["status"] == "unavailable":
            raise NonRetryableExecutionError("retrieval_unavailable", state["reason"])
        prompt, shown = qa.build_prompt(frozen, payload["question"], pack, previous=previous, can_read_evidence=can_evidence)
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "question and context exceed the input ceiling")
        ctx.request_context["qa_shown"] = shown
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=planned_calls(), external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        payload = ctx.job["payload"]
        if rc.get("qa_pre") is not None:
            return self._abstained(ctx, rc["qa_pre"], model_id=None, actual=None)
        pack = rc["qa_pack"]
        sem_calls = sem_in = sem_out = 0
        if semantic_armed():
            # Semantic retrieval under THIS attempt's call budget: every provider call is consumed (and journaled) before it is made; the QA call keeps one slot.
            sub = _SubBudget(calls, max(0, request.provider_calls_planned - 1))
            tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
            try:
                pack = v2_business_index.context_pack(ctx.store, tenant, ws, payload["question"], view=payload["view"], can_read_evidence=rc["qa_can_evidence"],
                                                      expected_snapshot_id=payload["snapshot_id"], semantic=True, semantic_budget=sub, allow_external_semantic=True)
            except v2_business.BusinessKnowledgeRefused as exc:
                raise NonRetryableExecutionError("snapshot_moved" if exc.code == "stale_snapshot" else exc.code, str(exc)) from exc
            sem = pack.get("semantic") or {}
            sem_calls, sem_in, sem_out = int(sem.get("calls") or 0), int(sem.get("tokens_in") or 0), int(sem.get("tokens_out") or 0)
            rc["qa_pack"] = pack
            if pack.get("sufficiency") == "insufficient" and sem.get("status") == "applied":
                # the judge layer ran fully and found the evidence insufficient: no answer call is made; the limit of the search is stated, not hidden
                state = {"status": "no_evidence", "reason": "evidence_judged_insufficient", "detail": retrieval_state(pack)["detail"]}
                return self._abstained(ctx, state, model_id=None, actual=None, semantic=(sem_calls, sem_in, sem_out))
            prompt, shown = qa.build_prompt(rc["qa_frozen"], payload["question"], pack, previous=rc["qa_previous"], can_read_evidence=rc["qa_can_evidence"])
            if extraction._input_token_bound(prompt, rc["qa_frozen"]["output_schema"]) > ctx.config.max_input_tokens_per_call:
                raise NonRetryableExecutionError("budget_exceeded", "question and context exceed the input ceiling")
            rc["qa_shown"] = shown
            request = ExecutionRequest(prompt=prompt, schema=request.schema, provider_calls_planned=request.provider_calls_planned, external=True)
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, qa.MODEL_KEYS)
        try:
            model = qa.validate_model_object(obj, rc["qa_frozen"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        kept, dropped = qa.judge_claims(model["claims"], pack, payload["question"], rc["qa_frozen"], can_read_evidence=rc["qa_can_evidence"])
        final = qa.finalize(model, kept, dropped, pack, rc["qa_frozen"])
        final["retrieval_state"] = retrieval_state(pack)
        outcome = self._result(ctx, final, pack, model_id, actual, provider_calls=1 + sem_calls, semantic=(sem_calls, sem_in, sem_out))
        return ExecutionOutcome(result=outcome, model_id=actual, input_tokens=(in_t or 0) + sem_in, output_tokens=(out_t or 0) + sem_out, thought_tokens=th_t)

    def _abstained(self, ctx, state, *, model_id, actual, semantic=(0, 0, 0)) -> ExecutionOutcome:
        rc = ctx.request_context
        pack = rc["qa_pack"]
        model = {"status": "ABSTAINED", "answer": "", "claims": [], "uncertainty": "", "clarifying_questions": []}
        final = qa.finalize(model, [], [], pack, rc["qa_frozen"])
        final["reasons"] = [state["reason"]] + [r for r in final["reasons"] if r not in ("no_claim",)]
        final["retrieval_state"] = retrieval_state(pack) if state["reason"] != "evidence_judged_insufficient" else dict(retrieval_state(pack), status="no_evidence", reason=state["reason"])
        result = self._result(ctx, final, pack, model_id, actual, provider_calls=semantic[0], semantic=semantic)
        return ExecutionOutcome(result=result, model_id=actual, input_tokens=semantic[1] or None, output_tokens=semantic[2] or None, thought_tokens=None)

    def _result(self, ctx, final, pack, model_id, actual, *, provider_calls, semantic):
        rc = ctx.request_context
        payload = ctx.job["payload"]
        slim = {k: v for k, v in pack.items() if k not in ("observations",)}
        slim["items"] = [{k: v for k, v in i.items() if k not in ("text", "term_vector", "scope", "condition", "exceptions", "outcome")} for i in pack["items"]]
        slim["observation_ids"] = [o["observation_id"] for o in pack["observations"].get("results", [])] if rc["qa_can_evidence"] else []
        slim["counterevidence"] = [{k: v for k, v in c.items() if k not in ("text",)} for c in pack["counterevidence"]]
        result = {**final, "question": payload["question"], "view": payload["view"], "pack": slim, "pack_titles": {i["item_id"]: i.get("title") or i["text"][:80] for i in pack["items"]}
                  | {m["item_id"]: i.get("title") or i["text"][:80] for i in pack["items"] for m in i.get("versions", [])}
                  | {c["rule_id"]: c["title"] for c in pack["counterevidence"] if c["kind"] == "conflicting_rule"},
                  "evidence_access": rc["qa_can_evidence"], "executor_id": EXECUTOR_ID, "config_sha256": rc["qa_sha"], "provider_calls": provider_calls, "applies_nothing": True,
                  "semantic_calls": semantic[0], "semantic_tokens": {"input": semantic[1], "output": semantic[2]},
                  "prompt_items_left_out": rc["qa_shown"]["items_left_out"], "model": {"pinned": model_id, "served": actual},
                  "endpoint": {"backend": "vertex", "location": "global", "pinned_model": model_id, "served_model": actual} if model_id else None}
        return json.loads(_canon_json(result))


# ---- retrieval state and the budget boundary of semantic calls (contract section 12) -----------------------------------------------------------------------------

def retrieval_state(pack) -> dict:
    """What the search did, kept apart from what it found. `no_evidence` only when the search COMPLETED over the pinned snapshot (index READY or the snapshot read directly)
    and the observation search ran, and nothing came back; a failed or half-built retrieval is `unavailable` (never read as "no evidence"); otherwise `complete` or `partial`
    (semantic layers fell back, or the observation search did not run). The retrieval note stays: the lexical search can miss relevant rules."""
    index_status = (pack.get("index") or {}).get("status")
    served = pack.get("served_from")
    obs = pack.get("observations") or {}
    sem = pack.get("semantic") or {}
    detail = {"index": index_status, "served_from": served, "observations": obs.get("status"), "semantic": sem.get("status"), "items": len(pack.get("items") or []),
              "observation_results": len(obs.get("results") or [])}
    if served not in ("index", "snapshot_direct") and pack.get("pin", {}).get("snapshot_id"):
        return {"status": "unavailable", "reason": f"retrieval_unavailable: the snapshot could not be searched ({index_status})", "detail": detail}
    if obs.get("status") not in (None, "NOT_REQUESTED", "OK", "DONE", "READY", "COMPLETE", "COMPLETED") and not obs.get("results"):
        return {"status": "unavailable", "reason": f"retrieval_unavailable: the source search did not complete ({obs.get('status')})", "detail": detail}
    if not pack.get("items") and not obs.get("results"):
        return {"status": "no_evidence", "reason": "no_evidence_in_snapshot", "detail": detail}
    if sem.get("status") in ("partial", "fallback"):
        return {"status": "partial", "reason": "semantic_layers_fell_back", "detail": detail}
    return {"status": "complete", "reason": None, "detail": detail}


def semantic_armed() -> bool:
    """True when the configured semantic adapter would call an EXTERNAL provider (disarmed and replay providers cost nothing)."""
    from infra import provider_gate
    adapter = v2_business_index.semantic_adapter()
    return bool(getattr(adapter.provider, "external", False)) and not provider_gate.is_disarmed()


def planned_calls(config=None) -> int:
    """The provider calls one attempt may make: the answer call plus, when an external semantic provider is armed, as many semantic calls as the configured per-attempt
    ceiling leaves (`max_provider_calls_per_job - 1`, at most the adapter's call cap). Admission, the reservation and the per-attempt budget all use this number, so every
    semantic call is reserved before it is made and booked once, by the attempt; the ceiling is never raised to fit the adapter."""
    if not semantic_armed():
        return 1
    if config is None:
        from infra.worker import job_quota
        import os
        config = job_quota.load_worker_budget_config() if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH") else None
    ceiling = int(getattr(config, "max_provider_calls_per_job", 1) or 1) if config is not None else 1
    return 1 + max(0, min(int(v2_business_index.semantic_adapter().caps["max_calls"]), ceiling - 1))


class _SubBudget:
    """A slice of the attempt's call budget handed to the semantic adapter: at most `max_n` of the remaining calls, each consumed from the real budget before the call."""

    def __init__(self, calls: ProviderCallBudget, max_n: int):
        self.calls, self.max_n, self.used = calls, max_n, 0

    def consume(self, n: int = 1) -> None:
        if self.used + n > self.max_n:
            from infra.worker.execution_types import ProviderCallCeilingExceeded
            raise ProviderCallCeilingExceeded(self.max_n, self.used + n)
        self.calls.consume(n)
        self.used += n
