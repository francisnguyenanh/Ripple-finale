# AI Closed Loop v2 schemas — version / migration table

Source modules: `core/change_intelligence/v2/schema.py`,
`core/change_intelligence/v2/serialize.py`, `core/change_intelligence/v2/compat.py`.
Acceptance tests: `core/tests/test_ai1_t01_schema.py`.

## 0. Summary of the current schema rules

This section collects the rules that the later sections define in detail.

- **Manifest content binding.** `SourceRevision.manifest` is a
  `Tuple[ManifestEntry, ...]`, where `ManifestEntry` carries `artifact_id`,
  `artifact_revision_id` and `blob_hash`. `manifest_hash` is a verified content
  hash over each entry's `artifact_id` and `blob_hash` (§6a). A caller-supplied
  digest that does not match the recomputation is rejected.
- **One active revision per artifact.** Each `SourceRevision` snapshot contains
  **at most one active revision per `artifact_id`**. The
  `(artifact_id, artifact_revision_id)` sort therefore has the same order as the
  hashed `artifact_id` values, and an identical `(artifact_id, blob_hash)` set
  always has one digest. Duplicate IDs raise `ValidationError`, even when their
  revision IDs and blob hashes differ. The canonical JSON hash and caller-supplied
  digest verification are otherwise unchanged.
- **Operators.** `RuleCandidate.operator` is optional, but when present must be
  exactly one of `>`, `>=`, `<`, `<=`, `==`, `!=`. The distinct strict and
  inclusive symbols are serialized unchanged.
- **Effective intervals.** `RuleCandidate` and `HumanDecision` carry optional
  `effective_start`/`effective_end` string fields, validated as ISO 8601
  timestamps with seconds and an explicit `Z` or `±HH:MM` offset. Invalid
  calendar dates, local/ambiguous timestamps and reversed intervals are
  rejected. They also carry `effective_start_inclusive: bool = True` and
  `effective_end_inclusive: bool = False`; for open bounds, the corresponding
  flag is preserved but has no effect. Equal instants are valid only when both
  flags are true. Comparison uses the timezone-aware instants, so different
  offsets cannot hide a reversed interval. All required event `created_at`
  fields (`SourceRevision`, `HumanDecision`, `AnalysisRun`, `Approval`) use the
  same timestamp validator. The inclusion flags are additive fields under
  schema version `1.0.0`; persisted records without them decode with the
  documented `[start, end)` default. Date-only or timezone-free values must be
  given an explicit timezone before construction. Business-day calculations
  require a later policy and are not inferred from these timestamps.
- **Uniform `schema_version`.** Every domain type carries a `schema_version`
  field, gated by the same `_require_schema_version` (§2).
- **Decimal type tag.** `serialize.py` tags `Decimal` with a reserved,
  NUL-embedding key (`_DECIMAL_TAG_KEY`) and escapes any plain user dict that
  already carries that key (§7a).

## 1. What this document covers

The v2 schema surface (source/observation/rule/edge/KM/finding/decision/run/
proposal/approval/verification/recovery). Every named domain has a type:

| Type | Module |
|---|---|
| `Money` | `schema.py` |
| `EvidenceRef` | `schema.py` |
| `ArtifactRevision` | `schema.py` |
| `SourceRevision` | `schema.py` |
| `RuleCandidate` (rule, candidate) | `schema.py` |
| `Rule` (rule, confirmed — distinct from `RuleCandidate`) | `schema.py` |
| `Observation` | `schema.py` |
| `RelationCandidate` / `Relation` (edge) | `schema.py` |
| `KnowledgeRevision` (KM) | `schema.py` |
| `Finding` | `schema.py` |
| `HumanDecision` (decision) | `schema.py` |
| `AnalysisRun` (run) | `schema.py` |
| `PatchProposal` (proposal) | `schema.py` |
| `Approval` (approval) | `schema.py` |
| `VerificationReport` (verification) | `schema.py` |
| `RecoveryProposal` (recovery) | `schema.py` |

State-machine *transition enforcement* (as opposed to a `lifecycle_state`
enum field accepting one of the valid state names) is defined in
`AI_CLOSED_LOOP_V2_LIFECYCLE_API.md`. The types in this module validate that
`lifecycle_state`/`status` is one of the named states, and a small number of
cross-field invariants (e.g. C15 on `VerificationReport`), but do not implement
the full transition graph as a state machine.

## 2. `schema_version` table

**Versioning strategy:** per-type field, not a shared envelope. Every one of
the 13 domain types in this module (`ArtifactRevision`, `SourceRevision`,
`RuleCandidate`, `Rule`, `Observation`, `RelationCandidate`/`Relation`,
`KnowledgeRevision`, `Finding`, `HumanDecision`, `AnalysisRun`,
`PatchProposal`, `Approval`, `VerificationReport`, `RecoveryProposal` —
14 counting `Relation` separately) carries its own `schema_version: str`
field, gated by the same `_require_schema_version` function. Types that
default the field (`SourceRevision`, `ArtifactRevision`, `HumanDecision`,
`AnalysisRun`, `Approval`, `VerificationReport`, `RecoveryProposal`) default
it to `"1.0.0"` so call sites that never pass `schema_version` keep
constructing valid objects; an explicit unsupported/malformed value passed by
a caller is still rejected exactly like the types that have a required
(no-default) `schema_version`.

| `schema_version` | Meaning | Status |
|---|---|---|
| `1.0.0` | Initial v2 shape for every domain type in this module (§3, §3a) | supported |
| any other value (e.g. `99.0.0`), missing, or non-`str` | rejected | `SchemaVersionError` (unsupported/malformed) or `TypeError`/`ValidationError` (missing/wrong-typed) |

There is no coercion path: an unrecognised version is never silently
treated as the nearest known version.

| Type | Has `schema_version`? |
|---|---|
| `ArtifactRevision` | yes, default `"1.0.0"` |
| `SourceRevision` | yes, default `"1.0.0"` |
| `RuleCandidate` | yes (required, no default) |
| `Rule` | yes, required, no default |
| `Observation` | yes (required, no default) |
| `RelationCandidate` / `Relation` | yes (required, no default) |
| `KnowledgeRevision` | yes (required, no default) |
| `Finding` | yes (required, no default) |
| `HumanDecision` | yes, default `"1.0.0"` |
| `AnalysisRun` | yes, default `"1.0.0"` |
| `PatchProposal` | yes (required, no default) |
| `Approval` | yes, default `"1.0.0"` |
| `VerificationReport` | yes, default `"1.0.0"` |
| `RecoveryProposal` | yes, default `"1.0.0"` |
| `Money`, `EvidenceRef` | no — value types embedded within a versioned record, not independently persisted/exchanged; the table above already covers every persisted/exchanged domain object |

## 3. `RuleCandidate` v1.0.0 — field table

| Field | Type | Required | Validation |
|---|---|---|---|
| `candidate_id` | `str` | yes | — |
| `subject` | `str` | yes | — |
| `predicate` | `str` | yes | — |
| `value` | any (typically `Money`) | yes | — |
| `applicability` | `str` | yes | must be one of `RECOGNIZED`, `UNKNOWN` (unknown spellings such as `'BOGUS'` are rejected) |
| `applicability_reason` | `Optional[str]` | no (default: `None`) | rejected if applicability is `UNKNOWN` and reason is `None`, empty, or whitespace-only — see §5 for why the default is `None`, not a placeholder |
| `modality` | `str` | yes | must be one of `must`, `may`, `observed` (unknown spellings rejected) |
| `operator` | `Optional[str]` | no (default `None`) | when present, exactly `>`, `>=`, `<`, `<=`, `==`, or `!=` |
| `unit` | `Optional[str]` | no (default `None`) | contracts §2.1 "currency/unit" for a non-`Money` scalar `value` |
| `exceptions` | `Tuple[str, ...]` | no (default `()`) | contracts §2.1 "exceptions" |
| `outcome` / `actor` | `Optional[str]` | no (default `None`) | contracts §2.1 "outcome/actor" |
| `effective_start` / `effective_end` | `Optional[str]` | no (default `None`) | ISO 8601 timestamps with seconds and explicit timezone; reversed or empty intervals rejected |
| `effective_start_inclusive` / `effective_end_inclusive` | `bool` | no (defaults `True` / `False`) | preserve boundary semantics; equal instants require both inclusive |
| `unknown_fields` | `Mapping[str, Any]` | no (default `{}`) | contracts §2.1 "unknown_fields" — preserved, not silently dropped |
| `evidence_support` / `evidence_contradiction` | `Tuple[EvidenceRef, ...]` | no (default `()`) | contracts §2.1; cross-revision refs rejected same as `evidence` |
| `model_run_id` | `Optional[str]` | no (default `None`) | contracts §2.1 "model_run_id" |
| `source_revision_id` | `str` | yes | must match every `EvidenceRef.source_revision_id` in `evidence`/`evidence_support`/`evidence_contradiction` |
| `artifact_revision_id` | `str` | yes | must match every `EvidenceRef.artifact_revision_id` in the same three groups |
| `evidence` | `Sequence[EvidenceRef]` | yes | cross-revision refs rejected |
| `scope` | `dict` | yes, no default | missing/empty/wrong-typed rejected; never defaults to "applies to all" |
| `schema_version` | `str` | yes, no default | must be in the supported-version table above |

## 3a. `Rule` v1.0.0 — field table

Distinct from `RuleCandidate`. `Rule` is the system-issued, human-confirmed
record; `RuleCandidate` is the AI-proposed, not-yet-authoritative
inference.

| Field | Type | Required | Validation |
|---|---|---|---|
| `rule_id` | `str` | yes | non-empty; system-issued, not caller-chosen by convention (not enforced structurally — a naming/issuance convention, not a schema constraint) |
| `version` | `int` | yes | must be a positive int |
| `candidate_ids` | `Tuple[str, ...]` | yes | non-empty — candidate ancestry: a confirmed `Rule` must trace back to at least one `RuleCandidate` |
| `normalized_semantics` | `Mapping[str, Any]` | yes | non-empty dict |
| `confirmation_state` | `str` | yes | must be one of `DRAFT`, `VALIDATED`, `PENDING_HUMAN`, `CONFIRMED`, `REJECTED`, `SUPERSEDED` — the contract gives no enumerated closed set for this field, so this set is a design choice (see section 10) |
| `evidence` | `Tuple[EvidenceRef, ...]` | yes | sequence-typed (no cross-revision check at this level — evidence cross-revision validity was `RuleCandidate`'s concern; `Rule`'s evidence is already-confirmed) |
| `decision_ids` | `Tuple[str, ...]` | yes | sequence-typed; non-empty REQUIRED when `confirmation_state == "CONFIRMED"` (contracts §2.1 "decision refs" — a confirmed Rule needs at least one `HumanDecision` reference) |
| `scope` | `Mapping[str, Any]` | yes, no default | missing/empty/wrong-typed rejected; never defaults to "applies to all" (same discipline as `RuleCandidate.scope`) |
| `schema_version` | `str` | yes, no default | must be in the supported-version table (§2) |
| `superseded_by` | `Optional[str]` | no (default `None`) | merge/split REDIRECT target: the new `rule_id` this Rule has been superseded by. `None` while this Rule is current. Setting it is a forward pointer, not a deletion — this Rule's own fields/history are unchanged |
| `merged_from` | `Tuple[str, ...]` | no (default `()`) | ancestry: `rule_id`s this Rule was formed by merging (empty if not a merge) |
| `split_from` | `Optional[str]` | no (default `None`) | ancestry: the single `rule_id` this Rule was formed by splitting from (`None` if not a split) |

**Merge/split scope note:** the schema provides room for merge/split
ancestry/redirect (`superseded_by`, `merged_from`, `split_from`); the
state-machine enforcement of who may trigger a merge/split and under what
preconditions is defined by the lifecycle contract
(`AI_CLOSED_LOOP_V2_LIFECYCLE_API.md`), not by this schema layer.

## 4. `EvidenceRef` v1.0.0

Fields: `source_revision_id`, `artifact_revision_id`, `content_hash`,
`locator_type`, `locator`, `quote`, `parser_version` — all required,
non-empty strings. A locator-less evidence ref is rejected (bytes cannot
be re-checked without one).

## 5. `Money` v1.0.0

`amount: Decimal` (rejects `float` input with `TypeError`), `currency: str`
(non-empty). Negative amounts rejected (`ValidationError`). Cross-currency
comparison never returns a false "equal": the dataclass-generated `__eq__`
compares `(amount, currency)`, so different-currency, same-amount values
compare unequal. **No cross-currency arithmetic operator is implemented**
— `Money` does not define `__add__`; combining amounts across currencies
is simply not offered as an operation on this type, by design. This is a deliberate design choice, not an oversight.

`RuleCandidate.applicability_reason` defaults to `None`
(`core/change_intelligence/v2/schema.py`, `_UNSPECIFIED_REASON = None`), not to
an internal non-blank placeholder.


## 6. `SourceRevision` / `ArtifactRevision` v1.0.0

`SourceRevision.id` (event/transaction identity) and `.manifest_hash`
(content hash over the manifest) are structurally distinct fields:
restoring identical bytes reproduces the same `manifest_hash` while still
minting a new `id` — the two are never the same field or the same value
by construction. `manifest` must be a non-empty sequence of
`ManifestEntry(artifact_id, artifact_revision_id, blob_hash)` (all three
non-empty `str`) in **sorted** order, sorted by
`(artifact_id, artifact_revision_id)` (contracts §2.1: "sorted manifest
artifact revisions"); an unsorted or empty manifest is rejected, and so is a
manifest with more than one entry for the same `artifact_id`. `manifest_hash`
is **verified**, not merely trusted — see §6a.

`SourceRevision.source_connector_id: Optional[str]` (contracts §2.1
"source connector/import ID") identifies which source connector/importer
produced this event; `None` for events with no external-connector origin
(e.g. a restore).

`ArtifactRevision.artifact_id` (stable across renames) and `.revision_id`
(this specific revision) are both required, non-empty. `source_role` must
be one of `normative`, `implementation`, `observational`, `test`,
`unknown`. `logical_path`, `media_type`, `blob_hash`, `parser_version` must
be non-empty strings and `byte_size` a non-negative `int`.
`ArtifactRevision.provenance: str` (contracts §2.1 "provenance") must be one
of `original_upload`, `derived`, `restored`; it defaults to `original_upload`.

## 6a. `manifest_hash` algorithm

The manifest is a sorted tuple of `ManifestEntry(artifact_id,
artifact_revision_id, blob_hash)` objects. The canonical content this hash
binds to is a list of `{"artifact_id": ..., "blob_hash": ...}` objects, one
per manifest entry, **in manifest order** (already validated sorted by
`(artifact_id, artifact_revision_id)`). `artifact_revision_id` is
deliberately EXCLUDED from the hashed content — see the identity-vs-content
decision below.

**Algorithm:** `"sha256:" + sha256(hexdigest)` of the UTF-8 encoding of
`core/kernel/serialize.py::canonical_dumps(content)`:

```python
def _compute_manifest_hash(manifest: Sequence[ManifestEntry]) -> str:
    content = [
        {"artifact_id": entry.artifact_id, "blob_hash": entry.blob_hash}
        for entry in manifest
    ]
    text = canonical_dumps(content)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
```

`canonical_dumps` is `core/kernel/serialize.py`'s sorted-key,
stable-separator JSON serializer (also used by `canonical_hash`, §7).
`serialize.py` has no dependency back on `schema.py`, so importing
`canonical_dumps` from `schema.py` introduces no cycle.

**Injectivity:** a canonical JSON array of explicitly-keyed objects gives
every field an unambiguous, escaped boundary (JSON string escaping turns an
embedded literal newline into the two-byte sequence `\n` inside the quoted
string, and object/array brackets are never valid inside an unescaped
string), so no two distinct `(artifact_id, blob_hash)` sequences can
serialize to the same text. In particular, one `ManifestEntry` with an
embedded-newline `artifact_id` and two separate entries do not collide:
`_compute_manifest_hash((ManifestEntry("a\nb","r1","blob1"),))` !=
`_compute_manifest_hash((ManifestEntry("a","r1","blob1"),
ManifestEntry("b","r2","blob1")))`. The one-entry case serializes as a
one-element JSON array; the two-entry case as a two-element array — the
bracket/brace structure alone already differs, before any string content is
compared. (A scheme that joined bare IDs with newlines would not be
injective, because a newline inside one entry is indistinguishable from a
separator between two entries; it would also hash identity rather than
content.)

**Identity-vs-content decision:** the digest input uses ONLY `artifact_id` +
`blob_hash`; `artifact_revision_id` does NOT participate. Therefore a new
`ArtifactRevision.revision_id` minted for byte-identical content (same
`blob_hash`) under the same `artifact_id` REUSES the same `manifest_hash`;
only a real content change (`blob_hash` differs) or a different artifact
(`artifact_id` differs) changes the digest. This tracks contracts §2.1's
content-identity intent: the hash names WHAT is there, not which revision
label currently points at it. The property "an identical
`(artifact_id, blob_hash)` set always has one digest" relies on artifact IDs
being unique within a snapshot, which `SourceRevision` enforces (§0).

**Enforcement point:** `SourceRevision.__post_init__` VERIFIES a
caller-supplied `manifest_hash` against this recomputation and raises
`ValidationError` on any mismatch (forged, wrong, or merely stale digest). It
does not silently overwrite the caller's value with the correct one, which
keeps the invariant airtight against a forged digest coincidentally matching
what the caller expected.

**Property:** restoring identical manifest content (same sorted
`(artifact_id, blob_hash)` set, any new `id` or `artifact_revision_id`)
always reproduces the identical `manifest_hash`, because the recomputation is
a pure function of `manifest`'s `artifact_id`/`blob_hash` pairs alone. A
forged/mismatched digest is rejected with `ValidationError`, not silently
accepted.

A manifest element built as a bare string (rather than a `ManifestEntry`)
fails with `AttributeError`, because a bare `str` has no
`.artifact_id`/`.blob_hash`.

## 7. `canonical_hash` (serialize.py)

`canonical_hash(obj) -> str` converts dataclasses/`Decimal`/dict/list/tuple
to a JSON-able structure, then serializes through
`core/kernel/serialize.py::canonical_dumps` (sorted keys, stable
separators) and SHA-256-hashes the resulting text. Deterministic across
independent construction of logically-identical objects; independent of
dict key insertion order; changes when content changes.

**Type tagging:** every `Decimal` value is tagged with a stable marker before
hashing, so `Decimal('1')` and the string `'1'` do not collide (without a tag
both would serialize to the bare JSON string `"1"`). A bare `str` value
serializes to a plain JSON string, which can never equal a JSON object
textually. The tag shape and its escaping are specified in §7a; see
`core/change_intelligence/v2/serialize.py::_to_jsonable`.

Known, accepted boundary: `list` and `tuple` with identical elements hash
identically (both become a JSON list), pinned by
`core/tests/test_ai1_t01_schema.py::
test_canonical_hash_list_and_tuple_with_same_elements_collide`; JSON has
no separate tuple type, and no `schema.py` field's runtime type varies
between `list` and `tuple` in a way that would make this ambiguous in
practice.

## 7a. Decimal type-tag escaping scheme

A tag shaped like an ordinary JSON object, e.g.
`{"__type__": "decimal", "value": str(x)}`, would collide with a user dict of
that exact shape (`canonical_hash(Decimal('1')) ==
canonical_hash({'__type__': 'decimal', 'value': '1'})`) in any open
`Any`/mapping field (e.g. `RuleCandidate.value`,
`RuleCandidate.unknown_fields`). The scheme therefore uses a reserved,
unambiguous tag key.

**Tag:** `_DECIMAL_TAG_KEY = "\x00__ripple_v2_decimal_type_tag__\x00"` (embeds
NUL control characters around a long, module-private literal). `Decimal(x)`
serializes to `{_DECIMAL_TAG_KEY: str(x)}` — a single-key object under a key
no ordinary JSON/YAML-authored dict literal can produce by accident (NUL
bytes are not typable in a JSON string literal without an explicit `\u0000`
escape, which no legitimate field name in this codebase uses).

**Escaping:** if a plain user `dict` is encountered that ALREADY contains
`_DECIMAL_TAG_KEY` (or the escape marker itself) as one of its own keys — an
adversarial or coincidental lookalike — `_to_jsonable` wraps that whole dict
as `{_ESCAPE_KEY: <dict, recursively converted>}`, where `_ESCAPE_KEY =
"\x00__ripple_v2_escaped_user_dict__\x00"` is a second, DISTINCT reserved
literal. Even a user dict deliberately constructed to mimic the internal
Decimal-tag shape therefore serializes to a JSON object keyed differently
from a real Decimal tag, and can never collide with it.

**Properties:** `canonical_hash(Decimal('1'))` differs from
`canonical_hash('1')` and from
`canonical_hash({'__type__': 'decimal', 'value': '1'})`. Determinism and
key-order independence are preserved (`canonical_dumps` still sorts keys; the
escape/tag keys are single, fixed literals, not derived from input order).

## 8. Legacy compatibility (`compat.py`)

### 8.1 `decode_legacy_knowledge_record`

`decode_legacy_knowledge_record(dict) -> LegacyKnowledgeRecordDTO` decodes
a legacy `core/change_intelligence/knowledge.py::KnowledgeRecord`-shaped
dict losslessly: every legacy field (`id`, `knowledge_type`, `semantic_id`,
`statement`, `evidence`, `derived_from`, `state`, `alignment`,
`representations`, `gap_id`, `reason`, `km_version`) is carried through
verbatim, with no enum-value rewriting (`state`/`alignment` keep their old
serialized spelling). A record missing a required legacy field (e.g.
`derived_from`) raises `KeyError` rather than substituting a silent
default.

**Unknown-field policy:** an input dict carrying a key outside the fixed
legacy field set is **not silently discarded**. The decoder collects every
non-legacy key into `LegacyKnowledgeRecordDTO.unknown_fields: Dict[str,
Any]`, verbatim. The policy is **preserve**, not raise, because a legacy
store may accumulate fields this schema did not anticipate, and raising
would make every such field a hard compatibility break instead of a
visible, inspectable one.

There is no "v1 -> v2 knowledge record" migration function; the decoder only
produces a read-facing DTO. A write-back/upgrade path does not exist.

### 8.2 `decode_legacy_session`

`decode_legacy_session(dict) -> LegacySessionDTO` decodes a legacy session
dict into a schema-compatible DTO.

Legacy shape read from `app/api/sessions.py::JudgeSession`/
`SessionRegistry` (read-only, not modified): `session_id`, `overlay`,
`lineage`, `baseline_id`, `created_at`, `work`, `durable_version`,
`orchestrator`. `overlay`/`lineage`/`orchestrator` are live, in-process
objects (an `InMemoryOverlay`, a `VerifiedLineage`, an arbitrary
orchestrator reference) with no JSON-shaped serialization anywhere in the
codebase; this DTO is a **schema-compatibility** DTO over an input
*dict* representation of a session (the fields a caller would extract from
a `JudgeSession` to serialize it), not a live-object adapter.

`LegacySessionDTO` fields: `session_id: str`, `baseline_id: str`,
`created_at: str`, `durable_version: int`, `work: Dict[str, Any]` (carried
as a raw dict — this DTO does not decode `work`'s values through
`app/api/codec.py`, which is a separate session-storage concern),
`source_revision_id: Optional[str]`, `knowledge_revision_id: Optional[str]`
(an extension carrying the full source/KM revisions),
`unknown_fields: Dict[str, Any]` (same PRESERVE policy as §8.1).

**Explicit, not inferred:** `source_revision_id`/`knowledge_revision_id`
must be present as explicit keys in the input dict (an explicit `None` value
is accepted — "not yet pinned" is a real state — but the KEY must exist).
`JudgeSession` carries no source/KM revision pointer of its own (only
`overlay`/`lineage`/`work`); this decoder does **not** invent one by
reading `work["reset_epoch"]`, `SessionRegistry._counter`, or any other
counter-shaped value as a stand-in. A caller who has no real source/KM
revision to report must pass `None` explicitly, not omit the key.

Missing a required key (any of `session_id`, `baseline_id`, `created_at`,
`durable_version`, `work`, `source_revision_id`, `knowledge_revision_id`)
raises `KeyError`, mirroring `decode_legacy_knowledge_record`'s
`derived_from` gate.

## 9. Migration notes

- Legacy `EvidenceRef` (`core/change_intelligence/evidence.py`: `artifact`,
  `locator`, `quote`, `snapshot_id`) is **not** the same type as v2's
  `EvidenceRef` in `schema.py` (which additionally requires
  `source_revision_id`, `artifact_revision_id`, `content_hash`,
  `locator_type`, `parser_version`). No automatic converter between the
  two exists; `compat.py` only decodes the legacy `KnowledgeRecord`
  top-level shape, embedding the legacy evidence dicts as-is (not
  re-typed into v2 `EvidenceRef`), as pinned by
  `test_decode_legacy_knowledge_record_evidence_items_stay_as_raw_legacy_dicts`.
- No production caller imports `core/change_intelligence/v2/*` yet; the
  modules are exercised by their acceptance tests.

## 10. Known limitations

1. State-machine *transition* enforcement is not implemented by any type in
   this module; only a `lifecycle_state`/`status` enum-membership check plus,
   in a few cases, a directly-named contract invariant (e.g.
   `VerificationReport`'s C15 check, and `Rule.decision_ids` being non-empty
   when `confirmation_state == "CONFIRMED"`). Transitions are defined in
   `AI_CLOSED_LOOP_V2_LIFECYCLE_API.md`.
2. `Relation` (the confirmed counterpart of `RelationCandidate`) is modelled
   as a subclass with `lifecycle_state="CONFIRMED"` plus a `decision_id`,
   rather than a fully independent type with its own ancestry-merge/redirect
   semantics (contracts §2.1: merge/split is an event with redirect/version
   that does not lose ancestry). `Rule` has this ancestry/redirect schema room
   (§3a); `Relation` is not symmetrically extended.
3. `Rule.confirmation_state`'s enum set (`DRAFT`/`VALIDATED`/`PENDING_HUMAN`/
   `CONFIRMED`/`REJECTED`/`SUPERSEDED`) is a design choice, not derived from
   an explicit contract enumeration — contracts §2.1 only says "confirmation
   state" in prose, with no spelled-out closed set (the same holds for the
   `applicability`/`modality` sets).
4. `Rule`'s merge/split ancestry fields (`superseded_by`, `merged_from`,
   `split_from`) are schema-only: nothing in `schema.py` enforces that a
   merge/split actually rewires these fields consistently across the old and
   new `Rule` records (e.g. that a `superseded_by` target actually exists, or
   that its own `merged_from`/`split_from` correctly reciprocates). That
   cross-object consistency is a lifecycle/transition concern, not a
   single-object structural validation this schema layer performs.
