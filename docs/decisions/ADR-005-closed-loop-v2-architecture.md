# ADR-005 — Closed Loop v2: runtime knowledge revisions, worker, workspace, stores, runner

**Date:** 2026-09-25
**Status:** ACCEPTED. The §2.2 amendment of ADR-004 has project-owner acceptance recorded in §6.

---

## 1. Problem

The v1 runtime serves a **read-only baseline built offline from a seed that carries its own answer key**:
- ADR-001 §2, §5.
- `app/api/handlers.py:36,72-111` reads `reference_system/seed_manifest.yaml`.
- Simulation runs seed handlers in-process on `default_config()` (`handlers.py:424`).

The unit of isolation is a judge **session**, not a shared workspace (`app/api/sessions.py:44,111`). Nothing is durable without Cloud SQL, and there is no job or worker.

The closed-loop scope needs the opposite on each of these points:
- Import new sources at runtime.
- Analyse them with a real model in a **durable job**.
- Persist **immutable source, knowledge and decision revisions**.
- Let several authenticated users work in one workspace.
- Execute uploaded code only in an isolated runner.

The goal is to get there **without rewriting `core/`** and without breaking the v1 judge journey.

## 2. Decisions

### 2.1 Runtime knowledge revisions (supersedes ADR-001 §3 row 4 for v2)

- `SourceRevision` and `KnowledgeRevision` are **immutable, content-addressed records**.
  - Their bytes and bundles go to the blob store (§2.5).
  - Their metadata goes to SQL.
  - `workspace.active_source_revision` and `workspace.active_knowledge_revision` are **heads moved only by CAS** (`UPDATE … WHERE version = :expected`).
- A new source always produces a new SourceRevision. A new analysis or decision epoch produces a new KnowledgeRevision.
- No record is updated in place. The spike enforces this with a DB trigger that refuses `UPDATE`/`DELETE` on revision tables.
- The v1 `KnowledgeStore` (read-only, tier-1 build) stays for v1.
- The tier-1 offline pipeline (ADR-001 tier 1, `core/cli.py`) is unchanged and is **not** called by v2.

### 2.2 Worker boundary (amends ADR-004 §1 for v2 only)

- HTTP handlers only **validate, enqueue and read**. Every model call, parse or reanalysis runs in a **worker process**: `python -m infra.worker` in production (spike: `python -m spike_ai0t02.worker`).
- The worker uses the **same image** as the web service but a different entrypoint.
- **Cloud deployment:** the worker runs as a Cloud Run Job (or a worker-pool service) with `--max-instances` bounded.
  - It is a **second Cloud Run resource**, which amends ADR-004's "exactly one service". This needs owner acceptance.
  - The web service `ripple` and its CMD `infra.judge:app` are unchanged.
- **Why not a thread inside the web container:**
  - Cloud Run throttles CPU outside requests and may kill instances at any time.
  - A job would then depend on instance lifetime, which violates C12/C13 and the rule that the offline pipeline never runs inside an HTTP request.
- **Job protocol:** a SQL `analysis_job` row.
  - The worker claims with `SELECT … FOR UPDATE SKIP LOCKED`, taking an expired lease or a queued row.
  - Each claim increments `fencing_token`.
  - A heartbeat extends `lease_expires_at`.
  - `complete`/`fail` succeed only if the token is still current, otherwise `StaleLease`.
  - `attempts` is bounded (default 3). When it is exhausted the job is `FAILED` with a reason, never an infinite retry.
  - Enqueue is idempotent on `(workspace, operation, idempotency_key)`, with a payload hash. The same key with a different payload is a conflict.
- **Publish:** the worker reads **pinned** source/KM/decision revisions. A stale result is stored on the run but **does not move a head** once the head or the decision epoch has changed. Source publish and reanalysis are linked by a durable outbox row (C13). The outbox is part of the storage work that follows; the spike covers only the job lease.

### 2.3 v1 / v2 routing

- **v1 is unchanged:**
  - `infra/judge.py` `/login` + `/rpc`
  - `PUBLIC_SURFACE` (18 entries: `health` + 17 operations, `core/change_intelligence/public_surface.py:157`)
  - the session overlay and seed engine
  
  v1 keeps its regression and smoke tests until a retire decision.
- **v2** is a separately versioned surface (`V2_SURFACE`) with its own registrar. It lives in `app/api/v2/`, mounted by `infra/judge.py` under `/v2/` only when `RIPPLE_V2_ENABLED=1`. Default is off (§2.8).
- The same classification rules apply (READ / WRITE / DECISION / CONTROLLED, with a stated boundary on every write). The same router check applies: handler set == declared surface.
- There is **one apply writer** for both versions: `apply_gate.apply_changeset`. v2 adds approval-digest and permission checks *in front of* it; it does not add a second gate.
- v2 handlers receive ports injected by `infra/`. `app/` keeps the AST inventory check (no `os`, `subprocess`, `open`, `connect`, `loads`…), so every SQL, blob and provider adapter lives in `infra/`.

### 2.4 Shared workspace (supersedes per-session isolation as the v2 isolation unit)

- `tenant → project → workspace` rows, plus membership and permission bundles. The actor comes from server-side authentication. **The session is authentication/UX only.**
- v1 per-session isolation and reset keep their meaning for v1.
- v2 has no "reset". A clean demo uses a new workspace or a copy of the pristine source revision.
- The workspace tables and enforcement belong to the multi-user implementation. The spike only carries `workspace_id` on every row and refuses cross-workspace reads with a not-found that leaks no ids (C06).

### 2.5 Stores

| Kind | Production | Local dev / CI | Semantics that must match (compared in §3) |
|---|---|---|---|
| Blobs (source bytes, KM bundles) | GCS, key `blobs/sha256/<hex>`, create-only upload (`ifGenerationMatch=0`) | `LocalDirBlobStore`: write to a temp file, `fsync`, atomic `os.replace`; key = sha256 | Content-addressed; re-put of the same bytes is a no-op; a read re-verifies sha256 and refuses corrupt bytes; no overwrite; no binary in SQL (ADR-001 §3, kept) |
| Metadata, heads, jobs, decisions, audit, outbox | Cloud SQL Postgres 16 (`<private-identifier>`), schema `ripple_v2` | Postgres 16 in Docker, image `postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea` | Same engine major, READ COMMITTED + row locks, `FOR UPDATE SKIP LOCKED`, CAS by version column, unique idempotency index |
| Retrieval index | Derived projection keyed by `(workspace, knowledge_revision)`, with an index-ready marker (C11). **Technology deferred to the retrieval work** | same | Never the source of truth; a stale index answers `KNOWLEDGE_REBUILDING` |

- **SQLite and the container filesystem are not durable state** for v2. SQLite stays legal in the tier-1 offline build.
- The worker may use an ephemeral scratch dir for legacy adapters that need paths (reuse map R12/R14), then upload results and commit references.

### 2.6 Runner isolation (probed in the spike)

- Every execution of **user-supplied code** (Harbor `python -m app.cli route|match`, pytest) runs in a **fresh container**. It never runs in the API or worker process (C09). This replaces `simulation.discover_handlers` over imported modules for v2 (reuse map R19, baseline S-3).
- **Local runner** (Docker):
  ```
  --network none --read-only --tmpfs /tmp:size=64m --cap-drop ALL
  --security-opt no-new-privileges --user 65534:65534 --pids-limit 128
  --memory 512m --cpus 1
  ```
  - Input snapshot mounted read-only.
  - One writable output bind.
  - Empty environment.
  - Image pinned by digest (spike: `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`).
  - Wall-clock timeout, then kill.
- **Cloud runner:** Cloud Run Jobs execution (gVisor sandbox), with:
  - a dedicated service account with no IAM roles,
  - no VPC connector and egress blocked,
  - the input snapshot passed as a GCS object readable only by that execution,
  - output written to an execution-scoped prefix.

  Cloud equivalence is **not proven by the spike**; it must be verified when the cloud runner is built.
- The Python version used by the runner is **pinned per dataset registration**. Harbor evidence shows 3.14 bytecode while its spec says 3.11 (inventory H5). The actual runtime is recorded per run.

### 2.7 Serialization

- v1 keeps `app/api/codec.py`. Stored v1 session JSON is decoded unchanged.
- v2 canonical form = `core/kernel/serialize.py::canonical_dumps`:
  - sorted keys in codepoint order
  - UTF-8
  - compact separators
  
  It is wrapped by a v2 guard that **refuses `float`** (money and thresholds are decimal strings or minor-unit integers) and requires `schema_version`.
- Content hash = sha256 of the canonical bytes.
- **Manifest order** = codepoint order of the normalised POSIX relpath, never OS path order (OS path order differs across platforms).

### 2.8 Migration, feature flag, rollback

- v2 tables are **additive**, in their own schema `ripple_v2`, with numbered forward migrations. v1 tables (`state_store`, `events_store`, `session_versions`, `cloud_adapters.py:185-217`) are untouched.
- `RIPPLE_V2_ENABLED` defaults to off. With it off, the deployed surface is byte-identical to v1: no upload, no v2 route, no worker required.
- **Application rollback** = turn the flag off and redeploy the previous image. v2 data is kept. There is no down-migration that deletes decisions .
- The **demo baseline is not lost**:
  - v1 keeps reading it as today.
  - The migration imports it into v2 as a SourceRevision with `provenance=legacy_baseline`. Legacy inferences are not marked human-confirmed.

### 2.9 Model boundary

- Structured tasks (`extract_rules`, …) go through the existing `core/agent/provider.py::LLMProvider` + `complete_structured`, reused unchanged. Cloud transport stays in `providers/` and `infra/`.
- **Evidence is script-owned.** `evidence`, `id`, `lineage` and `provenance` are in `FORBIDDEN_OUTPUT_KEYS` (`core/agent/provider.py:249`).
  - The model returns a claimed quote and line span.
  - The worker builds the EvidenceRef only after `core/agent/validate.py::check_evidence_location_and_quote` confirms the quote against the **pinned revision bytes**. Those bytes are preloaded into its `file_cache`, so no file is read by path.
- **Failure behaviour:**
  - Outage, refusal or unparseable output → the run is `FAILED` or `PARTIAL` with an error code (`provider_unavailable`, `invalid_output`). **No candidates and no KM head are published** (C19).
  - A candidate whose quote does not verify is rejected and counted. It is never stored as VALIDATED.
- **Budgets:** retries and token budget per job are bounded. Values are fixed in the budget configuration; without it the worker refuses to start without explicit limits.

### 2.10 Review board

- v2 review uses `core/agent/board.py::select_review_board` + `core/agent/arbiter.py::arbitrate` (CLI-proven, reuse map R13).
- `core/review/{board,arbiter,isolation}.py` (no production caller) are **not** wired into v2. Their isolation rules may be harvested into the chosen board, with tests.
- There is one orchestration path .

### 2.11 Seed and oracle exclusion

v2 code paths never read:
- `reference_system/seed_manifest.yaml`
- `semantic_hints`
- `ripple/_testdata/generated/{evaluator_private,authoring,operator,reports}`

**Enforcement:**
- The spike worker has an audit-hook `open` log (`sys.addaudithook`), which a test asserts on.
- A static scanner test checks the same paths.
- The image exclusion (baseline S-1) is applied in the image build.

### 2.12 Where code lives

In short:
- **Pure domain:** `core/change_intelligence/**`. Q5 tests stay green: no infra names and no provider there.
- **Ports:** Protocols next to the domain.
- **Adapters:** `infra/adapters/v2/` (SQL, blobs, GCS, provider transport).
- **Worker:** `infra/worker/`. **Runner:** `infra/runner/`.
- **v2 API handlers:** `app/api/v2/`, with ports injected.

`core/agent/*` is reused through the worker only.

## 3. Local vs cloud storage semantics (acceptance item)

| Property | Local (spike) | Cloud | Same? / risk |
|---|---|---|---|
| Blob create-only | `os.replace` onto a sha-named file. A second put with identical bytes is a no-op; the key is the content, so different bytes get a different key | `ifGenerationMatch=0` → 412 if the object exists | Same contract. GCS has strong read-after-write consistency |
| Blob integrity | sha256 re-verified on every `get` | re-verify after download (as `GCSBaselineStore` does, `infra/adapters/cloud_adapters.py:67-82`) | Same code path in the adapter |
| Partial write | temp file + atomic rename, so no partial object is visible | resumable upload commits atomically | Same |
| Orphan blobs | possible after a crash before the SQL commit; not referenced, so never current | same | GC keeps referenced blobs; GC is out of scope for the spike |
| SQL engine | Postgres 16 in Docker | Cloud SQL Postgres (version to be confirmed on `<private-identifier>`) | Same SQL; **a version mismatch is a risk** until checked |
| Job lease clock | `now()` of the DB server | same | Uses the DB clock, not the worker clock, which avoids skew |
| Connection loss mid-transaction | rollback, lease expires, reclaim | same, plus the Cloud SQL connector | Same |
| Durability across restart | Docker volume | managed | Same for the purpose of the spike. The Docker volume is not a backup (AI11) |

## 4. What the spike had to prove

The spike chain is: new Markdown artifact (Harbor `docs/procurement_policy.md`, copied and pinned) → immutable SourceRevision → durable analysis job → provider structured candidate → evidence validator → SQL record → API read. It must prove all of the following:
- A **process restart** between queue and run, and a crash after claim, leave the job and result intact.
- A **spy** proves the exact input bytes and hash reached the provider, and that no seed or oracle file was opened.
- **Model failure** publishes nothing.
- The **runner probe** demonstrates the isolation flags.

The spike is **disposable** (`spike_ai0t02/`); only the pieces that carry over are promoted.

## 5. Consequences

**Gained**
- Runtime ingestion and analysis of new sources.
- Durable, restartable jobs.
- Immutable history that rollback can build on.
- A real permission boundary.
- `core/` reused, not rewritten.
- v1 untouched.

**Lost / costs**
- A second Cloud Run resource (the worker).
- A Postgres schema to migrate.
- A container runner to operate.
- Two surfaces during the transition, which must share the one apply gate.

**Risks**
| Risk | Mitigation | Checked by |
|---|---|---|
| v2 imports the seed "for convenience" | audit-hook + static scanner | spike, static scanner test |
| Worker CPU/time on Cloud Run | Cloud Run Job with bounded retries | worker deployment |
| Cloud SQL version ≠ local | confirm and pin | infra setup |
| Runner escape via a misconfigured flag | probe test that fails when any flag is dropped | spike, runner probe test |
| Private oracle ships in the image (S-1) | `.dockerignore` fix + image test | image build, before any AI evaluation run |

## 6. Owner decision record (2026-09-25)

- **§2.2 amendment of ADR-004 (worker as a second Cloud Run resource): ACCEPTED by the project owner**, 2026-09-25.
  - The web service `ripple` (`infra.judge:app`) is unchanged.
  - The worker Cloud Run Job/worker-pool may be deployed.
- **Vertex project for live runs:** supplied by the owner and passed only via env `GCP_PROJECT` (`ai_module/accounts.py:27-29`). It is not written into repository config.
