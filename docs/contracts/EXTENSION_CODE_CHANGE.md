# Extension contract `ext/1` — from decided knowledge to a reviewed code change (FEX-00)

Status: contract for the **extension** packages, implemented locally by `core/change_intelligence/v2/code_patch.py`, `infra/worker/code_change.py`, `infra/adapters/v2_code_change.py`, `infra/v2_code_change_routes.py` (tests: `tests/final_loop/test_fex_code_change.py`); it is the contract (Git read, candidate patch, isolated validation, exact approval, PR intent, mock promotion, runtime verification,
recovery). It sits on top of the Final contract [`FINAL_KNOWLEDGE_LOOP.md`](FINAL_KNOWLEDGE_LOOP.md) and changes nothing in it. Everything here is **local-only in this
build**: the implementing code works on a disposable fixture repository created by the tests and on mock targets; no real repository, remote, registry, environment or
deployment is touched. A step that would act on a real external system is refused unless an explicit per-action grant exists, and no such grant exists in this build.
Nothing in this contract is a Final release criterion.

## 1. Principles

1. **The decided knowledge is the input, never the output.** A patch implements a TARGET rule of an accepted business snapshot (pinned). A patch never changes knowledge; the
   read-back of the sources after a merge is what the Final loop already does (`business_rebase`).
2. **Four separate things, four separate records:** a candidate patch (bytes + identities), its validation (a report bound to patch, base and runtime), its approval (a human
   decision bound to the patch and validation hashes) and its promotion (a PR intent, then a deployment record). None implies the next.
3. **Identity before action.** Every record carries `{repo_id, base_sha, target_pin, patch_hash, validation_hash, runtime_digest, environment_id}` as applicable; a changed
   identity makes every later record stale (`stale_binding`) and nothing acts on it.
4. **Per-action permissions, never a general grant.** `code.read`, `code.patch`, `code.validate`, `code.approve`, `code.publish` (push/PR), `code.merge`, `code.deploy`,
   `code.rollback` are distinct capabilities. A role holding one does not hold the next. A requester never approves their own patch (`distinct_approver`).
5. **Untrusted code runs nowhere that matters.** Validation runs in a throwaway clone with the repository's own declared tooling as timed subprocesses; this build has **no
   OS-level sandbox** by default (advisory isolation): every report says so and hostile code is out of scope. A declared repository may ask for the **container runtime**
   (`"runtime": {"kind": "container", "image": "python:3.12-slim", "memory", "cpus", "pids", "tmp_mb"}`, `infra/worker/sandbox_container.py`): each command then runs in its own
   container with no network, a read-only root and a READ-ONLY checkout, a tmpfs `/tmp`, memory/CPU/PID limits, an unprivileged numeric user, all capabilities dropped,
   `no-new-privileges`, no host environment and no engine socket; pytest is vendored read-only from the worker's own installation (nothing is pulled); an overrun is killed by
   container name; a missing engine or image is `INFRASTRUCTURE_FAILED`. Reports carry `isolation` and `runtime` (image id, limits). A container shares the host kernel: it is
   not the VM-class, verified boundary the plan requires before arbitrary or hostile repositories run.
6. **Fail closed, say why.** A missing mapping, an unresolved blocking gap, a base mismatch, a validation that did not run, an approval of a different hash, a revoked grant: each
   refuses with a code and leaves the previous records untouched.

## 2. Identities

| Identity | Meaning | Where it comes from |
|---|---|---|
| `repo_id` | the repository the patch targets, declared in the workspace's extension config; in this build always a fixture repository under a test temp directory | config |
| `base_sha` | the commit the candidate was built on | `git rev-parse HEAD` of the isolated checkout |
| `target_pin` | the Final snapshot pin plus the TARGET rule ids the patch implements (`{snapshot_id, business_revision, rule_ids}`) | the committed business change |
| `patch_hash` | sha256 of the unified diff bytes (LF-normalised) | computed once, never recomputed from a re-render |
| `validation_hash` | sha256 of the canonical validation report (`patch_hash`, `base_sha`, `runtime_digest`, command list, results) | validation job |
| `runtime_digest` | the toolchain identity used for validation (interpreter version string + lock-file hash when present) | validation job |
| `environment_id` | the deployment target; in this build a mock target directory | config |

## 3. Records and states

```
candidate_patch     : PROPOSED -> VALIDATED | VALIDATION_FAILED | STALE | WITHDRAWN
validation_report   : bound to (patch_hash, base_sha, runtime_digest); status PASSED | FAILED | INFRASTRUCTURE_FAILED | NOT_RUN
approval            : human decision {kind: code_patch, action: approve|reject, patch_hash, validation_hash, approver_id, reason}; stale when either hash moves
pr_intent           : RECORDED | REFUSED (no pull_request grant declared for the repository)
pr_published        : one branch + commit + pull request on the declared remote (code.publish job; never a merge); at most one per candidate + validation
deployment          : PLANNED -> PROMOTED (mock) -> VERIFYING -> VERIFIED | FAILED | ROLLED_BACK ; a deployment never closes a business change
```

A candidate patch is produced only from a committed business change whose code recommendations (`GET .../code`, SYN-01) have `binding_status = current`. Items `UNRESOLVED`
or `UNCERTAIN` are listed as **not implemented** in the candidate; they are never guessed.

## 4. Candidate patch (FEX-03)

- **Input:** committed change id, `repo_id`, allowed paths (allowlist), allowed operations (`replace_literal`, `replace_line`, `insert_after`), the SYN-01 recommendations.
- **Processing:** isolated checkout of `base_sha` into a disposable workspace; for each `SUPPORTED` recommendation with a bound literal, the literal is replaced on its line;
  a model may be asked (STUB in this build) for a bounded patch body for `UNCERTAIN` items, which the server accepts only if every touched path is in the allowlist, no path
  escapes the checkout (no `..`, no symlink target outside), no dependency, workflow, CI or secret file is touched, and the result applies cleanly to `base_sha`.
- **Output:** `{patch_hash, base_sha, target_pin, files[{path, before_hash, after_hash}], implemented_rule_ids, not_implemented[{ref, reason}], assumptions, tests_proposed[]}`
  plus the unified diff bytes as an immutable blob.
- **Refusals:** `repo_not_declared`, `base_mismatch`, `path_outside_allowlist`, `protected_file`, `mapping_unresolved`, `patch_does_not_apply`, `model_output_invalid`.

## 4a. Model-written code proposal, proposal-only (SUR-03, added 2026-10-19)
- `POST /v2/business/changes/{change_id}/code/proposal {workspace_id, repo_id, base?, attempt 1-3}` (human actor, `code.patch` + `ai.use` + `evidence.read`, change visibility,
  a visible declared repository; `base` defaults to the repository's HEAD and is resolved to a commit SHA now; 409 `not_confirmed`, `unknown_reference`, `provider_disarmed`,
  `budget_exceeded`); `GET .../code/proposal?repo_id=&base=` (`project.read` + `evidence.read`; reads only). One job (`code.diff_proposal`, pinned role of the same name) per
  change, repository, base commit, recommendation hash, attempt and frozen config + pinned model.
- Offered files: SUPPORTED / UNCERTAIN recommendation paths that resolve to exactly one tracked file at the base commit, inside the allowlist, not protected and not a test
  file; read with `git show <base>:<path>` (no clone, no write); the rest listed with the reason.
- The model explains the change and proposes `find` / `replace` edits with the recommendation ref they implement and suggests tests in words. The server keeps an edit only
  if its file was offered, it is allowed, not protected and not a test file, its ref was offered and its `find` occurs exactly once; it builds the unified diff in memory and
  parses each changed file (Python, JSON, YAML; other types `no_parser_for_type`); a parse failure is reported per file (`static_checks_passed: false`), never hidden.
- **Proposal only:** `applied: false`, `executed: false` on every result. Nothing is written to a checkout or run; the proposal is not a candidate patch and cannot be
  approved, published or promoted; the literal candidate path above stays the only way to a patch. Executing model-written code needs a VM-class sandbox (not granted).

## 5. Validation (FEX-04)

- **Input:** `patch_hash`, `base_sha`, the declared command list of the repository (from the extension config; never from the patch or a model), resource caps.
- **Processing:** apply the patch to a fresh isolated checkout (`git apply`), run the declared commands as timed subprocesses in that checkout (wall clock per command, output
  cap, an environment without secret-like variables), capture exit codes and outputs, compute `runtime_digest`. A command that cannot start is `INFRASTRUCTURE_FAILED`, never `FAILED`;
  no declared command is `NOT_RUN`, which is not a pass.
- **Output:** validation report bound to the three identities; `PASSED` only when every required command exited 0 and nothing was skipped.
- **Limit recorded in every report:** the subprocess runner is advisory; the container runtime is locked down but kernel-shared; hostile code is out of scope in this build.

## 6. Exact approval and PR intent (FEX-05)

- Approval is a human decision bound to `patch_hash` **and** `validation_hash`; `code.approve`; distinct from the requester; refused when either hash differs from the current
  records (`stale_binding`), when validation is not `PASSED`, or when the knowledge moved after the commit.
- Without a `pull_request` grant, `publish` is **refused** (`no_publish_grant`, 409) and records the PR intent (what would be published).
- With a declared remote that grants `pull_request` (section 6a), `publish` creates the `code.publish` job (id from candidate + validation hash; a repeat is the same job).
  The worker re-checks, at execution time: candidate completed, validation `PASSED` for the same patch, the current approval binds that validation, the committed knowledge is
  still the target pin, the requester still holds `code.approve`, and the candidate's base is still on the declared branch (`stale_base` otherwise). It then creates, through the
  GitHub REST API, the blobs of the patched files, one tree on the base tree, one commit whose parent is the base, the branch `ripple/<change_id>-<patch_hash[:12]>` and one pull
  request into the declared branch. Retry is idempotent: an existing branch with the same tree and parent and an existing pull request for that branch are reused; a branch with
  other content is `branch_conflict` and is never overwritten. The ledger records `pr_published` (branch, commit, PR number/url). Nothing merges, deletes a branch or changes
  branch protection.

## 6a. Remote repository (FEX-01 read, FEX-05 pull request)

Declared by the operator per repository in `RIPPLE_EXTENSION_CONFIG`:
`"remote": {"provider": "github", "repository": "owner/name", "branch": "main", "grants": ["read", "pull_request"], "token_env": "RIPPLE_GIT_TOKEN"}`; `path` is then a mirror
directory managed by the product. Grants are a subset of `read`, `pull_request`; any other (merge, admin) is refused as `remote_config_invalid`. The token is read from the named
environment variable when a job runs and is sent only to github.com (git over HTTPS through an environment-provided header, the REST API through the Authorization header); it
is never in argv, job results, logs or knowledge. `POST /v2/sources/git/sync` on a repository with a remote creates a `git.fetch` job (one per repository, source revision and
minute; response `{job_id, kind: "git.fetch", created, status}`); the job fetches only the declared branch into the mirror and creates the existing `git.sync` job for that
commit (its result names `sync_job_id`). Failure reasons: `no_read_grant`, `remote_auth_refused`, `remote_not_found`, `remote_branch_missing`, `remote_unreachable`,
`remote_token_missing`, `mirror_not_managed`, `stale_base`, `branch_conflict`. A failed job is retried by a new request. Webhook intake is not built (poll by request).

## 6b. Proposed correspondence on a source refresh (SUR-05, added 2026-10-19)
- `POST /v2/sources/refresh/{refresh_id}/correspondence {workspace_id, attempt 1-3}` (human actor, `source.import` + `ai.use` + `evidence.read`; the refresh must be PREPARED or
  COMMITTED and not a no-op, else 409 `refresh_not_open`; 409 `provider_disarmed`, `budget_exceeded`); `GET .../correspondence` (`project.read` + `evidence.read`; reads only).
  One job (`source.correspondence`, pinned role of the same name) per refresh, refresh digest, attempt and frozen config + pinned model.
- Old side = deleted and changed artifacts (content before, with the rules of the working knowledge bound to them); new side = added and changed artifacts (content after);
  renamed artifacts already carry a proof and are not sent; binary content is never sent (listed as withheld); excerpts are bounded (cut ones listed).
- The model proposes `moved_same_content | moved_rewritten | split | merged | obligation_changed | no_counterpart` with one verbatim quote per side and the bound rule ids
  concerned. The server keeps a proposal only if its paths were offered, both quotes are verbatim in the excerpts, its rule ids are bound to the old artifact and its numbers
  come from the quotes or those rules; it adds `bytes_identical` and `proof_possible` (`content_hash` only for identical bytes; otherwise `human_confirmed_with_reason`;
  `none_needed` for the same path). Old artifacts no kept proposal covers are listed (`unmatched_old`).
- Every proposal is `PROPOSED` with `is_rename_proof: false`. The refresh, its rename proofs, artifact identities, rules and implementation statuses are never changed; a
  rename is linked only by the existing commit path with a proof (a person's `human_confirmed` proof needs a reason). A failed proposal concludes nothing.

## 7. Promotion, verification, recovery (FEX-06..08)

- Promotion copies the validated artifact (the fixture repository at the merge commit) into the mock environment directory and writes a deployment record with the artifact
  hash; a wrong `environment_id` or a hash that differs from the approved one is refused.
- Verification runs the declared scenario checks against the mock environment and records per-axis results; `deployment PROMOTED` never sets `fully_implemented` on the
  business change: the Final journey keeps behaviour `todo` until a verification record exists, and the user result stays a separate decision.
- Recovery: every job is idempotent by its identities; a killed worker leaves no partial record (the Final outbox lease and fencing apply); rollback re-promotes the previous
  artifact hash and records it; it never rewrites approvals or knowledge.

## 8. Permissions

| Capability | Roles in this build | Note |
|---|---|---|
| `code.read` | requester, reviewer, approver, knowledge owner, admin | read candidates, reports, intents |
| `code.patch`, `code.validate` | requester (own change), admin | start a candidate or validation job on the fixture repository |
| `code.approve` | approver, admin (never for own change) | bound to both hashes |
| publish (pull request) | approver, admin (`code.approve`) | only when the repository's remote declares `pull_request`; otherwise refused |
| `code.merge`, `code.deploy` | nobody | never a grant; the mock promotion/rollback is a test-only path behind `RIPPLE_EXTENSION_MOCK_TARGET` |

## 9. Errors

`repo_not_declared`, `base_mismatch`, `path_outside_allowlist`, `protected_file`, `mapping_unresolved`, `patch_does_not_apply`, `model_output_invalid`, `validation_not_passed`,
`stale_binding`, `distinct_approver`, `no_publish_grant`, `wrong_environment`, `artifact_mismatch`, `knowledge_moved`; remote: see section 6a.

## 9a. Reads for the interface (additive; nothing removed or renamed)

**Repositories a member may see.** `GET /v2/sources/repositories?workspace_id=` (`project.read`):
`{schema: "ripple-repositories/1", workspace_id, configured, repositories: [{repo_id, name, source: "local"|"remote", provider|null, grants: {read, pull_request},
ready, unavailable_reason: null|"repo_not_ready"|"remote_config_invalid", sync: {allowed, reason_code: null|"permission_missing"|"repo_not_ready"|"remote_config_invalid"|"no_read_grant"}}], statement}`.
A repository may declare `visible_to` (role names); without it every reader of the workspace sees it. A repository outside the member's visibility is absent, and
`POST /v2/sources/git/sync` and `POST .../code/candidate` refuse it with the same `repo_not_declared` as an undeclared one. Never a path, URL, remote repository
name, token variable or token. `configured: false` = nothing declared (the interface shows the feature as unavailable, with that reason).

**Per-action hints.** `GET /v2/business/changes/{id}/code/candidate?workspace_id=[&repo_id=]` gains `can`:
`{for_candidate, binding: {patch_hash, validation_hash}, repo_id, actions: {create_candidate|validate|approve|reject|publish: {allowed, reason_code, reason}}, statement}`.
The codes come from the same check functions the actions run (`check_candidate`, `check_validation`, `check_decide`, `check_publish` + the publish grant), plus
`permission_missing`, `human_actor_required` and `no_candidate`. A hint is read with the state beside it and is **not an authorisation**: every action checks
permission and state again and may still be refused (tested: an approval read as allowed is refused with `already_decided` after another person decided).

**Stable reason codes** (the interface words exactly these; any other code gets a neutral sentence): `repo_not_declared`, `repo_not_ready`, `remote_config_invalid`,
`no_read_grant`, `no_publish_grant`, `permission_missing`, `human_actor_required`, `not_confirmed`, `knowledge_not_committed`, `stale_binding`,
`candidate_not_completed`, `nothing_to_validate`, `invalid_field`, `distinct_approver`, `validation_not_passed`, `knowledge_moved`, `already_decided`,
`not_approved`, `no_candidate`.

**Value sets shown by the interface.** Candidate `status`: `pending`, `failed` (job), `PROPOSED`, `NOTHING_TO_PATCH` (result). Validation `report_status`:
`PASSED`, `FAILED`, `NOT_RUN` (no declared command; not a pass), `INFRASTRUCTURE_FAILED`. `approval`: null or the latest approve row `{decision_id, kind:
"approve", actor_id, reason, patch_hash, validation_hash, created_at}`; a rejection is visible in `decisions[]` (`kind: "reject"`), `approval` is then null.
`pr_intent`: null or `{branch, title, patch_hash, base_sha, target_pin, validation_hash, approved_by, status: "REFUSED", reason: "no_publish_grant"}`.
`pull_request`: null or `{repository, branch, commit_sha, pr_number, pr_url, pr_state, merged, at, ...}` (never merged by the product). Job status from
`GET /v2/jobs/{id}` (`execution_status`): `QUEUED`/`PENDING_APPROVAL`/`RUNNING`/`COMPLETED`/`FAILED`/`CANCELLED`, for `git.fetch` (result names
`sync_job_id`), `git.sync` (result `sha`, `previous_sha`, files) and `code.publish`.

**Request id.** Every response carries `X-Request-Id` (32 hex characters, server-generated, random; an incoming value is ignored and never echoed). Error
responses are logged with it. It is a reference for support, never a credential; the interface shows it only when present.

## 10. What this contract does not cover

Git providers other than GitHub, webhooks, required checks, CI systems, container registries, cloud environments, hostile code, non-Python toolchains, multi-repository changes, merge or deploy.
