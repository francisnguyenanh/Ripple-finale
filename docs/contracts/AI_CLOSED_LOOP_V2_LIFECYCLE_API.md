# Lifecycle, permission and API v2 contract

The normative transition and operation registries are
`core/change_intelligence/v2/lifecycle.py` and
`core/change_intelligence/v2/api_contract.py`. They are domain definitions,
not HTTP endpoints. Existing v1 routes and serialized state names remain
unchanged. The versioned storage layer (`AI_CLOSED_LOOP_V2_STORAGE.md`) supplies durable CAS,
idempotency, membership and outbox; the v2 registrar and product entrypoints are wired separately.

## Transition contract

Every edge is in `transition_table(machine)`; an absent edge is forbidden.
`authorize_transition` checks actor kind, current permission, current
membership, workspace read-only state, expected source revision, policy
version and decision epoch before returning an auditable transition event.
The caller must persist that event and new head in one CAS transaction.

| Machine | Human decision edges | Service or validation edges | Important boundary |
|---|---|---|---|
| Analysis | cancel may be requested by an authorized user | queued/running/result with bounded retry | Result does not imply accepted knowledge |
| Candidate | `PENDING_HUMAN → CONFIRMED` via `knowledge.confirm` | `PROPOSED → VALIDATED → PENDING_HUMAN` | VALIDATED checks structure/evidence only |
| Knowledge | `PENDING_HUMAN → ACCEPTED` via `knowledge.confirm` | DRAFT/VALIDATED, stale/superseded | Accepted KM is a separate attestation |
| Gap | answer confirmation and `ACCEPTED_UNKNOWN` via `knowledge.confirm` | `OPEN → ANSWER_PROPOSED`, reopen | Unknown requires owner, scope, reason and review date; answer is not evidence by itself |
| Conflict | decision or scoped exception via `conflict.resolve` | remediation and verification | Exception retains residual mismatch; no `ALIGNED` transition |
| Change | `REVIEW_READY → APPROVED` via `change.approve`; `AWAITING_ACCEPTANCE → COMPLETED` via `change.accept_result` | staging, source publish, reanalysis, machine verification | Completion needs machine verification and zero required open work |

AI and provider outputs cannot exercise human decision edges. A service may
stage only against a current approval. A source restore must use
`source.restore` at preview/approval and the same apply gate at publication;
the historic snapshot contains bytes only, never restores membership, ACL,
credentials or revoked decisions. The current membership and policy are read
again at apply and publish. The `require_distinct_approver` policy rejects a
requester's own approval when enabled (pilot default: true; demo may disable
with an audit record).

`ApprovalBundle` hashes exact proposal, source and knowledge digests,
decision epoch, dependencies, review, tests and policy version using the
canonical v2 serializer. `validate_approval_at_execution` compares the
recorded digest and current revision, permission, policy and decision epoch.
An initial approval is distinct from `result.accept`; the latter follows
source publication, reanalysis, machine verification and human inspection of
the final report. A failed or partial report with required work cannot reach
`COMPLETED`.

## API v2 registry

`OPERATIONS` lists every public v2 capability in contracts §6 with its
required permission, mutation bit, job bit, response fields and v1 reuse
candidate. The transport prefix is `/v2/workspaces/{workspace_id}/operations/{name}`;
this is a contract namespace, not a live route. The server authenticates the
actor and scopes all reads and writes to tenant/project/workspace before
calling domain logic. `apply.execute` is a worker identity only; no user
endpoint can claim it. The source restore apply operation also consumes a
current, human-approved restore digest through that gate.
The shared request, synchronous response, job response and error JSON
Schemas are in `AI_CLOSED_LOOP_V2_API.schema.json`. Operation-specific payload
objects must use the domain DTO (or the dedicated phase DTO) when the
operation is wired; this envelope schema does not authorize an untyped
endpoint.

Every request has `schema_version: "1.0.0"` and `workspace_id`. Mutations
also require `idempotency_key`, `expected_revision` (or an exact expected
digest mapped to that revision) and a typed `payload` object. The server
derives actor/permission from authentication, not from payload fields.
An asynchronous operation returns `{schema_version, workspace_id,
operation_id, status}` and is polled by a read/status operation. Synchronous
responses carry `{schema_version, workspace_id, revision}` plus the typed
result. Domain payload and result schemas are the typed domain DTOs or the
phase-specific DTO added when an endpoint is wired; no generic `success`
placeholder is an accepted response. Duplicate `(tenant, workspace,
operation, idempotency_key)` with identical payload returns the same result;
a different payload returns `idempotency_conflict`. CAS mismatch returns
`stale_revision` without mutation.

The common error vocabulary is `forbidden`, `stale_revision`,
`invalid_evidence`, `unresolved_gap`, `unsupported_format`,
`unsupported_simulation`, `provider_unavailable`, `budget_exceeded`,
`runner_failed`, `recovery_required`, `idempotency_conflict`,
`invalid_request`, `not_found` and `knowledge_rebuilding`. Foreign tenant
IDs and paths are never echoed in errors. Read and status calls also enforce
workspace ACL and field-level restrictions.

## Reuse and migration

The v1 `core/change_intelligence/states.py` vocabulary and `apply_gate.py`
remain intact. V2 definitions are separate because `VERIFIED` in v1 has a
weaker meaning than the v2 post-publication, reanalysis and acceptance
sequence. Action planning and the single apply gate are reused;
the gate must migrate to this exact approval digest and current membership check.
No current product route imports these v2 registries yet; wiring is not claimed by the
module tests.
