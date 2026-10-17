# -*- coding: utf-8 -*-
"""Versioned v2 schemas: dataclasses + validators.

Generic domain types only. Contracts §2.1/§2.2 (data-class tables), §1
C01-C05, C11, C15, C17. No business-specific (Harbor or otherwise) literals
belong in this module's validation logic — Harbor values are TEST fixture
data only (see `core/tests/test_ai1_t01_schema.py`,
`test_schema_module_has_no_harbor_specific_identifiers`).

The schema surface covers `Observation`,
`RelationCandidate`/`Relation`, `KnowledgeRevision`, `Finding`,
`HumanDecision` (contracts §2.1), and `AnalysisRun`, `PatchProposal`,
`Approval`, `VerificationReport`, `RecoveryProposal` (contracts §2.2), with
strict validation (structural fields, enums) of `ArtifactRevision`,
`SourceRevision`, and `RuleCandidate`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence, Tuple

from core.kernel.serialize import canonical_dumps


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """A structural/domain validation failure in a v2 schema type."""


class SchemaVersionError(ValidationError):
    """An explicitly unsupported or unrecognised `schema_version` value."""


# ---------------------------------------------------------------------------
# Schema version gate
# ---------------------------------------------------------------------------

#: Versions this build of the v2 schema module accepts. Adding a version
#: here is the only sanctioned way to widen acceptance; nothing coerces an
#: unknown version to the nearest known one.
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0.0"})

#: Sentinel used when a caller does not pass `applicability_reason` at all.
#: `None` is falsy/rejectable, unlike a non-blank placeholder string, which
#: would make a fully-omitted `applicability_reason` argument
#: indistinguishable from a real reason, silently accepting bare `UNKNOWN`
#: with no reason at all (see
#: `core/tests/test_ai1_t01_schema.py::
#: test_unknown_applicability_reason_kwarg_fully_omitted_is_rejected`).
#: Omission, explicit `""`, and explicit whitespace-only are now all
#: rejected uniformly by the same `not reason.strip()`-shaped check below.
_UNSPECIFIED_REASON = None


def _require_schema_version(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(
            f"schema_version must be a str, got {type(value).__name__!r}")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaVersionError(
            f"unsupported schema_version {value!r}; supported: "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}")
    return value


def _require_nonempty_str(obj: Any, name: str) -> None:
    value = getattr(obj, name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{type(obj).__name__}.{name} must be a non-empty str")


def _require_enum(obj: Any, name: str, allowed) -> None:
    value = getattr(obj, name)
    if value not in allowed:
        raise ValidationError(
            f"{type(obj).__name__}.{name} {value!r} not in {sorted(allowed)}")


def _require_mapping(obj: Any, name: str, *, allow_empty: bool = False) -> None:
    value = getattr(obj, name)
    if not isinstance(value, dict):
        raise ValidationError(f"{type(obj).__name__}.{name} must be a dict")
    if not allow_empty and not value:
        raise ValidationError(f"{type(obj).__name__}.{name} must be a non-empty dict")


def _require_sequence(obj: Any, name: str) -> None:
    value = getattr(obj, name)
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{type(obj).__name__}.{name} must be a list/tuple")


# A local or date-only time cannot determine an instant without a timezone.
_AWARE_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def _require_aware_timestamp(obj: Any, name: str) -> datetime:
    value = getattr(obj, name)
    if not isinstance(value, str) or not _AWARE_TIMESTAMP.fullmatch(value):
        raise ValidationError(
            f"{type(obj).__name__}.{name} must be an ISO 8601 timestamp "
            "with seconds and an explicit timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"{type(obj).__name__}.{name} is not a valid timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValidationError(f"{type(obj).__name__}.{name} requires a timezone")
    return parsed


def _require_effective_interval(obj: Any) -> None:
    for name in ("effective_start_inclusive", "effective_end_inclusive"):
        if type(getattr(obj, name)) is not bool:
            raise ValidationError(f"{type(obj).__name__}.{name} must be bool")
    start = (_require_aware_timestamp(obj, "effective_start")
             if obj.effective_start is not None else None)
    end = (_require_aware_timestamp(obj, "effective_end")
           if obj.effective_end is not None else None)
    if start is not None and end is not None:
        if end < start or (end == start and not (
                obj.effective_start_inclusive and obj.effective_end_inclusive)):
            raise ValidationError(
                f"{type(obj).__name__} effective interval is reversed or empty")


# ---------------------------------------------------------------------------
# ManifestEntry / manifest content hash ("manifest_hash" must bind
# ARTIFACT CONTENT, not bare artifact-revision-ID strings, and the pre-hash
# encoding must be injective)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    """One line of a `SourceRevision.manifest`: WHICH artifact (`artifact_id`
    identity, stable across renames), WHICH specific revision of it
    (`artifact_revision_id`, for traceability/lookup), and its immutable
    CONTENT descriptor (`blob_hash`, copied from the matching
    `ArtifactRevision.blob_hash`). This replaces a bare
    `str` artifact-revision-id shape: a manifest of bare IDs cannot express "these bytes", only
    "this revision label", so a byte-identical restore under a new revision
    ID changed the hash and changed bytes under a stable ID were invisible.

    `blob_hash` is the field the content hash is computed from (see
    `_compute_manifest_hash` below); `artifact_revision_id` is carried for
    traceability/lookup only and does NOT participate in the digest --
    see the identity-vs-content decision documented on
    `_compute_manifest_hash`.
    """

    artifact_id: str
    artifact_revision_id: str
    blob_hash: str

    def __post_init__(self) -> None:
        for name in ("artifact_id", "artifact_revision_id", "blob_hash"):
            _require_nonempty_str(self, name)


def _compute_manifest_hash(manifest: Sequence[ManifestEntry]) -> str:
    """Deterministic CONTENT hash over a SourceRevision's sorted manifest.

    Canonical content input (documented, chosen scheme): the manifest is a sorted tuple of `ManifestEntry` objects,
    contracts §2.1's "sorted manifest artifact revisions" now carrying each
    entry's immutable `ArtifactRevision.blob_hash`, not just a bare
    revision-id string. The pre-hash text is
    `canonical_dumps([{"artifact_id": e.artifact_id, "blob_hash": e.blob_hash}
    for e in manifest])` -- `core/kernel/serialize.py::canonical_dumps`
    (sorted-key, separator-free JSON with explicit quoting/escaping of every
    field) -- then `"sha256:" + sha256(hex)` of that text. Using a canonical
    JSON array of explicitly-keyed objects is INJECTIVE: JSON's own string
    escaping gives every field an unambiguous boundary, so no two distinct
    `(artifact_id, blob_hash)` sequences can ever serialize to the same
    text (unlike a bare `"\\n".join(...)`, where a
    probe proved `_compute_manifest_hash(('a\\nb',)) ==
    _compute_manifest_hash(('a', 'b'))`). Concretely, with this fix, a
    one-entry manifest whose `artifact_id` embeds a literal newline
    (`"a\\nb"`) serializes as the JSON string `"a\\nb"` (backslash-n
    ESCAPE SEQUENCE, two ASCII bytes) inside its own `{"artifact_id":
    "...", ...}` object, which is structurally a ONE-ELEMENT JSON array;
    the corresponding two-entry manifest (`"a"`, `"b"`) serializes as a
    TWO-ELEMENT JSON array with two full `{"artifact_id": ..., "blob_hash":
    ...}` objects -- the two texts differ both in bracket/brace structure
    and in byte content, so they hash differently. (This is a stronger fix
    than merely "the counterexample cannot be represented any more": the
    old counterexample's `artifact_id` value CAN still be represented here,
    it simply no longer collides, because `canonical_dumps` escapes the
    embedded newline instead of treating it as a bare, un-escaped
    delimiter.)

    Identity-vs-content decision: the digest input is built ONLY from
    `artifact_id` + `blob_hash` -- `artifact_revision_id` does NOT
    participate. Therefore a new `ArtifactRevision.revision_id` minted for
    BYTE-IDENTICAL content (same `blob_hash`) under the same `artifact_id`
    REUSES the same `manifest_hash`; only a real content change
    (`blob_hash` differs) or a different artifact (`artifact_id` differs)
    changes the digest. This matches contracts §2.1's content-identity
    intent ('Source hash phân biệt event ID'): the hash tracks WHAT is
    there, not which revision label currently points at it.

    A SourceRevision contains at most one active revision per artifact ID.
    This makes sorting by artifact ID independent of revision labels.
    Restoring identical manifest content (same sorted `(artifact_id,
    blob_hash)` set) always reproduces this SAME digest, regardless of the
    `SourceRevision.id` (event/transaction identity) or the
    `artifact_revision_id`s used to restore it -- `id` and `manifest_hash`
    remain independently computed/verified."""
    content = [
        {"artifact_id": entry.artifact_id, "blob_hash": entry.blob_hash}
        for entry in manifest
    ]
    text = canonical_dumps(content)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Money — Decimal-typed, currency-scoped
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Money:
    """Decimal/typed money value. Contracts §2.1: 'Money dùng decimal/minor
    units theo currency; không float equality'.

    Cross-currency semantics (coordinator note, binding): this type does
    NOT implement `__add__`/arithmetic across currencies at all. Comparing
    two `Money` values of different currencies uses the dataclass's
    generated `__eq__`, which compares `(amount, currency)` as a tuple, so
    two values with the same numeric amount but different currencies
    compare UNEQUAL — never a silent, currency-blind numeric equality.
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(
                f"Money.amount must be a Decimal, got {type(self.amount).__name__!r} "
                "(float input is rejected to avoid float-precision corruption)")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValidationError("Money.currency must be a non-empty str")
        if self.amount < 0:
            raise ValidationError(
                f"Money.amount must not be negative, got {self.amount!r}")


# ---------------------------------------------------------------------------
# EvidenceRef
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """A single evidence citation: source/artifact revision + locator +
    quote + parser identity. Contracts §2.1: 'Snapshot/hash phải đọc được,
    không locator-only' -- a locator is required so the quote can be
    re-checked against real bytes.
    """

    source_revision_id: str
    artifact_revision_id: str
    content_hash: str
    locator_type: str
    locator: str
    quote: str
    parser_version: str

    def __post_init__(self) -> None:
        for name in ("source_revision_id", "artifact_revision_id", "content_hash",
                     "locator_type", "locator", "parser_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"EvidenceRef.{name} must be a non-empty str")


# ---------------------------------------------------------------------------
# ArtifactRevision
# ---------------------------------------------------------------------------

#: Contracts §2.1 fixed set for ArtifactRevision.source_role.
SOURCE_ROLES = frozenset({"normative", "implementation", "observational", "test", "unknown"})

#: Contracts §2.1 ArtifactRevision row adds "provenance". Distinguishes an artifact revision that was
#: uploaded/imported directly from a source connector (`original_upload`),
#: one produced by a scripted/AI transformation of another artifact
#: (`derived`), or one recreated by a recovery/rollback restore
#: (`restored`) -- matching contracts §2.2's general provenance vocabulary
#: (C02: "nguồn thật, quyết định human và giả định mô phỏng là ba
#: provenance khác nhau") applied to the artifact-revision row.
ARTIFACT_PROVENANCE_VALUES = frozenset({"original_upload", "derived", "restored"})


@dataclass(frozen=True)
class ArtifactRevision:
    """A specific revision of an artifact. `artifact_id` is stable across
    renames (identity); `revision_id` names this specific revision.
    Contracts §2.1: 'Đổi tên giữ identity khi có bằng chứng'."""

    artifact_id: str
    revision_id: str
    logical_path: str
    media_type: str
    blob_hash: str
    byte_size: int
    parser_version: str
    source_role: str
    #: contracts §2.1 "provenance" -- defaults to `original_upload` so
    #: existing call sites written before this field existed keep
    #: constructing valid objects; an explicit, unrecognised spelling is
    #: still rejected by the enum gate below.
    provenance: str = "original_upload"
    #: Uniform versioning strategy: every domain type in this module now carries its own
    #: `schema_version` field, gated the same way as the types that already
    #: had one (`_require_schema_version`). Defaults to the current
    #: supported version so existing call sites that predate this field
    #: keep constructing valid objects; an explicit unsupported/malformed
    #: value is still rejected.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("artifact_id", "revision_id", "logical_path", "media_type",
                     "blob_hash", "parser_version"):
            _require_nonempty_str(self, name)
        if not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool) \
                or self.byte_size < 0:
            raise ValidationError(
                f"ArtifactRevision.byte_size must be a non-negative int, got "
                f"{self.byte_size!r}")
        _require_enum(self, "source_role", SOURCE_ROLES)
        _require_enum(self, "provenance", ARTIFACT_PROVENANCE_VALUES)


# ---------------------------------------------------------------------------
# SourceRevision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRevision:
    """A history event over a manifest of artifact revisions. `id` is the
    event/transaction identity; `manifest_hash` is a content hash over the
    manifest bytes. These are STRUCTURALLY distinct fields on purpose
    (contracts §2.1: the source hash is distinct from the event ID):
    restoring identical bytes must reproduce the same `manifest_hash` while
    still minting a brand-new `id`.

    `manifest_hash` is not a trust-checked, caller-asserted string. It is
    VERIFIED against an internally recomputed digest
    (`_compute_manifest_hash`, defined above). A caller who passes a
    `manifest_hash` that does not match this recomputation gets
    `ValidationError` -- a forged/wrong digest is REJECTED, not silently
    accepted (this keeps the "verify, don't silently overwrite"
    choice; see `_compute_manifest_hash`'s docstring for why -- it remains
    the strictest option and gives the same rejection behaviour for a
    forged OR a malformed digest, with no extra code).

    `manifest` is not a tuple of bare artifact-revision-id strings; it is a
    sorted `Tuple[ManifestEntry, ...]` (see `ManifestEntry` above), and
    `manifest_hash` binds each entry's immutable `blob_hash` (content), not
    just its `artifact_revision_id` (identity) -- see
    `_compute_manifest_hash`'s docstring for the exact canonical encoding,
    the injectivity proof, and the identity-vs-content decision (same
    `blob_hash` under a new `artifact_revision_id` reuses the digest;
    a real content or artifact-identity change does not). Restoring
    identical manifest content always reproduces the identical
    `manifest_hash` (the recomputation is a pure function of `manifest`
    alone), while `id` is free to be a brand-new event identity every time
    -- the two fields remain independent by construction, exactly as
    `test_source_hash_differs_structurally_from_event_id` requires.
    """

    id: str
    parent_id: Optional[str]
    manifest: Tuple[ManifestEntry, ...]
    manifest_hash: str
    created_by: str
    created_at: str
    trigger: str
    restored_from: Optional[str]
    #: contracts §2.1 "source connector/import ID" -- identifies which source connector/
    #: importer produced this event. Optional/nullable because not every
    #: history event originates from an external connector (e.g. a
    #: restore's trigger is `"restore"`, not a fresh import).
    source_connector_id: Optional[str] = None
    #: Uniform versioning strategy -- see `ArtifactRevision.schema_version` docstring.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "manifest_hash", "created_by", "created_at", "trigger"):
            _require_nonempty_str(self, name)
        _require_aware_timestamp(self, "created_at")
        if not isinstance(self.manifest, (list, tuple)) or not self.manifest:
            raise ValidationError(
                "SourceRevision.manifest must be a non-empty list/tuple of "
                "ManifestEntry objects")
        for entry in self.manifest:
            if not isinstance(entry, ManifestEntry):
                raise ValidationError(
                    "SourceRevision.manifest entries must be ManifestEntry "
                    f"instances, got {type(entry).__name__!r}")
        artifact_ids = [entry.artifact_id for entry in self.manifest]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValidationError(
                "SourceRevision.manifest must contain at most one active "
                "revision per artifact_id")
        sort_key = lambda e: (e.artifact_id, e.artifact_revision_id)
        if tuple(sorted(self.manifest, key=sort_key)) != tuple(self.manifest):
            raise ValidationError(
                "SourceRevision.manifest must be sorted by "
                "(artifact_id, artifact_revision_id) (contracts §2.1: "
                "'sorted manifest artifact revisions')")
        expected_hash = _compute_manifest_hash(self.manifest)
        if self.manifest_hash != expected_hash:
            raise ValidationError(
                "SourceRevision.manifest_hash does not match the recomputed "
                f"content hash of manifest; expected {expected_hash!r}, got "
                f"{self.manifest_hash!r} (a forged/wrong digest is rejected, "
                "not trusted)")
        if self.source_connector_id is not None and (
                not isinstance(self.source_connector_id, str)
                or not self.source_connector_id.strip()):
            raise ValidationError(
                "SourceRevision.source_connector_id must be None or a "
                "non-empty str")


# ---------------------------------------------------------------------------
# RuleCandidate
# ---------------------------------------------------------------------------

#: Contracts §2.1 RuleCandidate row: "modality (must/may/observed)".
MODALITY_VALUES = frozenset({"must", "may", "observed"})

#: This module's two-value applicability model: a candidate's applicability
#: is either fully RECOGNIZED (expressed via `scope`) or explicitly
#: UNKNOWN (requiring `applicability_reason`/a GAP reference). No other
#: spelling is accepted -- an adversarial value
#: (`applicability='BOGUS'`) must raise `ValidationError`, not pass
#: through silently.
APPLICABILITY_VALUES = frozenset({"RECOGNIZED", "UNKNOWN"})

# Strict and inclusive comparisons have distinct serialized spellings.
COMPARISON_OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!="})


@dataclass(frozen=True)
class RuleCandidate:
    """A candidate rule extracted from a source, with mandatory scope and
    evidence that must cite the SAME source/artifact revision the
    candidate itself claims to come from. Contracts §2.1 RuleCandidate row:
    "candidate_id, subject, predicate/operator, typed value, currency/unit,
    applicability expression, exceptions, outcome/actor, modality
    (must/may/observed), effective interval, unknown_fields,
    evidence_support/contradiction, model_run_id".
    """

    candidate_id: str
    subject: str
    predicate: str
    value: Any
    applicability: str
    modality: str
    source_revision_id: str
    artifact_revision_id: str
    evidence: Sequence[EvidenceRef]
    scope: Mapping[str, Any]
    schema_version: str
    applicability_reason: Optional[str] = _UNSPECIFIED_REASON
    #: contracts §2.1: "predicate/operator" — `predicate` is the existing,
    #: already-tested field; `operator` is an additional, optional typed
    #: comparison operator (e.g. ">", ">=", "==") when the predicate needs
    #: one. Not required because not every predicate is a comparison.
    operator: Optional[str] = None
    #: contracts §2.1: "currency/unit" — a unit label for `value` when
    #: `value` is not itself a `Money` (e.g. a `Money` already carries its
    #: own `currency`; a plain-scalar `value` uses this field instead).
    unit: Optional[str] = None
    exceptions: Tuple[str, ...] = ()
    outcome: Optional[str] = None
    actor: Optional[str] = None
    effective_start: Optional[str] = None
    effective_end: Optional[str] = None
    effective_start_inclusive: bool = True
    effective_end_inclusive: bool = False
    #: Fields present in the raw upstream input that this schema does not
    #: (yet) have a typed slot for. Preserved rather than silently dropped,
    #: per the same unknown-field policy as `compat.py`.
    unknown_fields: Mapping[str, Any] = field(default_factory=dict)
    evidence_support: Tuple[EvidenceRef, ...] = ()
    evidence_contradiction: Tuple[EvidenceRef, ...] = ()
    model_run_id: Optional[str] = None

    def __post_init__(self) -> None:
        # schema_version gate first: an unsupported/malformed version is
        # rejected before any other structural check runs.
        _require_schema_version(self.schema_version)

        _require_enum(self, "applicability", APPLICABILITY_VALUES)
        _require_enum(self, "modality", MODALITY_VALUES)
        if self.operator is not None:
            if not isinstance(self.operator, str) or self.operator not in COMPARISON_OPERATORS:
                raise ValidationError(
                    "RuleCandidate.operator must be one of "
                    f"{sorted(COMPARISON_OPERATORS)}")
        _require_effective_interval(self)

        # scope: required, typed as a non-empty mapping. Never defaults to
        # "applies to all" -- there is no default at all (dataclass field
        # has none), and a wrong-typed or empty scope is rejected here.
        if not isinstance(self.scope, dict) or not self.scope:
            raise ValidationError(
                "RuleCandidate.scope must be a non-empty dict; missing/empty/"
                "wrong-typed scope is never treated as 'applies to all'")

        if not isinstance(self.unknown_fields, dict):
            raise ValidationError("RuleCandidate.unknown_fields must be a dict")

        # Cross-revision evidence check: every evidence ref (in `evidence`,
        # `evidence_support`, and `evidence_contradiction`) must cite the
        # SAME source/artifact revision as this candidate's own claim.
        for group_name in ("evidence", "evidence_support", "evidence_contradiction"):
            for ref in getattr(self, group_name):
                if ref.source_revision_id != self.source_revision_id:
                    raise ValidationError(
                        f"EvidenceRef.source_revision_id {ref.source_revision_id!r} "
                        f"!= RuleCandidate.source_revision_id {self.source_revision_id!r}: "
                        "cross-revision evidence is invalid")
                if ref.artifact_revision_id != self.artifact_revision_id:
                    raise ValidationError(
                        f"EvidenceRef.artifact_revision_id {ref.artifact_revision_id!r} "
                        f"!= RuleCandidate.artifact_revision_id {self.artifact_revision_id!r}: "
                        "cross-revision evidence is invalid")

        # UNKNOWN applicability requires a real, non-blank reason/GAP ref.
        if self.applicability == "UNKNOWN":
            reason = self.applicability_reason
            if reason is None or not isinstance(reason, str) or not reason.strip():
                raise ValidationError(
                    "RuleCandidate.applicability_reason must be a non-blank "
                    "reason/GAP reference when applicability is UNKNOWN "
                    "(omitted, None, empty, and whitespace-only are all rejected)")


# ---------------------------------------------------------------------------
# Observation (contracts §2.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """"id, source_revision, artifact_revision, kind, structured data,
    evidence refs; script tạo." An observation is a scripted extraction; it
    is NOT itself a confirmed fact (contracts §2.1: OCR/AI extraction is an
    uncertain inference until verified).
    """

    id: str
    source_revision_id: str
    artifact_revision_id: str
    kind: str
    data: Mapping[str, Any]
    evidence: Sequence[EvidenceRef]
    schema_version: str

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "source_revision_id", "artifact_revision_id", "kind"):
            _require_nonempty_str(self, name)
        _require_mapping(self, "data", allow_empty=True)
        _require_sequence(self, "evidence")
        for ref in self.evidence:
            if ref.source_revision_id != self.source_revision_id:
                raise ValidationError(
                    "Observation evidence must cite the same source_revision_id")
            if ref.artifact_revision_id != self.artifact_revision_id:
                raise ValidationError(
                    "Observation evidence must cite the same artifact_revision_id")


# ---------------------------------------------------------------------------
# RelationCandidate / Relation (edge, contracts §2.1)
# ---------------------------------------------------------------------------

#: Direction of a relation between two entity revisions.
RELATION_DIRECTIONS = frozenset({"a_to_b", "b_to_a", "bidirectional"})

#: Contracts §3 Candidate state machine, reused for relation lifecycle.
RELATION_LIFECYCLE_STATES = frozenset(
    {"PROPOSED", "VALIDATED", "PENDING_HUMAN", "CONFIRMED", "REJECTED"})


@dataclass(frozen=True)
class RelationCandidate:
    """"from/to entity revisions, relation type, direction, scope/
    conditions, supporting/refuting evidence, derivation method, lifecycle.
    Hai endpoint tồn tại chưa đủ xác nhận edge." Two existing endpoints do
    NOT by themselves confirm the edge -- `lifecycle_state` starts at
    `PROPOSED`, never `CONFIRMED`, by construction (see validation below).
    """

    candidate_id: str
    from_entity_revision_id: str
    to_entity_revision_id: str
    relation_type: str
    direction: str
    scope: Mapping[str, Any]
    evidence_support: Sequence[EvidenceRef]
    evidence_contradiction: Sequence[EvidenceRef]
    derivation_method: str
    lifecycle_state: str
    schema_version: str

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("candidate_id", "from_entity_revision_id", "to_entity_revision_id",
                     "relation_type", "derivation_method"):
            _require_nonempty_str(self, name)
        _require_enum(self, "direction", RELATION_DIRECTIONS)
        _require_enum(self, "lifecycle_state", RELATION_LIFECYCLE_STATES)
        _require_mapping(self, "scope", allow_empty=False)
        _require_sequence(self, "evidence_support")
        _require_sequence(self, "evidence_contradiction")


#: Relation is the confirmed counterpart of RelationCandidate; same shape
#: plus a stable relation_id/version and decision provenance, mirroring
#: RuleCandidate -> Rule. No separate acceptance test names a distinct
#: `Rule`-analogue for edges; `RelationCandidate` with
#: `lifecycle_state="CONFIRMED"` plus a `decision_id` models a confirmed
#: relation without duplicating validation logic in a second class.
@dataclass(frozen=True)
class Relation(RelationCandidate):
    """A confirmed `RelationCandidate`: additionally carries the
    `HumanDecision` reference that confirmed it."""

    decision_id: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.lifecycle_state != "CONFIRMED":
            raise ValidationError(
                "Relation.lifecycle_state must be CONFIRMED (use "
                "RelationCandidate for PROPOSED/VALIDATED/PENDING_HUMAN/REJECTED)")
        _require_nonempty_str(self, "decision_id")


# ---------------------------------------------------------------------------
# KnowledgeRevision (KM, contracts §2.1)
# ---------------------------------------------------------------------------

KNOWLEDGE_LIFECYCLE_STATES = frozenset(
    {"DRAFT", "VALIDATED", "PENDING_HUMAN", "ACCEPTED", "STALE", "SUPERSEDED"})


@dataclass(frozen=True)
class KnowledgeRevision:
    """"id, parent_id, source_revision, decision_epoch, schema/parser/
    prompt/model config versions, rules/edges/findings refs, coverage
    manifest, content_hash, lifecycle, accepted_by/at nullable."""

    id: str
    parent_id: Optional[str]
    source_revision_id: str
    decision_epoch: int
    parser_version: str
    prompt_version: str
    model_config_version: str
    rule_ids: Tuple[str, ...]
    edge_ids: Tuple[str, ...]
    finding_ids: Tuple[str, ...]
    coverage_manifest: Mapping[str, Any]
    content_hash: str
    lifecycle_state: str
    schema_version: str
    accepted_by: Optional[str] = None
    accepted_at: Optional[str] = None

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "source_revision_id", "parser_version", "prompt_version",
                     "model_config_version", "content_hash"):
            _require_nonempty_str(self, name)
        if not isinstance(self.decision_epoch, int) or isinstance(self.decision_epoch, bool) \
                or self.decision_epoch < 0:
            raise ValidationError(
                "KnowledgeRevision.decision_epoch must be a non-negative int")
        _require_enum(self, "lifecycle_state", KNOWLEDGE_LIFECYCLE_STATES)
        _require_mapping(self, "coverage_manifest", allow_empty=True)
        for name in ("rule_ids", "edge_ids", "finding_ids"):
            _require_sequence(self, name)
        if self.lifecycle_state == "ACCEPTED" and not (self.accepted_by and self.accepted_at):
            raise ValidationError(
                "KnowledgeRevision.accepted_by/accepted_at are required when "
                "lifecycle_state is ACCEPTED")


# ---------------------------------------------------------------------------
# Finding (contracts §2.1)
# ---------------------------------------------------------------------------

FINDING_TYPES = frozenset({"GAP", "CONFLICT", "DRIFT"})
FINDING_SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
#: Union of the Gap and Conflict state machines (contracts §3).
FINDING_LIFECYCLE_STATES = frozenset({
    "OPEN", "ANSWER_PROPOSED", "RESOLVED", "ACCEPTED_UNKNOWN", "REOPENED",
    "DECISION_RECORDED", "REMEDIATION_PENDING", "VERIFIED_RESOLVED",
    "EXCEPTION_ACCEPTED",
})


@dataclass(frozen=True)
class Finding:
    """"type GAP/CONFLICT/DRIFT, subject/version, involved claims, missing
    fields, severity, impact scope, authority required, lifecycle,
    resolution decision, residual remediation action."""

    id: str
    finding_type: str
    subject_id: str
    subject_version: str
    involved_claim_ids: Tuple[str, ...]
    missing_fields: Tuple[str, ...]
    severity: str
    impact_scope: Mapping[str, Any]
    authority_required: str
    lifecycle_state: str
    schema_version: str
    resolution_decision_id: Optional[str] = None
    residual_remediation_action: Optional[str] = None

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "subject_id", "subject_version", "authority_required"):
            _require_nonempty_str(self, name)
        _require_enum(self, "finding_type", FINDING_TYPES)
        _require_enum(self, "severity", FINDING_SEVERITIES)
        _require_enum(self, "lifecycle_state", FINDING_LIFECYCLE_STATES)
        _require_mapping(self, "impact_scope", allow_empty=True)
        for name in ("involved_claim_ids", "missing_fields"):
            _require_sequence(self, name)


# ---------------------------------------------------------------------------
# HumanDecision (contracts §2.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HumanDecision:
    """"actor từ auth, quyền/policy tại thời điểm duyệt, target +
    expected_version, action, answer/rationale, evidence, scope/effective
    interval, created_at, supersedes/revokes nullable; append-only."""

    id: str
    actor: str
    permissions_snapshot: Tuple[str, ...]
    policy_version: str
    target_id: str
    expected_version: int
    action: str
    rationale: str
    evidence: Tuple[EvidenceRef, ...]
    scope: Mapping[str, Any]
    created_at: str
    answer: Optional[str] = None
    effective_start: Optional[str] = None
    effective_end: Optional[str] = None
    effective_start_inclusive: bool = True
    effective_end_inclusive: bool = False
    supersedes: Optional[str] = None
    revokes: Optional[str] = None
    #: Uniform versioning strategy -- see
    #: `ArtifactRevision.schema_version` docstring.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "actor", "policy_version", "target_id", "action",
                     "rationale", "created_at"):
            _require_nonempty_str(self, name)
        if not isinstance(self.expected_version, int) or isinstance(self.expected_version, bool) \
                or self.expected_version < 0:
            raise ValidationError(
                "HumanDecision.expected_version must be a non-negative int")
        _require_sequence(self, "permissions_snapshot")
        _require_sequence(self, "evidence")
        _require_mapping(self, "scope", allow_empty=True)
        _require_aware_timestamp(self, "created_at")
        _require_effective_interval(self)


# ---------------------------------------------------------------------------
# AnalysisRun (contracts §2.2)
# ---------------------------------------------------------------------------

ANALYSIS_RUN_STATUSES = frozenset(
    {"QUEUED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"})


@dataclass(frozen=True)
class AnalysisRun:
    """"workspace, pinned source/KM/decision versions, purpose, coverage/
    omissions, status, provider calls/usage, prompt/schema digests,
    inputs/outputs hashes, attempts, timestamps/errors."""

    id: str
    workspace_id: str
    pinned_source_revision_id: str
    pinned_knowledge_revision_id: Optional[str]
    pinned_decision_epoch: int
    purpose: str
    coverage: Mapping[str, Any]
    omissions: Tuple[str, ...]
    status: str
    provider_calls: Tuple[Mapping[str, Any], ...]
    prompt_digest: str
    schema_digest: str
    input_hash: str
    output_hash: Optional[str]
    attempts: int
    created_at: str
    errors: Tuple[str, ...]
    #: Uniform versioning strategy -- see
    #: `ArtifactRevision.schema_version` docstring.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "workspace_id", "pinned_source_revision_id", "purpose",
                     "prompt_digest", "schema_digest", "input_hash", "created_at"):
            _require_nonempty_str(self, name)
        _require_aware_timestamp(self, "created_at")
        if not isinstance(self.pinned_decision_epoch, int) or isinstance(
                self.pinned_decision_epoch, bool) or self.pinned_decision_epoch < 0:
            raise ValidationError(
                "AnalysisRun.pinned_decision_epoch must be a non-negative int")
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) \
                or self.attempts < 0:
            raise ValidationError("AnalysisRun.attempts must be a non-negative int")
        _require_enum(self, "status", ANALYSIS_RUN_STATUSES)
        _require_mapping(self, "coverage", allow_empty=True)
        for name in ("omissions", "provider_calls", "errors"):
            _require_sequence(self, name)


# ---------------------------------------------------------------------------
# PatchProposal (contracts §2.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchProposal:
    """"target revisions, approved-path candidates, typed edits/unified
    diff, before/after blob hashes, action classification, rationale/
    evidence, generated_by, manual work, dependency DAG, no-op detection."""

    id: str
    target_source_revision_id: str
    target_knowledge_revision_id: str
    approved_candidate_ids: Tuple[str, ...]
    edits: Tuple[Mapping[str, Any], ...]
    before_blob_hash: str
    after_blob_hash: str
    action_classification: str
    rationale: str
    evidence: Tuple[EvidenceRef, ...]
    generated_by: str
    manual_work_required: bool
    dependency_ids: Tuple[str, ...]
    is_noop: bool
    schema_version: str

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "target_source_revision_id", "target_knowledge_revision_id",
                     "before_blob_hash", "after_blob_hash", "action_classification",
                     "rationale", "generated_by"):
            _require_nonempty_str(self, name)
        if not isinstance(self.manual_work_required, bool):
            raise ValidationError("PatchProposal.manual_work_required must be a bool")
        if not isinstance(self.is_noop, bool):
            raise ValidationError("PatchProposal.is_noop must be a bool")
        for name in ("approved_candidate_ids", "edits", "evidence", "dependency_ids"):
            _require_sequence(self, name)


# ---------------------------------------------------------------------------
# Approval (contracts §2.2)
# ---------------------------------------------------------------------------

APPROVAL_DECISIONS = frozenset({"APPROVE", "REJECT", "REQUEST_CHANGES"})


@dataclass(frozen=True)
class Approval:
    """"subject type/id, exact composite digest, human actor, permissions,
    decision/reason, expiry/policy, source/KM/decision versions, review/
    report hashes."""

    id: str
    subject_type: str
    subject_id: str
    composite_digest: str
    actor: str
    permissions_snapshot: Tuple[str, ...]
    decision: str
    reason: str
    policy_version: str
    source_revision_id: str
    knowledge_revision_id: Optional[str]
    decision_epoch: int
    review_bundle_hash: str
    created_at: str
    expiry: Optional[str] = None
    #: Uniform versioning strategy -- see
    #: `ArtifactRevision.schema_version` docstring.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "subject_type", "subject_id", "composite_digest", "actor",
                     "reason", "policy_version", "source_revision_id",
                     "review_bundle_hash", "created_at"):
            _require_nonempty_str(self, name)
        _require_aware_timestamp(self, "created_at")
        _require_enum(self, "decision", APPROVAL_DECISIONS)
        if not isinstance(self.decision_epoch, int) or isinstance(self.decision_epoch, bool) \
                or self.decision_epoch < 0:
            raise ValidationError("Approval.decision_epoch must be a non-negative int")
        _require_sequence(self, "permissions_snapshot")


# ---------------------------------------------------------------------------
# VerificationReport (contracts §2.2)
# ---------------------------------------------------------------------------

VERIFICATION_STATUSES = frozenset({"PASSED", "FAILED", "PARTIAL", "NOT_RUN"})


@dataclass(frozen=True)
class VerificationReport:
    """"expected/actual/unexpected/missing edits, parsing/tests/simulation/
    evidence results, open required work, scope/coverage, after source/KM
    versions, status/reasons."""

    id: str
    expected_edits: Tuple[Mapping[str, Any], ...]
    actual_edits: Tuple[Mapping[str, Any], ...]
    unexpected_edits: Tuple[Mapping[str, Any], ...]
    missing_edits: Tuple[Mapping[str, Any], ...]
    parsing_result: Mapping[str, Any]
    tests_result: Mapping[str, Any]
    simulation_result: Mapping[str, Any]
    evidence_result: Mapping[str, Any]
    open_required_work: Tuple[str, ...]
    scope: Mapping[str, Any]
    coverage: Mapping[str, Any]
    after_source_revision_id: str
    after_knowledge_revision_id: Optional[str]
    status: str
    reasons: Tuple[str, ...]
    #: Uniform versioning strategy -- see
    #: `ArtifactRevision.schema_version` docstring.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        _require_nonempty_str(self, "id")
        _require_nonempty_str(self, "after_source_revision_id")
        _require_enum(self, "status", VERIFICATION_STATUSES)
        # C15: required action chưa xong / test required chưa chạy / critical
        # GAP trong phạm vi ảnh hưởng -> không VERIFIED/complete.
        if self.status == "PASSED" and (self.open_required_work or self.missing_edits):
            raise ValidationError(
                "VerificationReport.status cannot be PASSED while "
                "open_required_work or missing_edits is non-empty (contracts C15)")
        for name in ("expected_edits", "actual_edits", "unexpected_edits",
                     "missing_edits", "open_required_work", "reasons"):
            _require_sequence(self, name)
        for name in ("parsing_result", "tests_result", "simulation_result",
                     "evidence_result", "scope", "coverage"):
            _require_mapping(self, name, allow_empty=True)


# ---------------------------------------------------------------------------
# RecoveryProposal (contracts §2.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryProposal:
    """"current source, target historic source, delta preview, affected
    decisions/KM, loss warning for intervening edits, approval,
    restored_from."""

    id: str
    current_source_revision_id: str
    target_historic_source_revision_id: str
    delta_preview: Mapping[str, Any]
    affected_decision_ids: Tuple[str, ...]
    affected_knowledge_revision_ids: Tuple[str, ...]
    loss_warning: Optional[str]
    approval_id: Optional[str]
    restored_from: str
    #: Uniform versioning strategy -- see
    #: `ArtifactRevision.schema_version` docstring.
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        for name in ("id", "current_source_revision_id",
                     "target_historic_source_revision_id", "restored_from"):
            _require_nonempty_str(self, name)
        if self.current_source_revision_id == self.target_historic_source_revision_id:
            raise ValidationError(
                "RecoveryProposal current and target historic source "
                "revisions must differ")
        _require_mapping(self, "delta_preview", allow_empty=True)
        for name in ("affected_decision_ids", "affected_knowledge_revision_ids"):
            _require_sequence(self, name)


# ---------------------------------------------------------------------------
# Rule (contracts §2.1, distinct from RuleCandidate).
# ---------------------------------------------------------------------------

#: Contracts §2.1 Rule row: "confirmation state". No enumerated closed set
#: is spelled out verbatim in the contract text; this module models the
#: same PROPOSED/VALIDATED/PENDING_HUMAN/CONFIRMED/REJECTED candidate
#: lifecycle from contracts §3 plus a terminal `SUPERSEDED` state for a
#: confirmed Rule that has since been redirected by a merge/split event
#: (see `superseded_by` below). This is a judgment call (like
#: `RuleCandidate.applicability`'s enum), recorded here to be
#: confirmed or widened, not re-derived from an explicit
#: contract enumeration.
RULE_CONFIRMATION_STATES = frozenset(
    {"DRAFT", "VALIDATED", "PENDING_HUMAN", "CONFIRMED", "REJECTED", "SUPERSEDED"})


@dataclass(frozen=True)
class Rule:
    """The CONFIRMED counterpart of `RuleCandidate`. Contracts §2.1 Rule
    row: "stable rule_id + version, candidate ancestry, normalized
    semantics, confirmation state, evidence, decision refs, scope. ID mới
    do hệ thống cấp; merge/split là event có redirect/version, không mất
    ancestry."

    Distinct from `RuleCandidate` on purpose (a rule must not be modelled
    as its candidate alone): `RuleCandidate` is the AI-proposed, not-yet-authoritative
    inference; `Rule` is the system-issued, human-confirmed record with its
    own stable identity and version, independent of any one candidate's
    lifecycle. A `Rule` never mutates in place -- a merge/split is modelled
    as a NEW `Rule` version/id whose `merged_from`/`split_from` preserves
    the ancestry of the rule(s) it replaces, while the OLD `Rule`(s) get
    `superseded_by` set to the new rule_id (a redirect, not a silent loss
    of ancestry). The full merge/split STATE MACHINE (who may trigger it,
    what preconditions apply) is explicitly out of scope here -- that is
    the lifecycle contract's job; this type only provides the SCHEMA room for
    it (this field, not its enforcement)."""

    rule_id: str
    version: int
    candidate_ids: Tuple[str, ...]
    normalized_semantics: Mapping[str, Any]
    confirmation_state: str
    evidence: Tuple[EvidenceRef, ...]
    decision_ids: Tuple[str, ...]
    scope: Mapping[str, Any]
    schema_version: str
    #: A merge/split event's redirect target: when this Rule has been
    #: superseded by a newer rule_id (merge/split), this holds that new
    #: rule_id. `None` while this Rule is still the current, authoritative
    #: one. Setting this does NOT erase this Rule's own history -- it is a
    #: pointer forward, not a deletion (contracts C16-style redirect, not
    #: overwrite).
    superseded_by: Optional[str] = None
    #: Ancestry: rule_ids this Rule was formed by MERGING together (empty
    #: if this Rule was not created by a merge).
    merged_from: Tuple[str, ...] = ()
    #: Ancestry: the single rule_id this Rule was formed by SPLITTING from
    #: (`None` if this Rule was not created by a split).
    split_from: Optional[str] = None

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        _require_nonempty_str(self, "rule_id")
        if not isinstance(self.version, int) or isinstance(self.version, bool) \
                or self.version < 1:
            raise ValidationError("Rule.version must be a positive int")
        _require_enum(self, "confirmation_state", RULE_CONFIRMATION_STATES)
        _require_mapping(self, "normalized_semantics", allow_empty=False)
        if not isinstance(self.scope, dict) or not self.scope:
            raise ValidationError(
                "Rule.scope must be a non-empty dict; missing/empty/"
                "wrong-typed scope is never treated as 'applies to all' "
                "(same discipline as RuleCandidate.scope)")
        for name in ("candidate_ids", "evidence", "decision_ids", "merged_from"):
            _require_sequence(self, name)
        if not self.candidate_ids:
            raise ValidationError(
                "Rule.candidate_ids (candidate ancestry) must be non-empty: "
                "a confirmed Rule must trace back to at least one "
                "RuleCandidate it was derived from")
        if self.confirmation_state == "CONFIRMED" and not self.decision_ids:
            raise ValidationError(
                "Rule.decision_ids must be non-empty when confirmation_state "
                "is CONFIRMED: a confirmed Rule needs at least one "
                "HumanDecision reference (contracts §2.1: 'decision refs')")
