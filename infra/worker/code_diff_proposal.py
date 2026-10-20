"""Job kind "code.diff_proposal" (SUR-03, proposal-only), executor_id "code.diff_proposal.v1", role "code.diff_proposal": a model's explanation and proposed edits for the
code change a CONFIRMED business change needs (one model call).

Files are read with `git show <base>:<path>` from the declared repository at the base commit pinned when the request was made: nothing is cloned, written, applied or run.
Only recommendation paths that resolve to exactly one repository file inside the allowlist (not protected, not a test) are offered; the rest are listed with the reason. The
server checks every edit and builds the diff in memory (core/change_intelligence/v2/code_proposal.py). The proposal is stored on the job only; the literal candidate path
(FEX-03) stays the only way to a patch, and model-written code is never executed without a VM-class sandbox (not granted).
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import yaml

from core.change_intelligence.v2 import business_impact as bim
from core.change_intelligence.v2 import code_patch
from core.change_intelligence.v2 import code_proposal as cp
from infra.adapters import v2_business
from infra.worker.change_draft import _canon_json
from infra.worker.code_change import _git, repo_config
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "code.diff_proposal.v1"
KIND = ROLE = "code.diff_proposal"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "code_diff_proposal.json"
PROPOSABLE = ("SUPPORTED", "UNCERTAIN")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not str(payload.get("change_id", "")).startswith("chg-") or not isinstance(payload.get("repo_id"), str):
        return "payload_invalid"
    if not isinstance(payload.get("base_sha"), str) or len(payload["base_sha"]) != 40 or not isinstance(payload.get("recommendation_hash"), str):
        return "payload_invalid"
    if isinstance(payload.get("attempt"), bool) or not isinstance(payload.get("attempt"), int) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    return None


def recommendations(store, tenant, ws, change_id):
    """(recommendation, operations, request text) of a confirmed change, built exactly as the code read route builds it; None when not confirmed."""
    from core.change_intelligence.v2 import code_recommendation
    from infra.adapters import v2_business_review
    got = v2_business_review.current_draft(store, tenant, ws, change_id)
    if got is None:
        return None
    draft, _w, parts = got
    head = store.head(tenant, ws)
    current = v2_business._working(v2_business.business_view(store, tenant, ws))
    rec = code_recommendation.build_recommendations(draft, parts["intent"]["operations"], head={"source_revision_id": head.source_revision_id,
                                                                                           "snapshot_id": current["snapshot"]["snapshot_id"] if current else None})
    return rec, parts["intent"]["operations"], parts["intent"]["request"]


class CodeDiffProposalExecutor:
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
        limits = frozen["limits"]
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        asker = ctx.store.job_record(tenant, ws, ctx.job["job_id"]).get("requested_by")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, asker)
        if not {"code.patch", "ai.use", "evidence.read"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        got = recommendations(ctx.store, tenant, ws, payload["change_id"])
        if got is None:
            raise NonRetryableExecutionError("change_not_confirmed")
        rec, operations, request_text = got
        if rec["recommendation_hash"] != payload["recommendation_hash"] or rec["stale"]:
            raise NonRetryableExecutionError("stale_binding", "the recommendations changed after the request")
        repo = repo_config(ws, payload["repo_id"])
        base = payload["base_sha"]
        tracked = [p for p in _git(repo["path"], "ls-tree", "-r", "--name-only", base).splitlines() if p]
        offered, withheld, shown_items = {}, [], []
        for item in rec["items"]:
            if item["status"] not in PROPOSABLE or not item.get("path"):
                continue
            path, why = code_patch.resolve_file(item["path"], tracked)
            reason = why or (code_patch.path_allowed(path, repo["allowlist"], repo["protected"]) if path else None) or ("test_file" if path and cp.is_test_file(path) else None)
            if reason:
                withheld.append({"ref": item["ref"], "path": item["path"], "reason": reason})
                continue
            if path not in offered:
                if len(offered) >= int(limits["max_files"]):
                    withheld.append({"ref": item["ref"], "path": path, "reason": "over_file_cap"})
                    continue
                text = _git(repo["path"], "show", f"{base}:{path}")
                if len(text) > int(limits["max_file_chars"]):
                    withheld.append({"ref": item["ref"], "path": path, "reason": "file_too_large"})
                    continue
                offered[path] = text
            shown_items.append((item, path))
        ops = {o["op_id"]: o for o in operations}
        change = "\n".join(f"- {bim._action(o, o['rule_id'])}" for o in operations) + f"\nRequest: {request_text}"
        recs = "\n".join(f"- ref={i['ref']} file={p} status={i['status']} rule={i['rule_id']}: {i.get('why') or ''} Desired: {(i.get('desired') or {}).get('text') or ''}"
                         f" Observed: {i.get('observed_logic') or ''}" for i, p in shown_items)
        files = "\n".join(f"=== file {p}\n{t}\n=== end {p}" for p, t in sorted(offered.items()))
        ctx.request_context.update(cp_frozen=frozen, cp_sha=sha, cp_offered=offered, cp_refs=[i["ref"] for i, _p in shown_items], cp_repo=repo,
                                   cp_coverage={"offered_files": sorted(offered), "withheld": withheld, "items_offered": len(shown_items), "operations": sorted(ops)})
        if not offered:
            return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0, external=False)       # nothing may be offered: no call
        prompt = frozen["prompt_template"].replace("{change}", change).replace("{recommendations}", recs).replace("{files}", files)
        from infra.worker import extraction
        if len(prompt) > int(limits["max_prompt_chars"]) or extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the files exceed the input ceiling")
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        p = ctx.job["payload"]
        model_id = actual = in_t = out_t = th_t = None
        if request.prompt is None:
            model_status = "not_run"
            out = {"files": [], "diff": "", "patch_hash": None, "kept_edits": [], "rejected": [], "explanation": "", "suggested_tests": [], "notes": [],
                   "static_checks_passed": False, "applied": False, "executed": False}
        else:
            obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, cp.MODEL_KEYS)
            try:
                model = cp.validate_model_object(obj, rc["cp_frozen"]["limits"])
            except ValueError as exc:
                raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
            model_status = model["status"]
            out = cp.check(model, rc["cp_offered"], rc["cp_refs"], allowlist=rc["cp_repo"]["allowlist"], protected=rc["cp_repo"]["protected"],
                           limits=rc["cp_frozen"]["limits"], yaml_reader=yaml.safe_load)
        result = {"schema": cp.SCHEMA, "executor_id": EXECUTOR_ID, "config_sha256": rc["cp_sha"], "change_id": p["change_id"], "repo_id": p["repo_id"], "base_sha": p["base_sha"],
                  "recommendation_hash": p["recommendation_hash"], "model_status": model_status, **out, "coverage": rc["cp_coverage"], "label": cp.LABEL,
                  "provider_calls": 0 if request.prompt is None else 1, "applies_nothing": True, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
