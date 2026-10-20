"""Job kind "source.correspondence" (SUR-05), executor_id "source.correspondence.v1", role "source.correspondence": proposed old/new correspondence of a source refresh
(one model call).

Old side = deleted and changed artifacts (content before, with the rules of the working knowledge bound to them); new side = added and changed artifacts (content after); renamed
artifacts already carry a proof and are not sent. Binary content is never sent (listed instead). The server checks each proposal (core/change_intelligence/v2/source_correspondence.py),
adds `bytes_identical` and the proof the refresh contract would accept, and stores the result on the job only: the refresh, its rename proofs, artifact identities and the
knowledge are never changed; a person decides and, for a rename, supplies the proof through the existing commit path.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import source_correspondence as sc
from infra.adapters import v2_business
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "source.correspondence.v1"
KIND = ROLE = "source.correspondence"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "source_correspondence.json"
OPEN_STATUSES = ("PREPARED", "COMMITTED")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("refresh_id"), str) or not payload["refresh_id"].startswith("rfr-"):
        return "refresh_required"
    if not isinstance(payload.get("refresh_digest"), str) or isinstance(payload.get("attempt"), bool) or not isinstance(payload.get("attempt"), int) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    return None


def text_of(content: bytes) -> str | None:
    if b"\x00" in content[:2048]:
        return None
    return content.decode("utf-8", errors="replace")


def sides(store, tenant, ws, diff: dict, limits: dict) -> tuple[list, list, list]:
    """(old sides, new sides, withheld) of a refresh diff; at most `max_artifacts_per_side` per side, the rest withheld with the reason."""
    view = v2_business.business_view(store, tenant, ws)
    working = v2_business._working(view)
    rules = working["rules"] if working else []
    bound = {}
    for r in rules:
        if r["status"] in ("SUPERSEDED", "REJECTED"):
            continue
        for b in r.get("source_bindings") or []:
            bound.setdefault(b.get("artifact_id"), []).append(r)
    cap, max_chars = int(limits["max_artifacts_per_side"]), int(limits["max_excerpt_chars"])
    old, new, withheld = [], [], []

    def add(target, path, hash_, rules_=()):
        if len(target) >= cap:
            withheld.append({"path": path, "reason": "over_artifact_cap"})
            return
        text = text_of(store._verified_blob(hash_))
        if text is None:
            withheld.append({"path": path, "reason": "binary_not_sent"})
            return
        target.append(sc.side(path, text, hash_=hash_, max_chars=max_chars, rules=sorted(rules_, key=lambda r: r["rule_id"])))
    for d in diff.get("deleted") or []:
        add(old, d["logical_path"], d["before_hash"], bound.get(d["artifact_id"], []))
    for c in diff.get("changed") or []:
        add(old, c["logical_path"], c["before_hash"], bound.get(c["artifact_id"], []))
    for a in diff.get("added") or []:
        add(new, a["logical_path"], a["after_hash"])
    for c in diff.get("changed") or []:
        add(new, c["logical_path"], c["after_hash"])
    return old, new, withheld


class SourceCorrespondenceExecutor:
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
        asker = ctx.store.job_record(tenant, ws, ctx.job["job_id"]).get("requested_by")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, asker)
        if not {"project.read", "ai.use", "evidence.read", "source.import"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        refresh = ctx.store.source_refresh(tenant, ws, payload["refresh_id"])
        if refresh["refresh_digest"] != payload["refresh_digest"]:
            raise NonRetryableExecutionError("refresh_moved", "the refresh is not the one requested")
        if refresh["status"] not in OPEN_STATUSES:
            raise NonRetryableExecutionError("refresh_not_open", f"the refresh is {refresh['status']}")
        old, new, withheld = sides(ctx.store, tenant, ws, refresh["diff"] or {}, frozen["limits"])
        ctx.request_context.update(sc_frozen=frozen, sc_sha=sha, sc_old=old, sc_new=new,
                                   sc_coverage={"old_sides": [s["path"] for s in old], "new_sides": [s["path"] for s in new], "withheld": withheld,
                                                "renamed_with_proof": [r["to_path"] for r in (refresh["diff"] or {}).get("renamed") or []],
                                                "excerpts_cut": sorted(s["path"] for s in old + new if s["truncated"])}, sc_status=refresh["status"])
        if not old:
            return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0, external=False)     # nothing on the old side to reconnect: no call
        old_block, new_block = sc.prompt_block(old, new)
        prompt = frozen["prompt_template"].replace("{old}", old_block).replace("{new}", new_block)
        from infra.worker import extraction
        if len(prompt) > int(frozen["limits"]["max_prompt_chars"]) or extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the excerpts exceed the input ceiling")
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        payload = ctx.job["payload"]
        model_id = actual = in_t = out_t = th_t = None
        if request.prompt is None:
            model_status, out = "not_run", {"kept": [], "rejected": [], "notes": [], "unmatched_old": []}
        else:
            obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, sc.MODEL_KEYS)
            try:
                model = sc.validate_model_object(obj, rc["sc_frozen"]["limits"])
            except ValueError as exc:
                raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
            model_status, out = model["status"], sc.check(model, rc["sc_old"], rc["sc_new"], rc["sc_frozen"]["limits"])
        result = {"schema": sc.SCHEMA, "executor_id": EXECUTOR_ID, "config_sha256": rc["sc_sha"], "refresh_id": payload["refresh_id"], "refresh_digest": payload["refresh_digest"],
                  "refresh_status_at_run": rc["sc_status"], "model_status": model_status, "proposals": out["kept"], "rejected": out["rejected"], "notes": out["notes"],
                  "unmatched_old": out["unmatched_old"], "coverage": rc["sc_coverage"], "label": sc.LABEL, "provider_calls": 0 if request.prompt is None else 1,
                  "applies_nothing": True, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
