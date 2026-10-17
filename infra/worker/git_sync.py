"""Extension contract ext/1, FEX-01/FEX-02 LOCAL ONLY: read a declared LOCAL git repository at its current commit and bring its allowlisted files into the workspace as one source
refresh revision (the existing refresh path: admission, staged immutable blobs, prepared refresh, CAS commit, read-back of observations), so the Final carry-over (SYN-02) can compare
the decided knowledge with what the repository now holds. No network, no webhook, no remote: the repository path comes from the operator's `RIPPLE_EXTENSION_CONFIG`
and in this build is always a fixture repository created by the tests. Source files parse as data; nothing from the repository is executed.

job kind "git.sync", payload {repo_id, requested_by}; id derives from (repo, commit, current source revision) so the same commit is one job. Result records the commit, the previous
synced commit and whether the history is linear (`merge-base --is-ancestor`) or rewritten (force-push shape), the files added/changed/deleted/skipped, and the new revision.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

from core.change_intelligence.v2.serialize import canonical_hash
from core.change_intelligence.v2.source_admission import AdmissionRejected, admit_files
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.worker import code_change as cc
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget

KIND = "git.sync"


def head_sha(repo_path: str) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise NonRetryableExecutionError("git_failed", r.stderr.strip()[:200])
    return r.stdout.strip()


def job_id_for(repo_id: str, sha: str, source_revision_id: str) -> str:
    return "gsync-" + canonical_hash({"repo": repo_id, "sha": sha, "rev": source_revision_id})[:20]


def last_sync(store, tenant_id, workspace_id, repo_id):
    """The newest completed sync of this repository (its commit), or None."""
    from infra.adapters.v2_restore import read_snapshot
    with read_snapshot(store) as cur:
        cur.execute("""SELECT j.execution_result FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='git.sync' AND j.execution_status='COMPLETED'
                       AND j.payload->>'repo_id'=%s ORDER BY (SELECT max(o.id) FROM v2_outbox o WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id AND o.payload->>'job_id'=j.job_id) DESC NULLS LAST LIMIT 1""",
                    (tenant_id, workspace_id, repo_id))
        row = cur.fetchone()
    if row is None:
        return None
    res = row[0]
    return res if isinstance(res, dict) else __import__("json").loads(res)


class GitSyncExecutor:
    external = False
    executor_id = "git.sync.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("repo_id"), str) or not isinstance(p.get("requested_by"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return ExecutionRequest(prompt=None, schema={"type": "object", "executor": self.executor_id}, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        from infra.adapters import v2_business
        from infra.worker.business_source_apply import read_back
        store, tenant, ws, p = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], ctx.job["payload"]
        repo = cc.repo_config(ws, p["repo_id"])
        mount = (repo.get("mount") or "").strip("/")
        mount = mount + "/" if mount else ""
        if "source.import" not in v2_business.current_permissions(store, tenant, ws, p["requested_by"]):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds source.import")
        previous = last_sync(store, tenant, ws, p["repo_id"])
        checkout = cc.clone(repo["path"], p.get("sha"))                                   # the commit the job was created for (its id derives from it)
        try:
            sha = cc._git(checkout, "rev-parse", "HEAD").strip()
            history = "first_sync"
            if previous and previous.get("sha") and previous["sha"] != sha:
                r = subprocess.run(["git", "merge-base", "--is-ancestor", previous["sha"], sha], cwd=checkout, capture_output=True, text=True, timeout=30)
                history = "linear" if r.returncode == 0 else "rewritten"                   # a rewritten history (force-push shape) is labelled, never hidden
            elif previous and previous.get("sha") == sha:
                history = "unchanged"
            entries, skipped = [], []
            for rel in cc.tracked_files(checkout):
                if code_patch_allowed(rel, repo) is not None:
                    skipped.append({"path": rel, "reason": code_patch_allowed(rel, repo)})
                    continue
                entries.append((mount + rel, pathlib.Path(checkout, rel).read_bytes(), None))
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
        head = store.head(tenant, ws)
        if previous and previous.get("sha") == sha and previous.get("source_revision_id") == head.source_revision_id:
            return ExecutionOutcome(result={"executor_id": self.executor_id, "repo_id": p["repo_id"], "sha": sha, "previous_sha": previous.get("sha"), "history": history, "status": "NO_OP",
                                            "source_revision_id": head.source_revision_id, "statement": "The repository commit is the one already synced into the current source revision."}, model_id=None)
        # a refresh is the WHOLE snapshot: every current artifact outside the repository's mount is carried as it is
        view = store.source_revision_view(tenant, ws, head.source_revision_id)
        repo_paths = {e[0] for e in entries}
        owned = [mount + a.strip("/") + "/" for a in repo["allowlist"]] or [mount]          # the repository owns its mount + allowlisted prefixes: a file missing there is a deletion
        for art in view["artifacts"]:
            path = art.get("logical_path") or art["artifact_id"]
            if path in repo_paths or any(path.startswith(o) for o in owned if o):
                continue                                                  # the rest of the current revision is carried unchanged
            entries.append((path, store.read_blob(tenant, ws, head.source_revision_id, art["artifact_id"]), None))
        try:
            report = admit_files(entries)
        except AdmissionRejected as exc:
            raise NonRetryableExecutionError("admission_rejected", str(exc)) from exc
        for item in report.items:
            if item.disposition != "admitted":
                skipped.append({"path": item.logical_path, "reason": item.reason or item.disposition})
        admitted = [i for i in report.items if i.disposition == "admitted"]
        if not admitted:
            raise NonRetryableExecutionError("nothing_admitted", "no repository file passed admission")
        for item in admitted:
            store.stage_blob(item.content)
        try:
            prepared = store.prepare_source_refresh(tenant, ws, actor_id=p["requested_by"], idempotency_key=f"git-sync:{p['repo_id']}:{sha}:{head.source_revision_id}",
                                                    items=[i.to_dict() for i in admitted], rename_proofs=None, language=None)
        except (StoreConflict, ValueError) as exc:
            raise NonRetryableExecutionError("refresh_prepare_failed", str(exc)[:300]) from exc
        diff = prepared.get("diff") or {}
        deleted = [d if isinstance(d, str) else (d.get("logical_path") or d.get("path")) for d in diff.get("deleted") or []]
        if prepared.get("noop"):
            return ExecutionOutcome(result={"executor_id": self.executor_id, "repo_id": p["repo_id"], "sha": sha, "previous_sha": (previous or {}).get("sha"), "history": history, "status": "NO_OP",
                                            "source_revision_id": head.source_revision_id, "skipped": skipped, "statement": "The repository holds the same bytes as the current source revision."}, model_id=None)
        try:
            committed = store.commit_source_refresh(tenant, ws, prepared["refresh_id"], actor_id=p["requested_by"], expected_version=prepared["base"]["version"],
                                                    expected_refresh_digest=prepared["refresh_digest"], acknowledged_deletions=[d for d in deleted if d], required_permission="source.import")
        except StaleVersion as exc:
            raise NonRetryableExecutionError("stale_head", str(exc)) from exc
        except StoreConflict as exc:
            raise NonRetryableExecutionError("authority_or_conflict", str(exc)[:300]) from exc
        new_rev = committed["source_revision_id"]
        rb = read_back(store, tenant, ws, new_rev)
        result = {"executor_id": self.executor_id, "repo_id": p["repo_id"], "sha": sha, "previous_sha": (previous or {}).get("sha"), "history": history, "status": "SYNCED",
                  "source_revision_id": new_rev, "previous_source_revision_id": head.source_revision_id, "mount": mount,
                  "files": {k: [x if isinstance(x, str) else (x.get("logical_path") or x.get("path")) for x in (diff.get(k) or [])] for k in ("added", "changed", "deleted", "renamed")},
                  "skipped": skipped, "read_back": rb, "applies_nothing": True,
                  "statement": "Repository files were read as data into one source revision; the decided knowledge is untouched. Run the analysis to compare it with the new sources (the carry-over reports verified, divergent or unverifiable per rule)."}
        return ExecutionOutcome(result=result, model_id=None)


def code_patch_allowed(rel: str, repo: dict):
    from core.change_intelligence.v2 import code_patch
    return code_patch.path_allowed(rel, repo["allowlist"], repo["protected"])
