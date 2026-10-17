# SourceChanged v1 envelope (FUTURE contract, not implemented)

Status: **contract only, not implemented.** There is no webhook route, no
subscription, no Git connector and no background watcher. What exists today is
**manual refresh** (`POST /v2/sources/refresh/prepare`, `POST /v2/sources/refresh/commit`,
`GET /v2/sources/refresh/{refresh_id}`): a human uploads the changed source and commits the
reviewed diff. This document fixes the envelope a *future* Git/object-store connector
gateway would send so that the auto-trigger can be added as an extension without redesign.
Git auto-trigger is a future extension; the Harbor demo uses manual refresh only.

Artifacts:

| Artifact | Role |
|---|---|
| `docs/contracts/source_changed.v1.schema.json` | JSON Schema 2020-12, structural half (`additionalProperties: false` at every level). |
| `core/change_intelligence/v2/source_changed.py` | Pure module: `validate_source_changed`, `dedup_key`, `classify_delivery`. No I/O, no network, no threads; no production caller. |
| `tests/fixtures/source_changed/` | 3 valid and 19 invalid examples, each invalid one carrying its expected error code. |

## Envelope

```json
{
  "schema_version": "source_changed.v1",
  "event_id": "evt-0001",
  "connector": {"connector_id": "conn-git-main", "kind": "git"},
  "tenant_id": "tenant-acme", "project_id": "project-harbor",
  "workspace_id": "workspace-harbor", "registered_source_id": "regsrc-harbor-repo",
  "previous_revision": "<sha or null on the first event>",
  "next_revision": "<sha>",
  "git": {"commit": "<must equal next_revision>", "ref": "refs/heads/main"},
  "changed_manifest": [
    {"path": "config/approval.yaml", "change": "modified", "sha256": "sha256:<64 hex>", "byte_size": 812},
    {"path": "docs/old.md", "change": "deleted"},
    {"path": "docs/renamed.md", "change": "renamed", "previous_path": "docs/before.md",
     "sha256": "sha256:<64 hex>", "byte_size": 64}
  ],
  "occurred_at": "2026-09-30T08:15:00Z",
  "auth_provenance": {"method": "hmac_sha256", "key_id": "key-2026-09",
                      "verified_by": "connector_gateway", "verified_at": "2026-09-30T08:15:01Z"}
}
```

Rules enforced by the validator (error codes: `missing_field`, `unknown_field`, `bad_type`,
`bad_format`, `bad_enum`, `empty_manifest`, `duplicate_path`, `bad_path`,
`bad_field_for_change`, `revision_mismatch`, `no_change`):

* `git` is required exactly when `connector.kind == "git"`, and `git.commit == next_revision`.
* `added`/`modified` carry `sha256` + `byte_size`; `renamed` also carries `previous_path`;
  `deleted` carries none of them. Paths are relative, without `..`, `.`, empty segments,
  backslashes or NUL; two entries may not share a (Unicode-normalised, case-folded) path.
* `previous_revision == next_revision` is `no_change`. `occurred_at` is RFC 3339 UTC (`Z`).
* No other field is allowed: an envelope cannot assert an actor, an approval or any authority.

## Order, replay and duplicate semantics

* **Order is the revision chain, never the clock.** An event is applied only when its
  `previous_revision` equals the last revision Ripple ingested for that registered source
  (`null` only when nothing was ingested). `occurred_at` is informational: a far-future or
  far-past timestamp neither jumps the queue nor is refused for its value.
* **Duplicate**: `(connector_id, event_id)` already seen (idempotency key, scoped to the
  connector, never global) is a `duplicate`, acknowledged without any work.
* **Out-of-order**: an event whose `previous_revision` differs from the current revision
  (a replay of an older event, or a successor that arrived before its predecessor) is
  `out_of_order`; it never makes an older revision current and is retried/reconciled by the
  connector, not applied.
* **Reject**: a malformed envelope is `reject` (nothing is stored).
* `classify_delivery(envelope, current_revision=..., seen=...)` returns exactly one of
  `accept`, `duplicate`, `out_of_order`, `reject`.

## Authority and identity

Identity is the **authenticated connector session** (`auth_provenance` is what the gateway
verified, not something a client may assert). `tenant_id`, `project_id`, `workspace_id` and
the other ids are hints that must MATCH the session; a mismatch is refused, never trusted.
The module deliberately exposes no authority helper.

## How it would enter Ripple (when implemented)

A future webhook goes through **the same import / admission / job path** as an upload: the
gateway-verified change set is fetched into an admission report (same admission limits and
checks as `POST /v2/sources/imports`), reviewed as a diff like a manual refresh
(`source_refresh.py` semantics: whole-content compare, rename only with proof), committed in
one CAS transaction with an outbox event and a `source_ingest` job, and consumed by the same
`SOURCE_PUBLISHED{trigger: refresh}` invalidation consumer. It adds no second write path and
no approval: an external observation is never a ChangeSet.

Not claimed: no auto-refresh, no webhook, no Git connector, no scheduled polling.
