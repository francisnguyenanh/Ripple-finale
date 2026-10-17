# Versioned storage and publication contract

Authoritative schema: `infra/migrations/ai1_t03_v2.sql`. Domain ports:
`core/change_intelligence/v2/store_ports.py`. PostgreSQL adapter:
`infra/adapters/v2_postgres.py`. Immutable object adapter:
`infra/adapters/v2_blobs.py`.

## Ownership and scope

This v2 store is independent of the v1 session overlay and `session.work`.
All metadata keys include `(tenant_id, workspace_id)`; revision, decision,
job, artifact, blob reference, index and outbox tables have composite scoped
foreign keys. The server must supply an authenticated principal and resolve
membership before calling the trusted store port. A foreign
tenant/workspace ID produces the same not-found class as a missing ID.

The existing `CloudSQLSessionBackend` supplies useful CAS and blob-check
patterns but persists a whole session JSONB document. V2 uses normalized
tables because concurrent actors require separate immutable revisions,
decisions, jobs, idempotency rows, audit and outbox. V1 remains unchanged.

## Publication sequence

1. `stage_blob(bytes)` hashes bytes with SHA-256 and writes an immutable
   `v2-blobs/<digest>` object. The GCS adapter uploads with
   `if_generation_match=0` and checks same-content retries. The repository
   reads back and verifies the digest before it starts the SQL transaction.
2. `publish_source` locks one scoped workspace row, checks the idempotency
   tuple `(tenant, workspace, operation, key)` and payload hash, then compares
   the expected head version. A same-key/same-payload retry returns the
   original result; different payload raises `idempotency_conflict`.
3. In one PostgreSQL transaction it inserts immutable source and artifact
   revision rows, blob references, advances the head by CAS, appends audit,
   adds an outbox event and records the idempotency result. The head has
   composite foreign keys to source and KM revisions. A crash before commit
   rolls all metadata back; any staged blob is orphaned and is never current.
   A lost response after commit is recovered by replaying the same key.
4. The durable outbox drives reanalysis after source publication. A worker
   claims a scoped row using `FOR UPDATE SKIP LOCKED`, a lease and a rising
   fencing token. Heartbeat and acknowledgement compare worker/token and
   unexpired lease. After five expired claims the row is marked `FAILED`
   with an audit record for operator recovery.

Source publication clears the current KM head; prior KM revisions remain
immutable. Decision recording increments the semantic decision epoch and
also clears current KM. `publish_knowledge` checks workspace version, source
head and decision epoch together, so a stale job cannot promote a result
after source/decision changes. Index readiness is separately keyed by exact
KM revision and index version; durable source/KM data stays readable while
the index is absent or rebuilding.
An index version may move from `BUILDING` to `READY` for the same manifest
hash. Reusing that version with different content or regressing a `READY`
marker is rejected. Idempotency payloads use the existing v2 canonical
serializer/hash; PostgreSQL JSONB stores the resulting structured metadata.

## Immutability, retention and recovery

PostgreSQL triggers reject UPDATE/DELETE on source, artifact and KM revisions,
decisions, and audit. Object bytes are keyed by content digest; a blob read
is checked again against that digest. Blob references from every retained
artifact revision and explicit approval/audit holds prevent garbage
collection from dropping reachable bytes. Garbage collection is not implemented.

The migration is additive and idempotent; it does not edit legacy session
tables. Running the adapter against a fresh local PostgreSQL 16 database and
against Cloud SQL uses the same schema and SQL. `ImmutableGCSBlobs` is
constructed with a trusted GCS bucket; tests use a bucket double and a real
local PostgreSQL container. No filesystem or SQLite backend is used as v2
production authority.

## Integration boundary

This document defines storage ports/adapters and transaction behavior. The v2
registrar, actor/membership enforcement, HTTP handlers and worker loops are
not part of it. The existing v1 product path is untouched; passing
these store integration tests does not claim v2 product reachability.
