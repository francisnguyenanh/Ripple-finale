"""Job kind "request.qa", executor "request.qa.v1", pinned role "request.assistant": one question of a private request-assistant thread -> a checked answer (RQA-03).

Prepare (immediately before the provider call) rechecks, from the server's own records: the thread's creator still holds `request.assistant.ask` (not suspended) and may
still read the request; the context pack rebuilt NOW under that person's rights has the same manifest hash as when the question was asked (otherwise `context_changed`:
the person asks again; nothing is answered from a context they could no longer see, or from a state that moved). The prompt carries only that pack, the person's last few
questions with the kept answer blocks of this thread, and the question. Execute makes one pinned call; `core/change_intelligence/v2/request_assistant.check_answer` keeps or
refuses each block. The result also carries the next steps the person may take, computed by the server from the task view (never by the model). Nothing is written to any
business record; the answer is published into the thread only when read back under the same authorization (`infra/adapters/v2_request_threads.py`).
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import request_assistant as ra
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "request.qa.v1"
ROLE = "request.assistant"
KIND = "request.qa"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "request_assistant.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def job_id_for(thread_id: str, message_id: str) -> str:
    from infra.worker import job_binding
    return "rqa-" + hashlib.sha256(json.dumps([thread_id, message_id, job_binding.fingerprint(KIND)]).encode("utf-8")).hexdigest()[:24]


class RequestAssistantExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        from infra.adapters import v2_business
        from infra.adapters import v2_request_assistant as vra
        from infra.adapters import v2_request_tasks
        from infra.adapters.v2_policy import permissions_for
        from infra.adapters.v2_postgres import TenantScopeError
        from infra import operator_flags
        if not operator_flags.assistant_on():                                                # switched off after the ask: stop before any provider call (normal accounting)
            raise NonRetryableExecutionError("assistant_off")
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        p = ctx.job["payload"] or {}
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        if not all(isinstance(p.get(k), str) and p.get(k) for k in ("thread_id", "message_id", "family", "request_id", "manifest_hash", "asked_by")):
            raise NonRetryableExecutionError("payload_invalid")
        with ctx.store._tx() as cur:
            cur.execute("""SELECT t.created_by, m.question, m.seq FROM v2_request_threads t JOIN v2_request_messages m ON m.tenant_id=t.tenant_id AND m.workspace_id=t.workspace_id
                           AND m.thread_id=t.thread_id WHERE t.tenant_id=%s AND t.workspace_id=%s AND t.thread_id=%s AND m.message_id=%s""", (tenant, ws, p["thread_id"], p["message_id"]))
            row = cur.fetchone()
        if row is None or row[0] != p["asked_by"]:
            raise NonRetryableExecutionError("thread_mismatch")
        asker, question, seq = row
        roles = v2_business.current_roles(ctx.store, tenant, ws, asker)
        if "request.assistant.ask" not in permissions_for(roles):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        authority = {"tenant_id": tenant, "workspace_id": ws, "account_id": asker, "roles": tuple(roles)}
        try:
            pack = vra.build_context(ctx.store, tenant, ws, p["family"], p["request_id"], authority)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("request_not_readable", "the person who asked can no longer read this request") from exc
        except ra.ContextOverflow as exc:                                                      # AIG-02: the mandatory facts no longer fit: refused before the provider
            raise NonRetryableExecutionError("context_overflow", exc.reason) from exc
        if pack["manifest_hash"] != p["manifest_hash"]:
            raise NonRetryableExecutionError("context_changed", "the request or the person's access changed after the question; ask again")
        frozen, sha = load_config()
        with ctx.store._tx() as cur:                                                          # the person's own earlier turns of THIS thread, kept blocks only
            cur.execute("""SELECT question, answer FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s AND seq < %s AND answer IS NOT NULL
                           ORDER BY seq DESC LIMIT %s""", (tenant, ws, p["thread_id"], seq, int(frozen["limits"]["max_history_turns"])))
            hist = [{"question": r[0], "blocks": ((r[1] if isinstance(r[1], dict) else json.loads(r[1] or "{}")) or {}).get("blocks") or []} for r in reversed(cur.fetchall())]
        perspective = ", ".join(sorted(roles)) or "member"
        try:
            prompt = ra.build_prompt(frozen, pack, hist, question, perspective=perspective)
        except ValueError as exc:
            raise NonRetryableExecutionError("budget_exceeded", "the request context is too large for one question") from exc
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the request context exceeds the input ceiling")
        tv = v2_request_tasks.task_view(ctx.store, tenant, ws, p["family"], p["request_id"], authority)
        next_actions = [{"action": a["action"], "label": a["label"]} for a in tv["permitted_actions"] if a.get("current")]   # only steps the asker is asked to do now
        ctx.request_context.update(rqa_frozen=frozen, rqa_sha=sha, rqa_pack=pack, rqa_next=next_actions)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, set(ra.REQUIRED_MODEL_KEYS))   # answer/5: no authority field from the model
        try:
            model = ra.validate_model_object(obj, rc["rqa_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        out = ra.check_answer(model, rc["rqa_pack"], rc["rqa_frozen"]["limits"])
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["rqa_sha"], **out, "next_actions": rc["rqa_next"], "as_of": rc["rqa_pack"]["as_of"],
                  "model_answer": model, "checks_version": ra.CHECKS_VERSION, "answer_schema": ra.ANSWER_SCHEMA, "locale": ra.LOCALE,                                                    # the model's own object before the checks (evaluation: raw vs kept); no route returns it
                  "manifest_hash": rc["rqa_pack"]["manifest_hash"], "context_version": rc["rqa_pack"]["version"], "provider_calls": 1, "applies_nothing": True, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
