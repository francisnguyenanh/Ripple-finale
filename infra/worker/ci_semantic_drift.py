"""Job kind "ci.semantic_drift" (SUR-04), executor_id "ci.semantic_drift.v1", role "ci.semantic_drift": a semantic drift ADVISORY over a declared repository's diff
(base..head, both resolved to SHAs when the request was made) next to the exact MON-CI scalar check (one model call).

The exact part is deterministic and computed first: when the repository declares a protected `ci_mapping`, `business_ci.run` reads the mapped YAML scalars at both commits
(`git show`, the repository's code is never run) and the report is stored unchanged. The model reads the bounded diff and the accepted rules; the server keeps a finding only if
its witness lines are verbatim changed lines (core/change_intelligence/v2/semantic_drift.py). Paths outside the repository's allowlist (when one is declared) and secret-like
files are never sent; what was left out is listed. Nothing is written to the repository, knowledge or a branch protection; "no finding" is never a PASS.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import pathlib

import yaml

from core.change_intelligence.v2 import business_ci as ci
from core.change_intelligence.v2 import semantic_drift as sd
from infra.adapters import v2_business
from infra.worker.change_draft import _canon_json
from infra.worker.code_change import _git, repo_config
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "ci.semantic_drift.v1"
KIND = ROLE = "ci.semantic_drift"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "ci_semantic_drift.json"
SECRET_LIKE = ("*.env", ".env*", "*.pem", "*.key", "*secret*", "*credential*", "*password*", "id_rsa*", "*.p12", "*.pfx")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not all(isinstance(payload.get(k), str) and payload[k] for k in ("repo_id", "base_sha", "head_sha", "snapshot_id")):
        return "payload_invalid"
    if any(len(payload[k]) != 40 or any(c not in "0123456789abcdef" for c in payload[k]) for k in ("base_sha", "head_sha")):
        return "payload_invalid"
    if isinstance(payload.get("attempt"), bool) or not isinstance(payload.get("attempt"), int) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    return None


def sendable(path: str, allowlist) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if any(fnmatch.fnmatch(name, p) for p in SECRET_LIKE):
        return False
    return not allowlist or any(fnmatch.fnmatch(path, p) for p in allowlist)


def _reader(repo_path: str, sha: str):
    cache: dict = {}

    def read(path: str, key: str):
        if path not in cache:
            try:
                cache[path] = yaml.safe_load(_git(repo_path, "show", f"{sha}:{path}"))
            except (NonRetryableExecutionError, yaml.YAMLError):
                cache[path] = ci.UNREADABLE
        node = cache[path]
        for part in key.split("."):
            if node is ci.UNREADABLE or not isinstance(node, dict) or part not in node:
                return ci.UNREADABLE
            node = node[part]
        return node if isinstance(node, (int, float, str)) and not isinstance(node, bool) else ci.UNREADABLE
    return read


def exact_check(repo: dict, repo_id: str, policy: dict, base_sha: str, head_sha: str, changed: list) -> dict:
    """The MON-CI scalar check, unchanged, or why it did not run."""
    mapping = repo.get("ci_mapping")
    if not isinstance(mapping, dict):
        return {"status": "not_configured", "statement": "This repository declares no protected mapping; no exact scalar check ran."}
    report = ci.run(policy, mapping, repo_id=repo_id, base_sha=base_sha, head_sha=head_sha, read_base=_reader(repo["path"], base_sha),
                    read_head=_reader(repo["path"], head_sha), changed_paths=changed)
    return {"status": "ran", "conclusion": report["conclusion"], "summary": report["summary"], "coverage": report["coverage"], "report_hash": report["report_hash"],
            "report": report}


class SemanticDriftExecutor:
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
        if not {"project.read", "ai.use", "evidence.read"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        acc = v2_business.business_view(ctx.store, tenant, ws)["accepted"]
        if acc is None or acc["snapshot"]["snapshot_id"] != payload["snapshot_id"]:
            raise NonRetryableExecutionError("policy_moved", "the accepted knowledge changed after the request")
        repo = repo_config(ws, payload["repo_id"])
        base, head = payload["base_sha"], payload["head_sha"]
        for c in (base, head):
            _git(repo["path"], "cat-file", "-e", f"{c}^{{commit}}")
        changed = [p for p in _git(repo["path"], "diff", "--name-only", base, head).splitlines() if p]
        rules = {r["rule_id"]: r for r in acc["rules"] if r["status"] not in ("SUPERSEDED", "REJECTED")}
        policy = {"snapshot_id": acc["snapshot"]["snapshot_id"], "business_revision": acc["snapshot"]["business_revision"], "rules": list(acc["rules"])}
        exact = exact_check(repo, payload["repo_id"], policy, base, head, changed)
        limits = frozen["limits"]
        sent = [p for p in changed if sendable(p, repo.get("allowlist"))]
        withheld = sorted(set(changed) - set(sent))
        diff = _git(repo["path"], "diff", "--unified=3", "--no-color", base, head, "--", *sent) if sent else ""
        truncated = len(diff) > int(limits["max_diff_chars"])
        diff = diff[: int(limits["max_diff_chars"])]
        shown_rules = sorted(rules)[: int(limits["max_rules"])]
        truncation = (f"(The diff was cut at {limits['max_diff_chars']} characters; findings beyond it cannot be reported.)" if truncated else "")
        prompt = (frozen["prompt_template"].replace("{rules}", "\n".join(sd.rule_line(rules[r]) for r in shown_rules)).replace("{diff}", diff or "(no sendable change)")
                  .replace("{truncation}", truncation))
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the diff and the rules exceed the input ceiling")
        ctx.request_context.update(sd_frozen=frozen, sd_sha=sha, sd_index=sd.index_diff(diff, max_lines_per_file=int(limits["max_lines_per_file"])),
                                   sd_rules={r: rules[r] for r in shown_rules}, sd_exact=exact,
                                   sd_coverage={"changed_paths": len(changed), "sent_paths": sent, "withheld_paths": withheld, "diff_truncated": truncated,
                                                "rules_offered": len(shown_rules), "rules_not_offered": max(0, len(rules) - len(shown_rules))})
        if not sent:
            return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0, external=False)     # nothing may be sent: no call, the exact result stands alone
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        payload = ctx.job["payload"]
        model_id = actual = None
        in_t = out_t = th_t = None
        if request.prompt is None:
            model_status, semantic = "not_run", {"kept": [], "rejected": [], "notes": [], "conclusion": "not_run_nothing_sendable", "never_a_pass": True}
        else:
            obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, sd.MODEL_KEYS)
            try:
                model = sd.validate_model_object(obj, rc["sd_frozen"]["limits"])
            except ValueError as exc:
                raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
            model_status, semantic = model["status"], sd.check(model, rc["sd_index"], rc["sd_rules"], rc["sd_frozen"]["limits"])
        result = {"schema": sd.SCHEMA, "executor_id": EXECUTOR_ID, "config_sha256": rc["sd_sha"], "repo_id": payload["repo_id"], "base_sha": payload["base_sha"],
                  "head_sha": payload["head_sha"], "snapshot_id": payload["snapshot_id"], "mode": "advisory", "exact": rc["sd_exact"], "model_status": model_status,
                  "semantic": semantic, "coverage": rc["sd_coverage"], "label": sd.LABEL, "provider_calls": 0 if request.prompt is None else 1, "applies_nothing": True,
                  "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
