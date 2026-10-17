# Cloud readiness — what is portable, and what is not yet

Written so the migration-sensitive boundaries are **named** rather than
discovered during a deploy. This is not a persistence redesign; P9's brief
explicitly excludes one. It is an inventory with evidence.

---

## What is already portable, and why

| Property | Status | Evidence |
|---|---|---|
| No local permanent filesystem state in the runtime | **yes** | a test fails on `open()` or any dangerous import anywhere under `app/` |
| No machine-specific paths | **yes** | the runtime reads only through the overlay; artifacts arrive as bytes |
| No implicit working directory | **yes** | every path is a logical artifact name, never resolved against `cwd` |
| No SQLite in the change-intelligence tier | **yes** | pinned since P1 by an architecture guard |
| Two instances behave identically given one store | **yes** | `Api` holds no state; the registry is injected |
| Deterministic session ids | **yes** | sequential, not random, so replay reproduces a scenario |
| Content-addressed snapshots | **yes** | `snapshot_id` is a hash of content, identical on any machine |

The last two matter more than they look. Because ids are derived rather
than minted from a clock or a UUID, two containers computing the same
transition produce the same identifier — which is what lets session affinity
be a performance optimisation rather than a correctness mechanism.

---

## Migration-sensitive boundaries — named, not hidden

### 1. `SessionRegistry` is process-local

**Today:** an in-process dict. **On Cloud Run it is a per-instance cache, not
the truth.** A second instance would not see a session created on the first,
and a restart would lose every session.

**Boundary:** `app/api/sessions.py::SessionRegistry`. Every method it
exposes — `create`, `get`, `reset`, `count`, `ids` — matches the
`SessionStore` protocol that already exists in
`core/change_intelligence/storage.py` from P1. Migration is a new class
behind the same protocol, and `Api` takes it by injection today.

**What must not be assumed anywhere:** that a session survives a restart, or
that two requests land on the same instance. Nothing in the public contract
promises either, and the reset contract is defined as "return this session
to the baseline", which is correct whichever store holds it.

### 2. `InMemoryOverlay` holds session artifacts in memory

**Today:** blobs in a dict, keyed by content hash. That is the right *shape*
for a cloud store — content-addressed blobs plus a manifest per snapshot map
directly onto object storage plus a row — but it is memory.

**Boundary:** `core/change_intelligence/session.py`. The `SessionArtifactOverlay`
protocol in `storage.py` is what business logic depends on; no module in
`change_intelligence/` names the implementation.

**Sizing note worth stating:** the overlay keeps every snapshot's blobs for
the life of the session. Fine for a demo seed; a real corpus would need the
blob store to be external and the manifest to be the only thing held.

### 3. The baseline is read from disk at start-up

**Today:** `build_baseline()` reads the seed from the repository. In the
two-tier architecture (ADR-001) this is tier 1 — a build-time step whose
output is an immutable baseline — so the *runtime* never reads the
filesystem, and a test enforces that.

**Boundary:** whoever constructs `InMemoryBaseline` at start-up. In cloud
this becomes a read from object storage of a baseline produced offline. The
`baseline_id` is content-addressed, so the correct one can be asserted at
boot rather than hoped for.

### 4. Tier 1 still uses SQLite

The offline document pipeline uses SQLite when building a baseline. That is
**correct and unchanged** — it runs on a build machine, not in the serving
container. Stated here because "no SQLite" is true of tier 2 only, and an
unqualified claim would be wrong.

### 5. Nothing async yet

Pub/Sub is a declared cut. Heavy operations run synchronously. The event bus
is in-process, and `dedup_key` already makes redelivery safe — which is the
property a real broker needs, implemented before the broker exists rather
than after.

---

## What a deploy would still need

Honest list, not a plan:

```text
a SessionStore backed by Cloud SQL, behind the existing protocol
an ArtifactStore backed by Cloud Storage, behind the existing protocol
a start-up read of the baseline from object storage, asserting baseline_id
a health and a readiness endpoint distinct from each other
environment-based configuration with no default credentials
```

The first two are new classes behind protocols that already exist and are
already the only thing business logic depends on. That is the part that was
worth getting right early, and it is the part that is done.

---

## What this document does not claim

No GCP project was provisioned in this phase, nothing was deployed, and
there is no live URL. The public surface is **compatible** with the intended
architecture and the boundaries are named; that is a different and smaller
claim than "deployed", and conflating them is exactly the kind of thing the
rest of this repository has been careful not to do.
