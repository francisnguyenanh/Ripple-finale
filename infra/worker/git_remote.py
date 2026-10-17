"""Extension contract ext/1, FEX-01 (remote read) and FEX-05 (pull request): the product-side Git connector for ONE declared GitHub repository per repository entry.

Configuration (operator's `RIPPLE_EXTENSION_CONFIG`, per workspace repository):
    "remote": {"provider": "github", "repository": "owner/name", "branch": "main", "grants": ["read", "pull_request"], "token_env": "RIPPLE_GIT_TOKEN"}
`path` is then a mirror directory MANAGED by the product (created on first fetch; never edited by hand). Grants are explicit and never inferred: `read` lets `git.fetch` bring the
allowlisted branch into the mirror (the existing local-only `git.sync` then reads it); `pull_request` lets an approved, validated candidate become ONE branch + commit + pull request.
`merge`, `admin` or any other grant is refused; nothing here merges, deletes a branch, changes branch protection or creates a workflow. The token is read from the named environment
variable at call time, sent only to github.com (git over HTTPS via an environment-provided header, the REST API via the Authorization header), never placed in argv, results, logs or
knowledge. A local filesystem path in `url` replaces github.com for tests (a bare repository); any other URL override is refused.

Failure reasons (job FAILED, the next request or the scheduler retries): remote_auth_refused (401/403, revoked token), remote_not_found (wrong repository or no access),
remote_branch_missing (deleted branch), remote_unreachable (network, 5xx, rate limit), stale_base (the candidate's base is not on the branch any more), branch_conflict (the
candidate branch exists with other content), remote_token_missing, no_read_grant / no_publish_grant.
"""
from __future__ import annotations

import base64
import os
import pathlib
import re
import shutil
import subprocess

from core.change_intelligence.v2.serialize import canonical_hash
from infra.worker import code_change as cc
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget

FETCH_KIND = "git.fetch"
PUBLISH_KIND = "code.publish"
GRANTS = frozenset({"read", "pull_request"})
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
API_BASE = "https://api.github.com"
API_FACTORY = None                                   # tests replace the transport (a fake GitHub); the product uses `GitHubApi`


def has_remote(repo: dict) -> bool:
    return isinstance(repo.get("remote"), dict)


def remote_spec(repo: dict) -> dict:
    r = repo.get("remote")
    if not isinstance(r, dict):
        raise NonRetryableExecutionError("no_remote", "this repository has no declared remote")
    if r.get("provider") != "github":
        raise NonRetryableExecutionError("remote_config_invalid", "only provider github is supported")
    if not isinstance(r.get("repository"), str) or not _REPO.match(r["repository"]):
        raise NonRetryableExecutionError("remote_config_invalid", "repository must be owner/name")
    branch = r.get("branch", "main")
    if not isinstance(branch, str) or not _BRANCH.match(branch) or ".." in branch or branch.startswith("/"):
        raise NonRetryableExecutionError("remote_config_invalid", "branch is not a plain branch name")
    grants = r.get("grants") or []
    if not isinstance(grants, list) or not set(grants) <= GRANTS:
        raise NonRetryableExecutionError("remote_config_invalid", f"grants must be a subset of {sorted(GRANTS)} (merge or admin rights are never used)")
    token_env = r.get("token_env", "RIPPLE_GIT_TOKEN")
    if not isinstance(token_env, str) or not _ENV.match(token_env):
        raise NonRetryableExecutionError("remote_config_invalid", "token_env must name an environment variable")
    url = r.get("url")
    if url is not None:
        if not isinstance(url, str) or "://" in url or url.startswith(("git@", "-")) or not os.path.isdir(url):
            raise NonRetryableExecutionError("remote_config_invalid", "a url override must be an existing local repository path (tests only)")
    else:
        url = f"https://github.com/{r['repository']}.git"
    return {"repository": r["repository"], "branch": branch, "grants": sorted(set(grants)), "token_env": token_env, "url": url, "https": url.startswith("https://")}


def has_grant(repo: dict, grant: str) -> bool:
    return has_remote(repo) and grant in remote_spec(repo)["grants"]


def _token(spec: dict) -> str:
    token = os.environ.get(spec["token_env"], "")
    if not token:
        raise NonRetryableExecutionError("remote_token_missing", f"the environment variable {spec['token_env']} is not set")
    return token


def _scrub(text: str, secrets: list) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text[:300]


# ---- FEX-01: fetch the allowlisted branch into the managed mirror ------------------------------------------------------------------------------------------------------------

def _git_env(spec: dict) -> tuple:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="")
    secrets = []
    if spec["https"]:
        token = _token(spec)
        header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0=header)
        secrets = [token, header.split(" ", 2)[-1]]
    return env, secrets


def _classify(stderr: str) -> str:
    s = stderr.lower()
    if "couldn't find remote ref" in s or "could not find remote ref" in s:
        return "remote_branch_missing"
    if "repository not found" in s or "does not appear to be a git repository" in s or "not a git repository" in s:
        return "remote_not_found"
    if "authentication failed" in s or "could not read username" in s or "403" in s or "401" in s or "permission to" in s:
        return "remote_auth_refused"
    if "could not resolve host" in s or "failed to connect" in s or "timed out" in s or "unable to access" in s or "connection" in s:
        return "remote_unreachable"
    return "remote_fetch_failed"


def _run(args, cwd, env, secrets, timeout=300):
    try:
        r = subprocess.run(["git", "-c", "credential.helper=", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NonRetryableExecutionError("remote_unreachable", "git timed out") from None
    if r.returncode != 0:
        raise NonRetryableExecutionError(_classify(r.stderr), _scrub(r.stderr.strip(), secrets))
    return r.stdout


def fetch(repo: dict) -> str:
    """Bring the allowlisted branch of the declared remote into the managed mirror at `repo["path"]` and check it out (detached). Returns the commit sha."""
    spec = remote_spec(repo)
    if "read" not in spec["grants"]:
        raise NonRetryableExecutionError("no_read_grant", "the repository's remote has no read grant")
    mirror = pathlib.Path(repo["path"])
    env, secrets = _git_env(spec)
    if not (mirror / ".git").is_dir():
        if mirror.exists() and any(mirror.iterdir()):
            raise NonRetryableExecutionError("mirror_not_managed", "the mirror path holds other content; give the product an empty or new directory")
        mirror.mkdir(parents=True, exist_ok=True)
        _run(["init", "-q"], mirror, env, secrets)
        _run(["remote", "add", "origin", spec["url"]], mirror, env, secrets)
    elif _run(["remote", "get-url", "origin"], mirror, env, secrets).strip() != spec["url"]:
        raise NonRetryableExecutionError("mirror_not_managed", "the mirror points at another remote")
    b = spec["branch"]
    _run(["fetch", "-q", "--no-tags", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}"], mirror, env, secrets)
    _run(["checkout", "-q", "--detach", "--force", f"refs/remotes/origin/{b}"], mirror, env, secrets)
    _run(["update-ref", "refs/heads/ripple-mirror", "HEAD"], mirror, env, secrets)      # a local ref, so a clone of the mirror carries the commit
    return _run(["rev-parse", "HEAD"], mirror, env, secrets).strip()


def fetch_job_id(repo_id: str, source_revision_id: str, bucket: str) -> str:
    return "gfetch-" + canonical_hash({"repo": repo_id, "rev": source_revision_id, "bucket": bucket})[:20]


class GitFetchExecutor:
    external = False
    executor_id = "git.fetch.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("repo_id"), str) or not isinstance(p.get("requested_by"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return ExecutionRequest(prompt=None, schema={"type": "object", "executor": self.executor_id}, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        from infra.adapters import v2_business
        from infra.worker import git_sync as gs
        from infra.worker.verification import ensure_job
        store, tenant, ws, p = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], ctx.job["payload"]
        if "source.import" not in v2_business.current_permissions(store, tenant, ws, p["requested_by"]):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds source.import")
        repo = cc.repo_config(ws, p["repo_id"])
        spec = remote_spec(repo)
        sha = fetch(repo)
        head = store.head(tenant, ws)
        sync_id = gs.job_id_for(p["repo_id"], sha, head.source_revision_id)
        created = ensure_job(store, tenant, ws, sync_id, gs.KIND, {"repo_id": p["repo_id"], "requested_by": p["requested_by"], "sha": sha})
        return ExecutionOutcome(result={"executor_id": self.executor_id, "repo_id": p["repo_id"], "repository": spec["repository"], "branch": spec["branch"], "sha": sha,
                                        "sync_job_id": sync_id, "sync_created": created, "applies_nothing": True,
                                        "statement": "The declared branch was fetched into the product's mirror; the sync job reads it as data into one source revision."}, model_id=None)


# ---- FEX-05: one branch + commit + pull request through the GitHub REST API ---------------------------------------------------------------------------------------------------

class GitHubApi:
    """The real transport: GitHub REST v3 with the token from the declared environment variable. `call` -> (status, json)."""

    def __init__(self, spec: dict, timeout: float = 20.0):
        import requests
        self._token = _token(spec)
        self._http = requests.Session()
        self._http.trust_env = False
        self._timeout = timeout

    def call(self, method: str, path: str, body=None):
        import requests
        try:
            r = self._http.request(method, API_BASE + path, json=body, timeout=self._timeout,
                                   headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        except requests.RequestException as exc:
            raise NonRetryableExecutionError("remote_unreachable", type(exc).__name__) from None
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        return r.status_code, data


def api_for(spec: dict):
    return (API_FACTORY or GitHubApi)(spec)


def _expect(status, data, ok=(200, 201)):
    if status in ok:
        return data
    msg = str((data or {}).get("message", "")) if isinstance(data, dict) else ""
    if status in (401,) or (status == 403 and "rate limit" not in msg.lower()):
        raise NonRetryableExecutionError("remote_auth_refused", f"HTTP {status}")
    if status == 404:
        raise NonRetryableExecutionError("remote_not_found", "HTTP 404")
    if status in (403, 429) or status >= 500:
        raise NonRetryableExecutionError("remote_unreachable", f"HTTP {status}")
    raise NonRetryableExecutionError("remote_rejected", f"HTTP {status}: {msg[:120]}")


def publish_pr(api, spec: dict, *, base_sha: str, files: dict, branch: str, title: str, body: str, message: str) -> dict:
    """Idempotent: the same patch on the same base gives the same tree; an existing branch with that tree and parent, and an existing pull request for that branch, are reused."""
    repo, target = spec["repository"], spec["branch"]
    st, cmp_ = api.call("GET", f"/repos/{repo}/compare/{base_sha}...{target}")
    if st == 404 or (st == 200 and cmp_.get("status") not in ("ahead", "identical")):
        raise NonRetryableExecutionError("stale_base", f"the candidate's base {base_sha[:12]} is not on {target} any more; build a new candidate")
    _expect(st, cmp_)
    base_tree = _expect(*api.call("GET", f"/repos/{repo}/git/commits/{base_sha}"))["tree"]["sha"]
    entries = []
    for path in sorted(files):
        data = files[path]
        if data is None:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        else:
            blob = _expect(*api.call("POST", f"/repos/{repo}/git/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}))
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = _expect(*api.call("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": entries}))["sha"]
    st, ref = api.call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    reused_branch = st == 200
    if reused_branch:
        head = _expect(*api.call("GET", f"/repos/{repo}/git/commits/{ref['object']['sha']}"))
        if head["tree"]["sha"] != tree or [x["sha"] for x in head.get("parents") or []] != [base_sha]:
            raise NonRetryableExecutionError("branch_conflict", f"branch {branch} exists with other content; nothing was overwritten")
        commit_sha = ref["object"]["sha"]
    else:
        if st != 404:
            _expect(st, ref)
        commit_sha = _expect(*api.call("POST", f"/repos/{repo}/git/commits", {"message": message, "tree": tree, "parents": [base_sha]}))["sha"]
        _expect(*api.call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit_sha}))
    owner = repo.split("/")[0]
    prs = _expect(*api.call("GET", f"/repos/{repo}/pulls?head={owner}:{branch}&state=all"))
    reused_pr = bool(prs)
    pr = prs[0] if prs else _expect(*api.call("POST", f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": target, "body": body, "maintainer_can_modify": False}))
    return {"repository": repo, "base_branch": target, "branch": branch, "base_sha": base_sha, "commit_sha": commit_sha, "tree_sha": tree, "pr_number": pr.get("number"),
            "pr_url": pr.get("html_url"), "pr_state": pr.get("state"), "merged": bool(pr.get("merged_at")), "reused_branch": reused_branch, "reused_pr": reused_pr}


def publish_job_id(candidate_job_id: str, validation_hash: str) -> str:
    return "cpub-" + canonical_hash({"candidate": candidate_job_id, "validation": validation_hash})[:20]


def _patched_files(repo_path: str, base_sha: str, diff: str) -> dict:
    checkout = cc.clone(repo_path, base_sha)
    try:
        patch_file = pathlib.Path(checkout, ".ripple-candidate.patch")
        patch_file.write_text(diff, encoding="utf-8", newline="\n")
        r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=checkout, capture_output=True, text=True, timeout=60)
        patch_file.unlink()
        if r.returncode != 0:
            raise NonRetryableExecutionError("patch_does_not_apply", r.stderr.strip()[:300])
        cc._git(checkout, "add", "-A")
        out = {}
        for line in cc._git(checkout, "diff", "--cached", "--name-status", "--no-renames").splitlines():
            status, rel = line.split("\t", 1)
            out[rel] = None if status.startswith("D") else pathlib.Path(checkout, rel).read_bytes()
        return out
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


class CodePublishExecutor:
    external = False
    executor_id = "code.publish.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("candidate_job_id"), str) or not isinstance(p.get("requested_by"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return ExecutionRequest(prompt=None, schema={"type": "object", "executor": self.executor_id}, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        from infra.adapters import v2_business, v2_business_commit, v2_code_change
        store, tenant, ws, p = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], ctx.job["payload"]
        cand_id = p["candidate_job_id"]
        cand = cc._completed(ctx, cand_id, "code.candidate", "candidate_not_completed")
        val = cc._completed(ctx, cc.validation_job_id(cand_id), "code.validate", "validation_not_completed")
        if val["status"] != "PASSED" or val["patch_hash"] != cand["patch_hash"]:
            raise NonRetryableExecutionError("validation_not_passed")
        approval = v2_code_change.current_approval(store, tenant, ws, cand_id, cand["patch_hash"])
        if approval is None or approval["validation_hash"] != val["validation_hash"]:
            raise NonRetryableExecutionError("not_approved", "no current approval binds this patch and validation (a later rejection or a new validation revokes it)")
        committed = v2_business_commit.committed_state(store, tenant, ws, cand["change_id"])
        if committed is None or committed["snapshot_id"] != cand["target_pin"]["snapshot_id"]:
            raise NonRetryableExecutionError("knowledge_moved", "the decided knowledge the patch implements is no longer the committed one")
        if "code.approve" not in v2_business.current_permissions(store, tenant, ws, p["requested_by"]):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds code.approve")
        repo = cc.repo_config(ws, cand["repo_id"])
        spec = remote_spec(repo)
        if "pull_request" not in spec["grants"]:
            raise NonRetryableExecutionError("no_publish_grant", "the repository's remote has no pull_request grant")
        files = _patched_files(repo["path"], cand["base_sha"], cand["diff"])
        if not files:
            raise NonRetryableExecutionError("nothing_to_publish")
        branch = f"ripple/{cand['change_id']}-{cand['patch_hash'][:12]}"
        title = f"Implement business change {cand['change_id']}"
        body = (f"Candidate patch for the committed business change `{cand['change_id']}` (knowledge snapshot `{cand['target_pin']['snapshot_id']}`).\n\n"
                f"- patch hash: `{cand['patch_hash']}`\n- validation hash: `{val['validation_hash']}` ({val['status']})\n- approved by: a distinct approver (`{approval['actor_id']}`)\n\n"
                "Opened by Ripple after approval. Ripple never merges: review and merge follow this repository's own rules.")
        result = publish_pr(api_for(spec), spec, base_sha=cand["base_sha"], files=files, branch=branch, title=title, body=body,
                            message=f"{title}\n\npatch {cand['patch_hash'][:12]}, validation {val['validation_hash'][:12]}")
        rows = [r for r in v2_code_change.ledger_rows(store, tenant, ws, cand_id) if r["kind"] == "pr_published" and r["validation_hash"] == val["validation_hash"]]
        if not rows:
            v2_code_change.ledger(store, tenant, ws, cand["change_id"], cand_id, kind="pr_published", actor_id=p["requested_by"],
                                  reason="pull request opened after approval (no merge)", patch_hash=cand["patch_hash"], validation_hash=val["validation_hash"], body=result)
        return ExecutionOutcome(result={"executor_id": self.executor_id, "candidate_job_id": cand_id, **result, "applies_nothing": False,
                                        "statement": "One branch, one commit and one pull request were created on the declared repository; nothing was merged."}, model_id=None)
