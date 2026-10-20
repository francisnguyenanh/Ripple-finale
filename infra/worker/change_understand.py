"""Job kind "change.understand", executor_id "change.understand.v1", role "change.understand": a business change request -> a validated understanding (one model call).

The model proposes operations over the rules it is shown (the pinned snapshot's rules, all of them when there are few, otherwise the best matches); the server keeps an operation
only if it quotes the request, names a shown rule and takes its numbers and scope values from the request (core/change_intelligence/v2/business_change.py). The result is stored
with the snapshot pin and a preview hash; a person confirms it separately. Nothing here changes knowledge, files or code. The asker's rights are rechecked when the job runs; a
moved snapshot fails the job visibly. Role `change.understand` must be pinned in the budget config, the endpoint must be the Vertex global one.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import business_change as bc
from core.change_intelligence.v2 import business_index as bi
from infra.adapters import v2_business, v2_business_index
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import extraction
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "change.understand.v1"
ROLE = "change.understand"
KIND = "change.understand"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    frozen, _ = load_config()
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), str) or not payload["request"].strip() or len(payload["request"]) > frozen["limits"]["max_request_chars"]:
        return "request_invalid"
    if not isinstance(payload.get("snapshot_id"), str) or not payload["snapshot_id"]:
        return "snapshot_required"
    choice = payload.get("decided_conflict_intent")
    if choice is not None and not (isinstance(choice, dict) and set(choice) == {"rule_id", "intent"} and isinstance(choice["rule_id"], str) and choice["rule_id"]
                                   and choice["intent"] in bc.DECIDED_CONFLICT_INTENTS):
        return "decided_conflict_intent_invalid"
    return None


class ChangeUnderstandExecutor:
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
        if not {"intent.create", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, record.get("requested_by")):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        view = v2_business.business_view(ctx.store, tenant, ws)
        snap = view["accepted"]
        if snap is None or snap["snapshot"]["snapshot_id"] != payload["snapshot_id"]:
            raise NonRetryableExecutionError("snapshot_moved", "the accepted knowledge changed after the request was made")
        meta = snap["snapshot"]
        head = view["workspace"]
        if (meta["source_revision_id"], meta["km_revision_id"], meta["decision_epoch"]) != (head["source_revision_id"], head["knowledge_revision_id"], head["decision_epoch"]):
            raise NonRetryableExecutionError("snapshot_moved", "the source or decision head moved after the accepted knowledge")
        live = [r for r in snap["rules"] if r["status"] not in ("SUPERSEDED", "REJECTED")]
        limit = frozen["limits"]["max_known_rules"]
        if len(live) > limit:      # too many to show: the best matches of the request, never an arbitrary cut
            pack = v2_business_index.context_pack(ctx.store, tenant, ws, payload["request"], view="target", top_k=limit, semantic=False, expected_snapshot_id=meta["snapshot_id"])
            keep = {i["item_id"] for i in pack["items"] if i["kind"] == "rule"} | {m["item_id"] for i in pack["items"] if i["kind"] == "rule" for m in i["versions"]}
            live = [r for r in live if r["rule_id"] in keep]
        offered = {r["rule_id"]: r for r in live}
        items = [i for i in bi.build_items(live, [], []) if i["kind"] == "rule"]
        prompt = bc.build_prompt(frozen, payload["request"], items, max_chars=frozen["limits"]["max_prompt_chars"])
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "request and rules exceed the input ceiling")
        pin = {"snapshot_id": meta["snapshot_id"], "business_revision": meta["business_revision"], "source_revision_id": meta["source_revision_id"],
               "km_revision_id": meta["km_revision_id"], "decision_epoch": meta["decision_epoch"]}
        ctx.request_context.update(cu_frozen=frozen, cu_sha=sha, cu_rules=offered, cu_relations=snap["relations"], cu_pin=pin)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, bc.MODEL_KEYS)
        try:
            model = bc.validate_model_object(obj, rc["cu_frozen"]["limits"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        payload = ctx.job["payload"]
        choice = payload.get("decided_conflict_intent")
        result = bc.resolve(model, payload["request"], rc["cu_rules"], rc["cu_relations"], rc["cu_frozen"]["limits"], pin=rc["cu_pin"],
                            choices={choice["rule_id"]: choice["intent"]} if choice else None)
        result.update(executor_id=EXECUTOR_ID, config_sha256=rc["cu_sha"], provider_calls=1, applies_nothing=True, offered_rule_ids=sorted(rc["cu_rules"]),
                      titles={rid: r["title"] for rid, r in rc["cu_rules"].items()}, model={"pinned": model_id, "served": actual})
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
