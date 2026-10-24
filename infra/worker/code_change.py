"""Extension contract ext/1 (FEX-03..07), LOCAL ONLY: deterministic jobs that build a candidate code patch for a committed business change, validate it in an isolated
checkout, promote the validated tree into a MOCK target and verify it there. 0 provider calls. Every job works on a disposable clone of a repository the operator declared
in `RIPPLE_EXTENSION_CONFIG`; in this build that is always a fixture repository created by the tests. Nothing here pushes, merges, deploys or touches a real environment:
those actions have no grant and are refused before anything runs.

  code.candidate  payload {change_id, repo_id}                 -> diff + identities (patch_hash, base_sha, target_pin), implemented / not implemented items
  code.validate   payload {candidate_job_id}                   -> report bound to (patch_hash, base_sha, runtime_digest): PASSED | FAILED | INFRASTRUCTURE_FAILED
  code.promote    payload {candidate_job_id, environment_id}   -> mock promotion: apply + local commit in a clone, copy the tree to <mock_target>/<env>/current, ledger row
  code.verify     payload {candidate_job_id, environment_id}   -> declared scenario checks against the mock target, ledger row (VERIFIED | FAILED)
  code.rollback   payload {candidate_job_id, environment_id}   -> previous tree restored in the mock target, ledger row

Isolation statement: commands run as timed subprocesses with output caps in a throwaway directory (advisory; NOT an OS-level sandbox). Hostile code is out of scope and the
report says so.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from core.change_intelligence.v2 import code_patch
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget

KINDS = ("code.candidate", "code.validate", "code.promote", "code.verify", "code.rollback")
ISOLATION = "timed subprocess with output caps in a throwaway clone; advisory only, not an OS-level sandbox; hostile code out of scope"
MAX_DIFF_BYTES = 200_000
MAX_OUTPUT = 20_000
_SENSITIVE = __import__("re").compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|DSN|^RIPPLE_|^GOOGLE_|^AWS_|^AZURE_)", __import__("re").I)


# ---- operator configuration ---------------------------------------------------------------------------------------------------------------------------------------------

def extension_config() -> dict:
    raw = os.environ.get("RIPPLE_EXTENSION_CONFIG")
    if not raw:
        return {}
    text = pathlib.Path(raw).read_text(encoding="utf-8") if os.path.isfile(raw) else raw
    return json.loads(text)


def repo_config(workspace_id: str, repo_id: str) -> dict:
    ws = (extension_config().get("workspaces") or {}).get(workspace_id) or {}
    repo = (ws.get("repos") or {}).get(repo_id)
    if not repo or not repo.get("path") or (not os.path.isdir(repo["path"]) and not isinstance(repo.get("remote"), dict)):   # a remote's mirror is created by git.fetch
        raise NonRetryableExecutionError("repo_not_declared", f"no declared repository {repo_id!r} for this workspace")
    repo.setdefault("allowlist", [])
    repo.setdefault("protected", list(code_patch.PROTECTED_DEFAULT))
    repo.setdefault("validate", [])
    repo.setdefault("verify", [])
    return repo


def candidate_job_id(change_id: str, repo_id: str, recommendation_hash: str) -> str:
    return "cand-" + canonical_hash({"change": change_id, "repo": repo_id, "rec": recommendation_hash})[:20]


def repair_candidate_job_id(parent_candidate_job_id: str, n: int) -> str:
    """The n-th repair candidate of a candidate (AIX-C1): same identity family, new patch hash."""
    return f"{parent_candidate_job_id}-r{int(n)}"


def validation_job_id(candidate_job_id_: str) -> str:
    return "cval-" + canonical_hash({"candidate": candidate_job_id_})[:20]


def promotion_job_id(candidate_job_id_: str, environment_id: str) -> str:
    return "cprm-" + canonical_hash({"candidate": candidate_job_id_, "env": environment_id})[:20]


def verify_job_id(candidate_job_id_: str, environment_id: str, artifact_hash: str) -> str:
    return "cver-" + canonical_hash({"candidate": candidate_job_id_, "env": environment_id, "artifact": artifact_hash})[:20]


def rollback_job_id(candidate_job_id_: str, environment_id: str, n: int) -> str:
    return "crbk-" + canonical_hash({"candidate": candidate_job_id_, "env": environment_id, "n": n})[:20]


# ---- git and command helpers (the repository's own tooling, never a model's prose) ------------------------------------------------------------------------------------------

def _git(cwd, *args, timeout=120) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise NonRetryableExecutionError("git_failed", f"git {args[0]}: {r.stderr.strip()[:300]}")
    return r.stdout


def clone(repo_path: str, base_sha: str | None = None) -> str:
    tmp = tempfile.mkdtemp(prefix="ripple-ext-")
    _git(tempfile.gettempdir(), "clone", "--quiet", "--no-hardlinks", repo_path, tmp)
    if base_sha:
        _git(tmp, "checkout", "--quiet", base_sha)
    return tmp


def tracked_files(checkout: str) -> list:
    return [l for l in _git(checkout, "ls-files").splitlines() if l]


def isolation_for(repo: dict) -> str:
    from infra.worker import sandbox_container
    return sandbox_container.ISOLATION_CONTAINER if sandbox_container.settings(repo) else ISOLATION


def run_commands(cwd: str, commands: list, *, timeout_seconds: int, repo: dict | None = None) -> dict:
    """Each declared command as a timed subprocess (default), or in its own locked-down container when the declared repository asks for `runtime.kind = container`;
    (status, rows). A command that cannot start is an infrastructure failure, never a product failure."""
    from infra.worker import sandbox_container
    cfg = sandbox_container.settings(repo or {})
    if cfg is not None:
        return sandbox_container.run(cwd, commands, timeout_seconds=timeout_seconds, cfg=cfg, max_output=MAX_OUTPUT)
    rows, status = [], "PASSED"
    env = {k: v for k, v in os.environ.items() if not _SENSITIVE.search(k)}            # the repository's tooling needs the interpreter's environment; secrets and Ripple settings never reach it
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "TEMP": cwd, "TMP": cwd})
    for argv in commands:
        argv = [sys.executable if a == "{python}" else a for a in argv]
        t0 = time.monotonic()
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, env=env, encoding="utf-8", errors="replace")
            out = (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-MAX_OUTPUT:]
            rows.append({"argv": argv, "exit": r.returncode, "seconds": round(time.monotonic() - t0, 2), "output_tail": out})
            if r.returncode != 0 and status == "PASSED":
                status = "FAILED"
        except subprocess.TimeoutExpired as exc:
            rows.append({"argv": argv, "exit": None, "seconds": timeout_seconds, "output_tail": "timeout"})
            status = "FAILED" if status == "PASSED" else status
        except (OSError, ValueError) as exc:
            rows.append({"argv": argv, "exit": None, "seconds": 0, "output_tail": f"could not start: {exc}"})
            status = "INFRASTRUCTURE_FAILED"
    return {"status": status, "commands": rows}


def runtime_digest(checkout: str) -> str:
    locks = [p for p in ("requirements.txt", "requirements-dev.txt", "poetry.lock", "package-lock.json") if os.path.isfile(os.path.join(checkout, p))]
    h = hashlib.sha256(sys.version.encode("utf-8"))
    for p in locks:
        h.update(pathlib.Path(checkout, p).read_bytes())
    return h.hexdigest()


def tree_hash(root: str) -> str:
    h = hashlib.sha256()
    for path in sorted(str(p.relative_to(root)).replace("\\", "/") for p in pathlib.Path(root).rglob("*") if p.is_file() and ".git" not in p.parts):
        h.update(path.encode("utf-8") + b"\0" + pathlib.Path(root, path).read_bytes() + b"\0")
    return h.hexdigest()


def _record(ctx, job_id):
    try:
        return ctx.store.job_record(ctx.job["tenant_id"], ctx.job["workspace_id"], job_id)
    except TenantScopeError:
        return None


def _completed(ctx, job_id, kind, code):
    rec = _record(ctx, job_id)
    if rec is None or rec.get("kind") != kind or rec.get("execution_status") != "COMPLETED" or not rec.get("execution_result"):
        raise NonRetryableExecutionError(code, f"{kind} job {job_id} is not completed")
    return rec["execution_result"]


def _request():
    return ExecutionRequest(prompt=None, schema={"type": "object", "executor": "code_change"}, provider_calls_planned=0, external=False)


# ---- candidate (FEX-03) ----------------------------------------------------------------------------------------------------------------------------------------------------

class CodeCandidateExecutor:
    external = False
    executor_id = "code.candidate.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not str(p.get("change_id", "")).startswith("chg-") or not isinstance(p.get("repo_id"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return _request()

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        from infra.adapters import v2_business, v2_business_commit, v2_business_review
        from core.change_intelligence.v2 import code_recommendation
        tenant, ws, p = ctx.job["tenant_id"], ctx.job["workspace_id"], ctx.job["payload"]
        repo = repo_config(ws, p["repo_id"])
        committed = v2_business_commit.committed_state(ctx.store, tenant, ws, p["change_id"])
        if committed is None:
            raise NonRetryableExecutionError("knowledge_not_committed", "the business change is not committed; a patch implements decided knowledge only")
        got = v2_business_review.current_draft(ctx.store, tenant, ws, p["change_id"])
        if got is None:
            raise NonRetryableExecutionError("change_not_confirmed")
        draft, _w, parts = got
        head = ctx.store.head(tenant, ws)
        current = v2_business._working(v2_business.business_view(ctx.store, tenant, ws))
        rec = code_recommendation.build_recommendations(draft, parts["intent"]["operations"], head={"source_revision_id": head.source_revision_id, "snapshot_id": current["snapshot"]["snapshot_id"] if current else None})
        from infra.adapters import v2_code_change
        stale, reasons = v2_code_change.binding_moved(ctx.store, tenant, ws, p["change_id"], draft["pin"], head, current)
        if stale:
            raise NonRetryableExecutionError("stale_binding", "; ".join(reasons))
        checkout = clone(repo["path"])
        try:
            base_sha = _git(checkout, "rev-parse", "HEAD").strip()
            files = tracked_files(checkout)
            plan = code_patch.plan(rec["items"], {o["op_id"]: o for o in parts["intent"]["operations"]}, files, lambda rel: pathlib.Path(checkout, rel).read_text(encoding="utf-8"),
                                   allowlist=repo["allowlist"], protected=repo["protected"], extra_sites=p.get("extra_sites") or [],
                                   source_root=repo.get("source_root"))
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
        if plan["diff"] and len(plan["diff"].encode("utf-8")) > MAX_DIFF_BYTES:
            raise NonRetryableExecutionError("patch_too_large")
        target_pin = {"snapshot_id": committed["snapshot_id"], "business_revision": committed["business_revision"], "rule_ids": sorted({i["rule_id"] for i in plan["implemented"] if i.get("rule_id")}),
                      "semantic_diff_hash": committed["semantic_diff_hash"]}
        result = {"executor_id": self.executor_id, "change_id": p["change_id"], "repo_id": p["repo_id"], "base_sha": base_sha, "recommendation_hash": rec["recommendation_hash"],
                  "target_pin": target_pin, "patch_hash": plan["patch_hash"], "diff": plan["diff"], "files": plan["files"], "implemented": plan["implemented"],
                  "not_implemented": plan["not_implemented"], "status": "PROPOSED" if plan["diff"] else "NOTHING_TO_PATCH", "allowlist": repo["allowlist"], "applies_nothing": True,
                  "repair_of": p.get("parent_candidate_job_id"), "extra_sites": p.get("extra_sites") or [],
                  "statement": "A candidate patch for a person to validate and approve. It implements only the literal changes the recommendations support; everything else is listed as not implemented. Nothing was pushed, merged or deployed."}
        if p.get("ma_run_id") and p.get("ma_child_id"):                                    # AIX-C1: a repair candidate wakes the coordinator that asked for it (idempotent join job)
            from core.change_intelligence.v2 import ma_orchestration as ma
            from infra.worker.verification import ensure_job
            try:
                ensure_job(ctx.store, tenant, ws, ma.join_job_id(p["ma_run_id"], p["ma_child_id"]) + "-cand", "ma.join", {"run_id": p["ma_run_id"], "after": p["ma_child_id"]})
            except Exception:                                                               # noqa: BLE001 - the operator's advance route recovers a missing kick
                pass
        return ExecutionOutcome(result=result, model_id=None)


# ---- validation (FEX-04) ---------------------------------------------------------------------------------------------------------------------------------------------------

class CodeValidateExecutor:
    external = False
    executor_id = "code.validate.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        if not isinstance(ctx.job["payload"], dict) or not isinstance(ctx.job["payload"].get("candidate_job_id"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return _request()

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        cand = _completed(ctx, ctx.job["payload"]["candidate_job_id"], "code.candidate", "candidate_not_completed")
        if not cand.get("diff"):
            raise NonRetryableExecutionError("nothing_to_validate")
        repo = repo_config(ctx.job["workspace_id"], cand["repo_id"])
        checkout = clone(repo["path"], cand["base_sha"])
        try:
            patch_file = pathlib.Path(checkout, ".ripple-candidate.patch")
            patch_file.write_text(cand["diff"], encoding="utf-8", newline="\n")
            r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=checkout, capture_output=True, text=True, timeout=60)
            patch_file.unlink()
            if r.returncode != 0:
                raise NonRetryableExecutionError("patch_does_not_apply", r.stderr.strip()[:300])
            digest = runtime_digest(checkout)
            ran = run_commands(checkout, repo["validate"], timeout_seconds=int(repo.get("timeout_seconds", 180)), repo=repo) if repo["validate"] else {"status": "NOT_RUN", "commands": []}
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
        report = {"patch_hash": cand["patch_hash"], "base_sha": cand["base_sha"], "runtime_digest": digest, "status": ran["status"], "commands": ran["commands"], "isolation": isolation_for(repo), "runtime": ran.get("runtime", {"kind": "subprocess"})}
        report["validation_hash"] = canonical_hash({k: report[k] for k in ("patch_hash", "base_sha", "runtime_digest", "status")} | {"commands": [{"argv": c["argv"], "exit": c["exit"]} for c in report["commands"]]})
        result = {"executor_id": self.executor_id, "candidate_job_id": ctx.job["payload"]["candidate_job_id"], **report, "applies_nothing": True,
                  "statement": ("The declared commands ran on a fresh checkout with the patch applied; PASSED means every required command exited 0. " if report["status"] == "PASSED" else
                                "The declared commands did not all pass; the candidate cannot be approved. " if report["status"] == "FAILED" else
                                "No declared command ran; a report without commands is not a pass. ") + "This is not proof of business correctness."}
        return ExecutionOutcome(result=result, model_id=None)


# ---- mock promotion, verification, rollback (FEX-06..08) --------------------------------------------------------------------------------------------------------------------

def _mock_target(repo: dict, environment_id: str) -> pathlib.Path:
    if os.environ.get("RIPPLE_EXTENSION_MOCK_TARGET") != "1" or not repo.get("mock_target"):
        raise NonRetryableExecutionError("no_deploy_grant", "no deployment grant exists; only the mock target of a declared repository is allowed, and it is off")
    if environment_id not in (repo.get("environments") or []):
        raise NonRetryableExecutionError("wrong_environment", f"{environment_id!r} is not a declared environment of this repository")
    return pathlib.Path(repo["mock_target"]) / environment_id


def _ledger_approval(ctx, cand_job_id, patch_hash):
    from infra.adapters import v2_code_change
    return v2_code_change.current_approval(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], cand_job_id, patch_hash)


class CodePromoteExecutor:
    external = False
    executor_id = "code.promote.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("candidate_job_id"), str) or not isinstance(p.get("environment_id"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return _request()

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        p = ctx.job["payload"]
        cand = _completed(ctx, p["candidate_job_id"], "code.candidate", "candidate_not_completed")
        val = _completed(ctx, validation_job_id(p["candidate_job_id"]), "code.validate", "validation_not_completed")
        if val["status"] != "PASSED" or val["patch_hash"] != cand["patch_hash"]:
            raise NonRetryableExecutionError("validation_not_passed")
        approval = _ledger_approval(ctx, p["candidate_job_id"], cand["patch_hash"])
        if approval is None or approval["validation_hash"] != val["validation_hash"]:
            raise NonRetryableExecutionError("not_approved", "no current approval binds this patch and this validation")
        repo = repo_config(ctx.job["workspace_id"], cand["repo_id"])
        target = _mock_target(repo, p["environment_id"])
        checkout = clone(repo["path"], cand["base_sha"])
        try:
            patch_file = pathlib.Path(checkout, ".ripple-candidate.patch")
            patch_file.write_text(cand["diff"], encoding="utf-8", newline="\n")
            r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=checkout, capture_output=True, text=True, timeout=60)
            patch_file.unlink()
            if r.returncode != 0:
                raise NonRetryableExecutionError("patch_does_not_apply", r.stderr.strip()[:300])
            _git(checkout, "checkout", "--quiet", "-B", f"ripple/{cand['change_id']}")
            _git(checkout, "-c", "user.name=ripple-extension", "-c", "user.email=ripple-extension@localhost", "commit", "--quiet", "-am", f"Implement {cand['change_id']} (patch {cand['patch_hash'][:12]})")
            commit_sha = _git(checkout, "rev-parse", "HEAD").strip()
            artifact = tree_hash(checkout)
            target.mkdir(parents=True, exist_ok=True)
            current, previous = target / "current", target / "previous"
            if previous.exists():
                shutil.rmtree(previous)
            if current.exists():
                current.rename(previous)
            shutil.copytree(checkout, current, ignore=shutil.ignore_patterns(".git"))
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
        from infra.adapters import v2_code_change
        v2_code_change.ledger(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], cand["change_id"], p["candidate_job_id"], kind="promotion", actor_id="worker",
                              reason="mock promotion of the approved, validated artifact", patch_hash=cand["patch_hash"], validation_hash=val["validation_hash"],
                              body={"environment_id": p["environment_id"], "artifact_hash": artifact, "commit_sha": commit_sha, "mock": True})
        result = {"executor_id": self.executor_id, **{k: p[k] for k in ("candidate_job_id", "environment_id")}, "status": "PROMOTED", "artifact_hash": artifact, "commit_sha": commit_sha,
                  "mock": True, "statement": "Promoted into a LOCAL MOCK target only (a copy of the committed tree). No real environment was touched; the business change is not implemented until verification and read-back say so."}
        return ExecutionOutcome(result=result, model_id=None)


class CodeVerifyExecutor:
    external = False
    executor_id = "code.verify.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("candidate_job_id"), str) or not isinstance(p.get("environment_id"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return _request()

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        p = ctx.job["payload"]
        cand = _completed(ctx, p["candidate_job_id"], "code.candidate", "candidate_not_completed")
        prom = _completed(ctx, promotion_job_id(p["candidate_job_id"], p["environment_id"]), "code.promote", "promotion_not_completed")
        repo = repo_config(ctx.job["workspace_id"], cand["repo_id"])
        current = _mock_target(repo, p["environment_id"]) / "current"
        if not current.is_dir():
            raise NonRetryableExecutionError("artifact_missing", "the mock target holds no current artifact")
        deployed = tree_hash(str(current))
        if deployed != prom["artifact_hash"]:
            raise NonRetryableExecutionError("artifact_mismatch", "the mock target does not hold the promoted artifact")
        ran = run_commands(str(current), repo["verify"], timeout_seconds=int(repo.get("timeout_seconds", 180)), repo=repo) if repo["verify"] else {"status": "NOT_RUN", "commands": []}
        status = {"PASSED": "VERIFIED", "FAILED": "FAILED", "NOT_RUN": "UNVERIFIABLE", "INFRASTRUCTURE_FAILED": "UNVERIFIABLE"}[ran["status"]]
        from infra.adapters import v2_code_change
        v2_code_change.ledger(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], cand["change_id"], p["candidate_job_id"], kind="verification", actor_id="worker",
                              reason="declared scenario checks against the mock target", patch_hash=cand["patch_hash"], validation_hash=None,
                              body={"environment_id": p["environment_id"], "artifact_hash": deployed, "status": status, "commands": [{"argv": c["argv"], "exit": c["exit"]} for c in ran["commands"]]})
        result = {"executor_id": self.executor_id, **{k: p[k] for k in ("candidate_job_id", "environment_id")}, "artifact_hash": deployed, "status": status, "commands": ran["commands"],
                  "isolation": isolation_for(repo), "runtime": ran.get("runtime", {"kind": "subprocess"}), "statement": "Scenario checks on the MOCK target. VERIFIED means the declared checks passed there; the business change's behaviour axis is a separate record and its knowledge read-back is the Final loop's."}
        return ExecutionOutcome(result=result, model_id=None)


class CodeRollbackExecutor:
    external = False
    executor_id = "code.rollback.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("candidate_job_id"), str) or not isinstance(p.get("environment_id"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return _request()

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        p = ctx.job["payload"]
        cand = _completed(ctx, p["candidate_job_id"], "code.candidate", "candidate_not_completed")
        repo = repo_config(ctx.job["workspace_id"], cand["repo_id"])
        target = _mock_target(repo, p["environment_id"])
        current, previous = target / "current", target / "previous"
        if not previous.is_dir():
            raise NonRetryableExecutionError("nothing_to_roll_back", "the mock target holds no previous artifact")
        restored = tree_hash(str(previous))
        if current.exists():
            shutil.rmtree(current)
        previous.rename(current)
        from infra.adapters import v2_code_change
        v2_code_change.ledger(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], cand["change_id"], p["candidate_job_id"], kind="rollback", actor_id="worker",
                              reason="previous artifact restored in the mock target", patch_hash=cand["patch_hash"], validation_hash=None,
                              body={"environment_id": p["environment_id"], "restored_artifact_hash": restored, "mock": True})
        result = {"executor_id": self.executor_id, **{k: p[k] for k in ("candidate_job_id", "environment_id")}, "status": "ROLLED_BACK", "restored_artifact_hash": restored, "mock": True,
                  "statement": "The previous artifact is back in the MOCK target. Approvals and knowledge are untouched; the verified state of the target is unknown until the checks run again."}
        return ExecutionOutcome(result=result, model_id=None)
