"""Job kind "impact.judge" -- ONE model judgment for ONE (target rule, artifact) pair:
does this bundle artifact depend on the rule being changed. Modelled on `gap_answer.GapAnswerExecutor` (registered
executor, exactly one provider call per attempt, prepare/execute/validate, reservation through the normal job
admission path). Nothing here writes knowledge, candidates or decisions.

Fail-closed rules (all deterministic; every refusal below happens in `prepare`, i.e. with ZERO provider calls):
* the target candidate must be in `scenario_candidate_ids` and resolvable in this tenant/workspace;
* the artifact must exist in the pinned revision, be in `scope_artifact_ids`, share the target artifact's bundle prefix
  (a payload scope may not reach another bundle), not be the target artifact, and be rule-bearing (data artifacts are
  evidence, not rules);
* an artifact already flagged by the deterministic rules (1)/(2) is never sent to the model (`already_deterministic`);
* an artifact with more than `extraction._MAX_CONTEXT_CHUNKS` chunks, or a prompt over the input ceiling, is
  `budget_exceeded` (never truncated);
After the provider returns: the output must be exactly {depends: bool, evidence_quote: str|null, reason: str} (else the
job fails); `accepted` is true ONLY when depends is true and the quote is an exact substring of the artifact's stored
observation text (`impact.quote_supported`, the same validator impact.analyse re-applies). The artifact text is sent as
an untrusted-data block. The frozen prompt and schema live in `config/ai10_impact_judge.json`; its LF-normalised sha256
is stamped into every result together with the sha256 of the exact prompt sent. No label/oracle text is ever an input."""
from __future__ import annotations

from infra.worker.provider_response import require_normal_finish

import hashlib
import json
import math
import pathlib

from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import extraction, impact
from infra.worker.gap_answer import _model_object
from infra.worker.execution_types import (is_timeout_exception, 
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError,
    ProviderCallBudget, ProviderExecutionError, ProviderInvalidResponseError,
    ProviderRateLimitError, ProviderTimeoutError,
)
from infra.worker.live_analysis import _client_for_model, _field, _token_count

CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "ai10_impact_judge.json"
_MODEL_KEYS = {"depends", "evidence_quote", "reason"}


def load_frozen_config(path=None) -> tuple[dict, str]:
    raw = pathlib.Path(path or CONFIG_PATH).read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    """Shared by the route and the executor: an error code, or None when the payload shape is valid."""
    if not isinstance(payload, dict):
        return "payload_not_an_object"
    for key in ("target_candidate_id", "artifact_id"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return f"{key}_required"
    for key in ("scope_artifact_ids", "scenario_candidate_ids"):
        ids = payload.get(key)
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids):
            return f"{key}_required"
    if payload["target_candidate_id"] not in payload["scenario_candidate_ids"]:
        return "target_not_in_scenario"
    if payload["artifact_id"] not in payload["scope_artifact_ids"]:
        return "artifact_not_in_scope"
    return None


def _check_shape(obj: dict) -> None:
    def bad(why):
        raise NonRetryableExecutionError("schema_violation", why)
    if set(obj) != _MODEL_KEYS:
        bad("keys")
    if not isinstance(obj["depends"], bool):
        bad("depends")
    if not (obj["evidence_quote"] is None or isinstance(obj["evidence_quote"], str)):
        bad("evidence_quote")
    if not isinstance(obj["reason"], str):
        bad("reason")


class ImpactJudgeExecutor:
    external = True
    executor_id = "impact.judge.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        payload = ctx.job["payload"]
        err = validate_payload(payload)
        if err:
            raise NonRetryableExecutionError(err)
        frozen, sha = load_frozen_config()
        tenant, ws, rev = ctx.job["tenant_id"], ctx.job["workspace_id"], ctx.job["pinned_source_revision_id"]
        target_id, aid = payload["target_candidate_id"], payload["artifact_id"]
        found, _missing = ctx.store.find_candidates_by_id(tenant, ws, [target_id])
        target = found.get(target_id)
        if target is None:
            raise NonRetryableExecutionError("target_candidate_unavailable")
        try:
            view = ctx.store.source_revision_view(tenant, ws, rev)
            all_obs = ctx.store.source_observations(tenant, ws, rev)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("source_artifact_unavailable") from exc
        by_id = {a["artifact_id"]: a for a in view["artifacts"]}
        tart = by_id.get(target.get("artifact_revision_id"))
        art = by_id.get(aid)
        if tart is None or art is None or any(i not in by_id for i in payload["scope_artifact_ids"]):
            raise NonRetryableExecutionError("source_artifact_unavailable", "artifact outside the pinned revision")
        bundle = impact.bundle_prefix(tart)
        if any(impact.bundle_prefix(by_id[i]) != bundle for i in payload["scope_artifact_ids"]):
            raise NonRetryableExecutionError("artifact_out_of_scope", "scope is not confined to the target's bundle")
        if aid == tart["artifact_id"]:
            raise NonRetryableExecutionError("artifact_out_of_scope", "the target artifact is never judged")
        if not impact._is_rule_bearing(art):
            raise NonRetryableExecutionError("not_rule_bearing")
        obs = sorted((o for o in all_obs if o["artifact_id"] == aid and extraction._observation_text(o) is not None),
                     key=lambda o: str(o["observation_id"]))
        deterministic, _omitted = impact.scan_indirect_artifacts(
            target=target, target_artifact=tart, artifacts=by_id, observations=all_obs, scope_artifact_ids={aid})
        if deterministic:
            raise NonRetryableExecutionError("already_deterministic", "flagged by the deterministic rules")
        path = art.get("logical_path") or aid
        limit = extraction._MAX_CONTEXT_CHUNKS
        if max(len(obs), 1) > limit:          # an artifact without recorded chunks is one unchunked unit
            raise NonRetryableExecutionError("budget_exceeded", f"{path}: {max(len(obs), 1)} chunks exceed {limit}")
        if obs:
            body = "\n\n".join(extraction._observation_text(o) for o in obs)
        else:
            try:
                body = ctx.store.read_blob(tenant, ws, rev, aid).decode("utf-8")
            except (KeyError, FileNotFoundError, UnicodeDecodeError, ValueError, TenantScopeError,
                    NotImplementedError) as exc:
                raise NonRetryableExecutionError("source_artifact_unavailable") from exc
        if not body.strip():
            raise NonRetryableExecutionError("source_artifact_empty")
        prompt = frozen["prompt_template"]
        for key, val in (("{subject}", target.get("subject")), ("{predicate}", target.get("predicate")),
                         ("{value}", target.get("value")), ("{unit}", target.get("unit")),
                         ("{target_path}", tart.get("logical_path") or tart["artifact_id"]),
                         ("{artifact_path}", path)):
            prompt = prompt.replace(key, str(val))
        prompt = prompt.replace("{artifact_text}", body)       # last: the artifact text is never re-expanded
        if math.ceil(len(prompt.encode("utf-8")) / 2) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "source prompt exceeds input ceiling")
        ctx.request_context.update(judge_obs=obs, judge_config_sha=sha,
                                   judge_prompt_sha=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                                   judge_text=body)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1,
                                external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
                calls: ProviderCallBudget) -> ExecutionOutcome:
        config = ctx.config
        from infra.worker.budget_config import model_for_role
        model_id = model_for_role(config, "impact.judge")
        pinned = "impact.judge" in (getattr(config, "role_model_ids", None) or {})
        if pinned:                      # a pinned role: Vertex GLOBAL endpoint only, served by exactly that model, no fallback
            from infra.worker.change_draft import _assert_global_endpoint
            _assert_global_endpoint()
        calls.consume()
        try:
            client = _client_for_model(model_id)
            response = client.models.generate_content(
                model=model_id, contents=request.prompt,
                config={"max_output_tokens": ctx.max_output_tokens, "temperature": 0.0,
                        "response_mime_type": "application/json"})
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if callable(code):
                code = code()
            if code == 429:
                raise ProviderRateLimitError("provider rate limited") from exc
            if is_timeout_exception(exc, code):
                raise ProviderTimeoutError("provider timed out") from exc
            raise ProviderExecutionError("provider request failed") from exc
        actual_model = _field(response, "model_version") or model_id
        if pinned and actual_model != model_id:
            raise NonRetryableExecutionError("served_model_differs_from_pinned_role_model", f"{actual_model}")
        if not isinstance(actual_model, str) or actual_model not in config.model_prices:
            raise NonRetryableExecutionError("unpriced_model")
        require_normal_finish(response, model_id=model_id, requested_cap=getattr(ctx, "max_output_tokens", None))
        usage = _field(response, "usage_metadata")
        input_tokens = _token_count(usage, "prompt_token_count")
        output_tokens = _token_count(usage, "candidates_token_count")
        thought_tokens = _token_count(usage, "thoughts_token_count")
        total = _token_count(usage, "total_token_count")
        if total is not None and input_tokens is not None and output_tokens is not None:
            thought_tokens = max(thought_tokens or 0, total - input_tokens - output_tokens)
        try:
            text = _field(response, "text")
        except (ValueError, IndexError, AttributeError) as exc:
            raise ProviderInvalidResponseError("provider returned no text") from exc
        obj = _model_object(text)
        _check_shape(obj)
        return ExecutionOutcome(result=self._validate(ctx, obj), model_id=actual_model, input_tokens=input_tokens,
                                output_tokens=output_tokens, thought_tokens=thought_tokens)

    def _validate(self, ctx: ExecutionContext, obj: dict) -> dict:
        rc, payload = ctx.request_context, ctx.job["payload"]
        quote = obj["evidence_quote"]
        rejected = None
        if not obj["depends"]:
            rejected = "no_dependency"
        elif not isinstance(quote, str) or not quote.strip():
            rejected = "quote_missing"
        elif not (impact.quote_supported(quote, rc["judge_obs"])
                  or (not rc["judge_obs"] and len(quote.strip()) >= impact.MIN_QUOTE_LEN and quote in rc["judge_text"])):
            rejected = "quote_not_substring"
        accepted = rejected is None
        return {"accepted": accepted, "depends": obj["depends"],
                "evidence_quote": quote if accepted else None, "rejected": rejected,
                "artifact_id": payload["artifact_id"], "target_candidate_id": payload["target_candidate_id"],
                "reason": str(obj["reason"])[:300], "config_sha256": rc["judge_config_sha"],
                "prompt_sha256": rc["judge_prompt_sha"], "executor_id": self.executor_id,
                "live_call_provenance": "ripple_runtime"}
