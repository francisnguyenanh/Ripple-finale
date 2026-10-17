# -*- coding: utf-8 -*-
"""Acceptance tests for the versioned v2 schemas, provenance and
compatibility layer.

The tests are written against the data-class contract (C01-C20, the
"unknown applicability" / money-decimal / decision_epoch rules), not
against any particular implementation.

The modules under test are:

    core/change_intelligence/v2/schema.py     - dataclasses + validators
    core/change_intelligence/v2/serialize.py  - canonical hash function
    core/change_intelligence/v2/compat.py     - legacy compatibility DTO

Failure-injection guidance for reviewers of this suite: each of the
following mutations should make the named test fail:
  - break `canonical_hash` (e.g. use `id(obj)` or an unsorted dict) and
    confirm `test_canonical_hash_stable_and_deterministic` fails;
  - weaken the scope-required gate (e.g. default missing scope to an
    "applies to all" sentinel) and confirm
    `test_rule_candidate_missing_scope_is_rejected` fails;
  - allow a same-source EvidenceRef to pass validation against a foreign
    revision id and confirm `test_evidence_ref_cross_revision_rejected`
    fails;
  - make source_hash equal to the event/transaction id and confirm
    `test_source_hash_differs_structurally_from_event_id` fails.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# --- imports of the v2 domain modules ----------------------------------------
from core.change_intelligence.v2.schema import (  # noqa: E402
    ANALYSIS_RUN_STATUSES,
    APPLICABILITY_VALUES,
    APPROVAL_DECISIONS,
    ARTIFACT_PROVENANCE_VALUES,
    FINDING_LIFECYCLE_STATES,
    FINDING_SEVERITIES,
    FINDING_TYPES,
    KNOWLEDGE_LIFECYCLE_STATES,
    MODALITY_VALUES,
    RELATION_DIRECTIONS,
    RELATION_LIFECYCLE_STATES,
    RULE_CONFIRMATION_STATES,
    VERIFICATION_STATUSES,
    AnalysisRun,
    Approval,
    ArtifactRevision,
    EvidenceRef,
    Finding,
    HumanDecision,
    KnowledgeRevision,
    ManifestEntry,
    Money,
    Observation,
    PatchProposal,
    RecoveryProposal,
    Relation,
    RelationCandidate,
    Rule,
    RuleCandidate,
    SchemaVersionError,
    SourceRevision,
    ValidationError,
    VerificationReport,
    _compute_manifest_hash,
)
from core.change_intelligence.v2.serialize import (  # noqa: E402
    _DECIMAL_TAG_KEY,
    _ESCAPE_KEY,
    canonical_hash,
)
from core.change_intelligence.v2.compat import (  # noqa: E402
    decode_legacy_knowledge_record,
    decode_legacy_session,
)


# ---------------------------------------------------------------------------
# Fixture values: concrete numbers borrowed from Harbor's
# ripple/_testdata/generated/bundles/harbor_demo/input/config/approval.yaml
# (standard_manager_threshold_jpy: 1000000, price_tolerance_percent: 2).
# These are CONCRETE TEST VALUES ONLY. The schema/validator code under test
# must stay generic — see test_schema_accepts_non_harbor_equivalent_example,
# which proves the same validators accept a structurally identical example
# with no Harbor business meaning at all.
# ---------------------------------------------------------------------------

HARBOR_SOURCE_REVISION_ID = "sr-harbor-0001"
HARBOR_ARTIFACT_REVISION_ID = "ar-harbor-approval-yaml-0001"
GENERIC_SOURCE_REVISION_ID = "sr-generic-0001"
GENERIC_ARTIFACT_REVISION_ID = "ar-generic-config-0001"


def _make_evidence_ref(source_revision_id: str, artifact_revision_id: str,
                        quote: str = "standard_manager_threshold_jpy: 1000000"):
    return EvidenceRef(
        source_revision_id=source_revision_id,
        artifact_revision_id=artifact_revision_id,
        content_hash="sha256:deadbeef",
        locator_type="text_line",
        locator="config/approval.yaml:5",
        quote=quote,
        parser_version="yaml-parser@1",
    )


def _make_rule_candidate(*, scope=None, source_revision_id=HARBOR_SOURCE_REVISION_ID,
                          artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
                          evidence_source_revision_id=None,
                          applicability="RECOGNIZED", applicability_reason=None):
    """Builds a structurally valid RuleCandidate unless a kwarg overrides
    a field to make it invalid. `evidence_source_revision_id` lets a test
    deliberately point evidence at a DIFFERENT revision than the
    candidate's own `source_revision_id` (cross-revision case).

    Default `applicability="RECOGNIZED"` (a plain, non-"UNKNOWN" value)
    deliberately decouples this general-purpose helper from the
    UNKNOWN-applicability-requires-a-reason gate in
    `RuleCandidate.__post_init__` (`core/change_intelligence/v2/schema.py`):
    the helper is used by scope/evidence/hash/schema-version tests that
    have nothing to do with applicability semantics, so it must not
    incidentally trip that gate. Tests that specifically exercise UNKNOWN
    behaviour either pass `applicability="UNKNOWN"` (with a real
    `applicability_reason`) through this helper, or construct
    `RuleCandidate` directly (as the dedicated UNKNOWN-without-reason
    tests below already do, in isolation from every other field)."""
    ev_source = evidence_source_revision_id or source_revision_id
    kwargs = dict(
        candidate_id="cand-0001",
        subject="purchase_approval_threshold",
        predicate="requires_manager_approval_above",
        value=Money(amount=Decimal("1000000"), currency="JPY"),
        applicability=applicability,
        modality="must",
        source_revision_id=source_revision_id,
        artifact_revision_id=artifact_revision_id,
        evidence=(_make_evidence_ref(ev_source, artifact_revision_id),),
        schema_version="1.0.0",
    )
    if applicability_reason is not None:
        kwargs["applicability_reason"] = applicability_reason
    if scope is not None:
        kwargs["scope"] = scope
    return RuleCandidate(**kwargs)


# ===========================================================================
# (a) Missing `scope` is rejected, never defaulted to "applies to all"
# ===========================================================================

def test_rule_candidate_missing_scope_is_rejected():
    """C: missing field. Task card: 'reject a missing scope instead of
    defaulting to applies to all'. `scope` must be a REQUIRED constructor
    argument / required validated field, not one with an implicit default
    that means 'all'."""
    with pytest.raises((TypeError, ValidationError)):
        _make_rule_candidate(scope=None)  # scope omitted entirely


def test_rule_candidate_scope_explicitly_empty_string_is_rejected():
    """Edge: an empty-string scope is not a valid scope declaration either
    — it must not be silently treated as 'unscoped == all'."""
    with pytest.raises(ValidationError):
        _make_rule_candidate(scope="")


def test_rule_candidate_scope_wrong_type_is_rejected():
    """Edge: malformed input — scope given as an int instead of the typed
    scope representation."""
    with pytest.raises((TypeError, ValidationError)):
        _make_rule_candidate(scope=12345)


def test_rule_candidate_with_explicit_scope_is_accepted():
    """Happy path counterpart to (a): a well-formed, non-empty, correctly
    typed scope is accepted."""
    candidate = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    assert candidate.scope == {"applies_to": ["dept:finance"]}


# ===========================================================================
# (b) Cross-revision EvidenceRef is invalid
# ===========================================================================

def test_evidence_ref_cross_revision_rejected():
    """C: invalid / conflicting. An EvidenceRef pointing at a source/
    artifact revision different from the claim's own revision must be
    rejected as invalid (contracts §2.1: EvidenceRef must reference the
    SAME source/artifact revision as the claim it supports)."""
    with pytest.raises(ValidationError):
        _make_rule_candidate(
            scope={"applies_to": ["dept:finance"]},
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            evidence_source_revision_id="sr-DIFFERENT-0999",
        )


def test_evidence_ref_same_revision_is_accepted():
    """Happy-path counterpart: evidence citing the claim's own revision is
    valid."""
    candidate = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    assert candidate.evidence[0].source_revision_id == candidate.source_revision_id


def test_evidence_ref_missing_locator_is_rejected():
    """C: missing evidence field. A locator-less evidence ref cannot be
    verified against bytes (contracts §2.1: 'Snapshot/hash phải đọc được,
    không locator-only' — read the other way: no locator-only is invalid,
    reading must be possible, i.e. locator is required)."""
    with pytest.raises((TypeError, ValidationError)):
        EvidenceRef(
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            content_hash="sha256:deadbeef",
            locator_type="text_line",
            locator="",
            quote="standard_manager_threshold_jpy: 1000000",
            parser_version="yaml-parser@1",
        )


# ===========================================================================
# (c) Canonical hash is stable / deterministic
# ===========================================================================

def test_canonical_hash_stable_and_deterministic_same_process():
    """Same logical content -> same hash, called twice in this process."""
    candidate = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    h1 = canonical_hash(candidate)
    h2 = canonical_hash(candidate)
    assert h1 == h2


def test_canonical_hash_deterministic_across_independent_construction():
    """Two independently constructed but logically identical objects hash
    the same — this is what makes 'deterministic across process runs'
    testable at all without literally forking a process: if two
    independent object graphs with the same content diverge, no amount of
    same-process determinism would save a real distributed re-run."""
    c1 = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    c2 = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    assert canonical_hash(c1) == canonical_hash(c2)


def test_canonical_hash_differs_when_content_differs():
    """Negative control: a real hash function must NOT collide different
    content, or 'stable' would be trivially satisfied by a constant."""
    c1 = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    c2 = _make_rule_candidate(scope={"applies_to": ["dept:sales"]})
    assert canonical_hash(c1) != canonical_hash(c2)


def test_canonical_hash_key_order_independent():
    """Edge: canonical serialization must sort keys, so two dicts built in
    different insertion order still hash identically (ADR-005 §2.7:
    'sorted keys in codepoint order')."""
    d1 = {"b": 1, "a": 2}
    d2 = {"a": 2, "b": 1}
    assert canonical_hash(d1) == canonical_hash(d2)


# ===========================================================================
# (d) source hash is a DIFFERENT field/value from any event/transaction ID
# ===========================================================================

def test_source_hash_differs_structurally_from_event_id():
    """Task card: 'Source hash phân biệt event ID' / 'Hash nội dung khác
    commit/event ID để quay về cùng bytes vẫn có lịch sử mới' (contracts
    §2.1, SourceRevision row). This must be a STRUCTURAL assertion: the two
    are different fields, and restoring identical bytes produces the SAME
    manifest_hash but a NEW id/event, proving they are not the same value
    by construction, not merely coincidentally different strings today.

    `SourceRevision.__post_init__` rejects any manifest_hash that does not
    match `_compute_manifest_hash(manifest)`, so the fixture uses a real,
    correctly-computed digest rather than a placeholder. `manifest` is a
    sorted tuple of `ManifestEntry(artifact_id, artifact_revision_id,
    blob_hash)`."""
    manifest = (ManifestEntry(
        artifact_id="art-harbor-approval-yaml",
        artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
        blob_hash="sha256:harbor-approval-yaml-blob-0001"),)
    real_hash = _compute_manifest_hash(manifest)
    rev1 = SourceRevision(
        id="event-0001",
        parent_id=None,
        manifest=manifest,
        manifest_hash=real_hash,
        created_by="tester@example.com",
        created_at="2026-09-26T00:00:00Z",
        trigger="import",
        restored_from=None,
    )
    # Field-level distinctness: the dataclass must not alias id to the hash.
    assert rev1.id != rev1.manifest_hash
    assert "manifest_hash" in rev1.__dataclass_fields__
    assert "id" in rev1.__dataclass_fields__

    # Behavioural distinctness: restoring the exact same bytes (same
    # manifest -> same manifest_hash) must still mint a NEW event id,
    # because a restore is a new event over old bytes (contracts §2.1
    # worked example: "Restore S0 tạo S2 ... event mới").
    rev2 = SourceRevision(
        id="event-0002-restore",
        parent_id=rev1.id,
        manifest=rev1.manifest,
        manifest_hash=rev1.manifest_hash,
        created_by="tester@example.com",
        created_at="2026-09-26T01:00:00Z",
        trigger="restore",
        restored_from=rev1.id,
    )
    assert rev2.manifest_hash == rev1.manifest_hash  # same bytes
    assert rev2.id != rev1.id  # different event/history


# ===========================================================================
# (e) decoding an old/legacy serialized record loses no field
# ===========================================================================

def test_decode_legacy_knowledge_record_preserves_all_legacy_fields():
    """Legacy shape based on reading (read-only, not modified):
      - core/change_intelligence/knowledge.py::KnowledgeRecord
        (id, knowledge_type, semantic_id, statement, evidence,
        derived_from, state, alignment, representations, gap_id, reason,
        km_version)
      - core/change_intelligence/evidence.py::EvidenceRef
        (artifact, locator, quote, snapshot_id)
    A literal legacy-serialized dict (as `app/api/codec.py::encode_record`
    would have produced for a KnowledgeRecord) must decode through the new
    compatibility DTO WITHOUT dropping any of the legacy fields — task
    card: 'old record decode không mất trường'."""
    legacy_record = {
        "id": "kr-legacy-0001",
        "knowledge_type": "rule",
        "semantic_id": "sem-0001",
        "statement": "Purchases above 1,000,000 JPY require manager approval.",
        "evidence": [
            {
                "artifact": "config/approval.yaml",
                "locator": "line:5",
                "quote": "standard_manager_threshold_jpy: 1000000",
                "snapshot_id": "snap-0001",
            }
        ],
        "derived_from": ["cand-legacy-0001"],
        "state": "CONFIRMED",
        "alignment": "ALIGNED",
        "representations": [],
        "gap_id": "",
        "reason": "",
        "km_version": 1,
    }

    decoded = decode_legacy_knowledge_record(legacy_record)

    # Every legacy top-level key must be recoverable from the decoded DTO,
    # by field name or an explicit legacy-field mapping the DTO documents.
    for key, value in legacy_record.items():
        assert hasattr(decoded, key), f"legacy field {key!r} was dropped"
        assert getattr(decoded, key) == value, f"legacy field {key!r} changed value"


def test_decode_legacy_knowledge_record_does_not_mutate_enum_serialization():
    """Task card: 'không đổi enum serialized cũ tại chỗ' (do not change old
    serialized enum values in place). Decoding a legacy record whose
    `state`/`alignment` are the OLD string spellings must not rewrite them
    to a new v2 spelling as a side effect visible on the decoded object's
    legacy-facing fields."""
    legacy_record = {
        "id": "kr-legacy-0002",
        "knowledge_type": "constraint",
        "semantic_id": "sem-0002",
        "statement": "Payment terms default to 30 days.",
        "evidence": [
            {
                "artifact": "config/approval.yaml",
                "locator": "line:12",
                "quote": "payment_term_days: 30",
                "snapshot_id": "snap-0001",
            }
        ],
        "derived_from": ["cand-legacy-0002"],
        "state": "CONFIRMED",
        "alignment": "CONFLICT",
        "representations": [],
        "gap_id": "",
        "reason": "",
        "km_version": 2,
    }
    decoded = decode_legacy_knowledge_record(legacy_record)
    assert decoded.state == "CONFIRMED"
    assert decoded.alignment == "CONFLICT"


def test_decode_legacy_knowledge_record_missing_key_is_rejected():
    """C: malformed input. A legacy record missing a required legacy key
    (`derived_from`, which carries lineage/I2) must be rejected clearly,
    not decoded with a silently substituted empty default that would hide
    a real data-loss bug in the legacy store."""
    legacy_record = {
        "id": "kr-legacy-0003",
        "knowledge_type": "rule",
        "semantic_id": "sem-0003",
        "statement": "Some statement.",
        "evidence": [],
        # "derived_from" intentionally omitted
        "state": "CONFIRMED",
        "alignment": "ALIGNED",
        "representations": [],
        "gap_id": "",
        "reason": "",
        "km_version": 1,
    }
    with pytest.raises((KeyError, ValidationError)):
        decode_legacy_knowledge_record(legacy_record)


# ===========================================================================
# (f) unsupported/unknown schema version is rejected, not silently coerced
# ===========================================================================

def test_unsupported_schema_version_rejected_clearly():
    """C: unknown enum/version input. `schema_version="99.0.0"` does not
    exist; it must raise a distinct, identifiable error type
    (`SchemaVersionError`), never be coerced to the nearest known version
    or silently accepted."""
    with pytest.raises(SchemaVersionError):
        _make_rule_candidate(scope={"applies_to": ["dept:finance"]}).__class__(
            candidate_id="cand-0002",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
            schema_version="99.0.0",
        )


def test_missing_schema_version_is_rejected():
    """Edge: missing-key case of the same gate — no `schema_version` at
    all must not default to 'assume current'."""
    with pytest.raises((TypeError, ValidationError, SchemaVersionError)):
        RuleCandidate(
            candidate_id="cand-0003",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
        )


def test_schema_version_wrong_type_is_rejected():
    """Edge: malformed input — schema_version given as a float/int instead
    of the typed version string."""
    with pytest.raises((TypeError, ValidationError, SchemaVersionError)):
        _make_rule_candidate(scope={"applies_to": ["dept:finance"]}).__class__(
            candidate_id="cand-0004",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
            schema_version=1.0,
        )


# ===========================================================================
# (g) UNKNOWN applicability/value requires a non-empty reason/GAP reference
# ===========================================================================

def test_unknown_applicability_without_reason_is_rejected():
    """Contracts §2.1: 'unknown applicability khác applies to all'. A bare
    UNKNOWN with no reason/GAP reference is invalid — this is NOT the same
    check as (a) (missing scope); this is the applicability field itself
    being UNKNOWN with no accompanying justification."""
    with pytest.raises(ValidationError):
        RuleCandidate(
            candidate_id="cand-0005",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            applicability_reason="",  # bare UNKNOWN, no reason/GAP ref
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
            schema_version="1.0.0",
        )


def test_unknown_applicability_with_gap_reference_is_accepted():
    """Happy path: UNKNOWN with an explicit GAP reference is valid."""
    candidate = RuleCandidate(
        candidate_id="cand-0006",
        subject="x",
        predicate="y",
        value=Money(amount=Decimal("1"), currency="JPY"),
        applicability="UNKNOWN",
        applicability_reason="GAP-0042: authority for cross-department scope unresolved",
        modality="must",
        source_revision_id=HARBOR_SOURCE_REVISION_ID,
        artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
        evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
        scope={"applies_to": ["dept:finance"]},
        schema_version="1.0.0",
    )
    assert candidate.applicability_reason


def test_unknown_applicability_reason_whitespace_only_is_rejected():
    """Edge: a whitespace-only reason is not a real reason (malformed
    input masquerading as present)."""
    with pytest.raises(ValidationError):
        RuleCandidate(
            candidate_id="cand-0007",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            applicability_reason="   ",
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
            schema_version="1.0.0",
        )


# ===========================================================================
# (h) money/threshold values use decimal/typed representation, not float
# ===========================================================================

def test_money_field_rejects_float_input():
    """Contracts §2.1: 'Money dùng decimal/minor units theo currency;
    không float equality'. ADR-005 §2.7: v2 guard 'refuses float'. A float
    passed where a typed Money amount is expected must be rejected, not
    silently accepted and coerced."""
    with pytest.raises((TypeError, ValidationError)):
        Money(amount=1000000.0, currency="JPY")


def test_money_decimal_precision_not_corrupted_by_float_arithmetic():
    """Classic float trap: 0.1 + 0.2 != 0.3 in binary floating point. A
    Decimal-typed money field constructed from decimal-safe inputs must
    compare exactly equal to the expected decimal total, proving the
    schema's money type does not route through float arithmetic anywhere
    in construction or comparison."""
    a = Money(amount=Decimal("0.10"), currency="USD")
    b = Money(amount=Decimal("0.20"), currency="USD")
    total = Money(amount=a.amount + b.amount, currency="USD")
    assert total.amount == Decimal("0.30")
    # The float-precision trap this guards against:
    assert 0.1 + 0.2 != 0.3
    assert float(total.amount) != 0.1 + 0.2 or total.amount == Decimal("0.30")


def test_money_currency_mismatch_addition_is_rejected_or_not_offered():
    """Edge / conflicting: money values must be typed by currency; the
    schema must not expose an implicit cross-currency numeric equality.
    Two Money values with different currencies and the same numeric
    amount must NOT compare equal (typed, not just numeric)."""
    jpy = Money(amount=Decimal("1000000"), currency="JPY")
    usd = Money(amount=Decimal("1000000"), currency="USD")
    assert jpy != usd


def test_money_negative_amount_is_rejected():
    """Edge: negative amount domain case for a threshold-typed money
    field. (If a genuine business case needs negative money — e.g. a
    credit note — the validator must express that explicitly; a bare
    threshold candidate built here has no such semantics, so negative is
    invalid.)"""
    with pytest.raises(ValidationError):
        Money(amount=Decimal("-1"), currency="JPY")


# ===========================================================================
# Genericity: schemas must accept a structurally-equivalent NON-Harbor
# example (task card: "schemas of Ripple must be generic")
# ===========================================================================

def test_schema_accepts_non_harbor_equivalent_example():
    """The same RuleCandidate validator must accept an example with no
    Harbor business meaning at all — proving the schema logic is generic,
    not hardcoded to Harbor's routing/matching vocabulary."""
    candidate = _make_rule_candidate(
        scope={"applies_to": ["region:emea"]},
        source_revision_id=GENERIC_SOURCE_REVISION_ID,
        artifact_revision_id=GENERIC_ARTIFACT_REVISION_ID,
    )
    assert candidate.scope == {"applies_to": ["region:emea"]}
    assert candidate.source_revision_id == GENERIC_SOURCE_REVISION_ID


def test_schema_module_has_no_harbor_specific_identifiers():
    """Structural genericity guard: the schema module's own source text
    must not hardcode Harbor field/business names (e.g.
    'standard_manager_threshold_jpy', 'capex_finance_threshold_jpy') into
    validation logic. This is a lightweight static check, not a full AST
    scan; it fails loudly if a Harbor-specific literal
    into the generic schema module."""
    import inspect
    import core.change_intelligence.v2.schema as schema_mod

    source = inspect.getsource(schema_mod)
    banned = ["standard_manager_threshold_jpy", "capex_finance_threshold_jpy",
              "high_risk_route", "NEEDS_DIRECTOR"]
    for token in banned:
        assert token not in source, (
            f"schema module hardcodes Harbor-specific token {token!r}; "
            "task card requires schemas to stay generic"
        )


# ===========================================================================
# ArtifactRevision / SourceRevision structural sanity (contracts §2.1)
# ===========================================================================

def test_artifact_revision_requires_stable_artifact_id_and_revision_id():
    """C: missing field. `artifact_id` (stable across renames) and
    `revision_id` (this specific revision) are two distinct required
    fields — contracts §2.1: 'Đổi tên giữ identity khi có bằng chứng'."""
    with pytest.raises((TypeError, ValidationError)):
        ArtifactRevision(
            artifact_id="",
            revision_id="rev-0001",
            logical_path="config/approval.yaml",
            media_type="application/yaml",
            blob_hash="sha256:abc123",
            byte_size=128,
            parser_version="yaml-parser@1",
            source_role="normative",
        )


def test_artifact_revision_unknown_source_role_is_rejected():
    """C: unknown enum. `source_role` must be one of the contract's fixed
    set (normative/implementation/observational/test/unknown); an
    arbitrary unrecognised string must be rejected, not passed through."""
    with pytest.raises(ValidationError):
        ArtifactRevision(
            artifact_id="art-0001",
            revision_id="rev-0001",
            logical_path="config/approval.yaml",
            media_type="application/yaml",
            blob_hash="sha256:abc123",
            byte_size=128,
            parser_version="yaml-parser@1",
            source_role="totally-made-up-role",
        )


# ===========================================================================
# Additional negative/edge/contract tests against the real implementation
# (core/change_intelligence/v2/{schema,serialize,compat}.py).
# ===========================================================================

def test_unknown_applicability_reason_kwarg_fully_omitted_is_rejected():
    """Omitting `applicability_reason` entirely must be rejected.

    `RuleCandidate.
    applicability_reason` defaults to a non-blank internal placeholder
    (`_UNSPECIFIED_REASON = "reason not provided"`,
    `core/change_intelligence/v2/schema.py:59`) when the keyword argument
    is fully OMITTED from the call, rather than passed as an explicit
    empty/whitespace string. The related tests
    (`test_unknown_applicability_without_reason_is_rejected`,
    `test_unknown_applicability_reason_whitespace_only_is_rejected`) only
    ever pass an EXPLICIT `""` / `"   "` — they never test true omission of
    the keyword argument itself, which is the actual gap the task card's
    'UNKNOWN bắt buộc reason/GAP ref' requirement is meant to cover: a
    caller who does not supply a reason at all must not silently get a
    placeholder that satisfies the whitespace check.

    This test constructs a RuleCandidate with `applicability="UNKNOWN"`
    and the `applicability_reason` keyword argument not passed at all. Per
    the task card, this must be rejected exactly like an explicit blank
    reason. As implemented today, `schema.py:228`'s default
    (`applicability_reason: str = _UNSPECIFIED_REASON`) makes the
    `__post_init__` check at `schema.py:259-263` see a non-blank string and
    accept it — so this test currently FAILS (no ValidationError is
    raised).
    """
    with pytest.raises(ValidationError):
        RuleCandidate(
            candidate_id="cand-0008",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            # applicability_reason intentionally NOT passed at all.
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
            schema_version="1.0.0",
        )


def test_schema_version_error_is_subclass_of_validation_error():
    """Contract-consistency check: `core/change_intelligence/v2/schema.py`
    defines `SchemaVersionError(ValidationError)` (line 37). A caller that
    only catches the broader `ValidationError` must still catch every
    version-rejection case; this pins that relationship so a future change
    cannot silently detach `SchemaVersionError` from `ValidationError`
    without this test flagging it."""
    assert issubclass(SchemaVersionError, ValidationError)
    with pytest.raises(ValidationError):
        _make_rule_candidate(scope={"applies_to": ["dept:finance"]}).__class__(
            candidate_id="cand-0009",
            subject="x",
            predicate="y",
            value=Money(amount=Decimal("1"), currency="JPY"),
            applicability="UNKNOWN",
            applicability_reason="GAP-0099: version probe",
            modality="must",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
            scope={"applies_to": ["dept:finance"]},
            schema_version="not-a-real-version",
        )


def test_decode_legacy_knowledge_record_evidence_items_stay_as_raw_legacy_dicts():
    """Legacy `evidence` items stay raw legacy dicts:
    `decode_legacy_knowledge_record` does NOT convert legacy `evidence`
    list items (legacy shape: `artifact`/`locator`/`quote`/`snapshot_id`,
    see `core/change_intelligence/evidence.py`) into v2 `EvidenceRef`
    (shape: `source_revision_id`/`artifact_revision_id`/`content_hash`/
    `locator_type`/`locator`/`quote`/`parser_version`). This is confirmed
    ACCEPTABLE scope for this task: the task card only requires 'old
    record decode không mất trường' (lossless field preservation), not
    type conversion, and no acceptance test names a
    conversion requirement. This test pins the CURRENT, accepted
    behaviour (raw dicts pass through unchanged) so a future silent
    partial-conversion cannot regress the lossless-preservation guarantee
    without a visible test failure; it is not evidence of a defect."""
    legacy_record = {
        "id": "kr-legacy-0004",
        "knowledge_type": "rule",
        "semantic_id": "sem-0004",
        "statement": "Some other statement.",
        "evidence": [
            {
                "artifact": "config/approval.yaml",
                "locator": "line:5",
                "quote": "standard_manager_threshold_jpy: 1000000",
                "snapshot_id": "snap-0001",
            }
        ],
        "derived_from": ["cand-legacy-0004"],
        "state": "CONFIRMED",
        "alignment": "ALIGNED",
        "representations": [],
        "gap_id": "",
        "reason": "",
        "km_version": 1,
    }
    decoded = decode_legacy_knowledge_record(legacy_record)
    assert decoded.evidence == legacy_record["evidence"]
    assert isinstance(decoded.evidence[0], dict)
    assert not isinstance(decoded.evidence[0], EvidenceRef)


def test_canonical_hash_list_and_tuple_with_same_elements_collide():
    """Boundary: the real `_to_jsonable` (`core/change_intelligence/v2/serialize.py:30-31`)
    is visible: both `list` and `tuple` are converted to a plain JSON
    `list` before hashing, so a list and a tuple with identical elements
    hash IDENTICALLY. This documents actual, current behaviour (not a bug
    for this task's scope, since JSON has no tuple type and
    `RuleCandidate.evidence` is typed as `Sequence[EvidenceRef]`), so a
    future accidental change to sequence handling is caught."""
    assert canonical_hash([1, 2, 3]) == canonical_hash((1, 2, 3))


def test_money_dataclass_equality_distinguishes_currency_not_just_amount():
    """Re-verifies, directly against the real dataclass-generated `__eq__`
    (`Money` is `@dataclass(frozen=True)` with no custom `__eq__`,
    `core/change_intelligence/v2/schema.py:78-108`), that equality compares
    the `(amount, currency)` tuple -- not amount alone. An
    inequality check alone is not enough; this additionally checks that two
    Money values with the SAME currency and amount but constructed
    independently DO compare equal, so the inequality above is proven to
    come from currency, not from frozen-dataclass identity/reference
    semantics."""
    a = Money(amount=Decimal("500"), currency="JPY")
    b = Money(amount=Decimal("500"), currency="JPY")
    c = Money(amount=Decimal("500"), currency="USD")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)


# ===========================================================================
# Extended coverage: existence/roundtrip for every one of the 11 newly-added types,
# bad-enum per new enum field, missing/empty-required-field per contracts
# §2.1/§2.2's minimum-fields table; VerificationReport C15; ArtifactRevision
# negative size; SourceRevision unsorted-manifest + wrong-digest trust
# boundary; Decimal-vs-string hash regression; legacy session decode +
# unknown-field preserve; unsupported schema version spot-check across new
# types.
# ===========================================================================

def _evidence_for(source_revision_id, artifact_revision_id):
    return _make_evidence_ref(source_revision_id, artifact_revision_id)


# --- Observation -------------------------------------------------------

def test_observation_happy_path_construction():
    obs = Observation(
        id="obs-0001",
        source_revision_id=HARBOR_SOURCE_REVISION_ID,
        artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
        kind="ocr_extraction",
        data={"amount": "1000000"},
        evidence=(_evidence_for(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
        schema_version="1.0.0",
    )
    assert obs.kind == "ocr_extraction"


def test_observation_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        Observation(
            id="obs-0002",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            kind="ocr_extraction",
            data={},
            evidence=(),
            schema_version="99.0.0",
        )


def test_observation_missing_kind_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        Observation(
            id="obs-0003",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            kind="",
            data={},
            evidence=(),
            schema_version="1.0.0",
        )


def test_observation_cross_revision_evidence_is_rejected():
    with pytest.raises(ValidationError):
        Observation(
            id="obs-0004",
            source_revision_id=HARBOR_SOURCE_REVISION_ID,
            artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
            kind="ocr_extraction",
            data={},
            evidence=(_evidence_for("sr-DIFFERENT-0999", HARBOR_ARTIFACT_REVISION_ID),),
            schema_version="1.0.0",
        )


# --- RelationCandidate / Relation ---------------------------------------

def _relation_candidate_kwargs(**overrides):
    kwargs = dict(
        candidate_id="rel-cand-0001",
        from_entity_revision_id="ent-a-0001",
        to_entity_revision_id="ent-b-0001",
        relation_type="depends_on",
        direction="a_to_b",
        scope={"applies_to": ["dept:finance"]},
        evidence_support=(),
        evidence_contradiction=(),
        derivation_method="script:v1",
        lifecycle_state="PROPOSED",
        schema_version="1.0.0",
    )
    kwargs.update(overrides)
    return kwargs


def test_relation_candidate_happy_path_construction():
    rc = RelationCandidate(**_relation_candidate_kwargs())
    assert rc.lifecycle_state == "PROPOSED"


def test_relation_candidate_bad_direction_enum_is_rejected():
    with pytest.raises(ValidationError):
        RelationCandidate(**_relation_candidate_kwargs(direction="sideways"))


def test_relation_candidate_bad_lifecycle_state_enum_is_rejected():
    with pytest.raises(ValidationError):
        RelationCandidate(**_relation_candidate_kwargs(lifecycle_state="MADE_UP_STATE"))


def test_relation_candidate_missing_candidate_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        RelationCandidate(**_relation_candidate_kwargs(candidate_id=""))


def test_relation_candidate_empty_scope_is_rejected():
    """Structural/empty-field case: scope must be a non-empty mapping,
    same discipline as RuleCandidate.scope."""
    with pytest.raises(ValidationError):
        RelationCandidate(**_relation_candidate_kwargs(scope={}))


def test_relation_happy_path_confirmed_with_decision_id():
    rel = Relation(
        **_relation_candidate_kwargs(lifecycle_state="CONFIRMED"),
        decision_id="dec-0001",
    )
    assert rel.decision_id == "dec-0001"


def test_relation_missing_decision_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        Relation(**_relation_candidate_kwargs(lifecycle_state="CONFIRMED"), decision_id="")


def test_relation_non_confirmed_lifecycle_state_is_rejected():
    """Two existing endpoints do not by themselves confirm the edge --
    `Relation` must reject any lifecycle_state other than CONFIRMED."""
    with pytest.raises(ValidationError):
        Relation(**_relation_candidate_kwargs(lifecycle_state="PROPOSED"), decision_id="dec-0002")


# --- KnowledgeRevision ----------------------------------------------------

def _knowledge_revision_kwargs(**overrides):
    kwargs = dict(
        id="km-0001",
        parent_id=None,
        source_revision_id=HARBOR_SOURCE_REVISION_ID,
        decision_epoch=1,
        parser_version="yaml-parser@1",
        prompt_version="prompt@1",
        model_config_version="model@1",
        rule_ids=(),
        edge_ids=(),
        finding_ids=(),
        coverage_manifest={},
        content_hash="sha256:abc",
        lifecycle_state="DRAFT",
        schema_version="1.0.0",
    )
    kwargs.update(overrides)
    return kwargs


def test_knowledge_revision_happy_path_construction():
    km = KnowledgeRevision(**_knowledge_revision_kwargs())
    assert km.lifecycle_state == "DRAFT"


def test_knowledge_revision_bad_lifecycle_state_enum_is_rejected():
    with pytest.raises(ValidationError):
        KnowledgeRevision(**_knowledge_revision_kwargs(lifecycle_state="MADE_UP_STATE"))


def test_knowledge_revision_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        KnowledgeRevision(**_knowledge_revision_kwargs(schema_version="99.0.0"))


def test_knowledge_revision_missing_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        KnowledgeRevision(**_knowledge_revision_kwargs(id=""))


def test_knowledge_revision_negative_decision_epoch_is_rejected():
    with pytest.raises(ValidationError):
        KnowledgeRevision(**_knowledge_revision_kwargs(decision_epoch=-1))


def test_knowledge_revision_accepted_without_accepted_by_at_is_rejected():
    """ACCEPTED lifecycle requires accepted_by/accepted_at -- contracts
    §2.1: 'accepted_by/at nullable' (nullable elsewhere, but required once
    lifecycle_state genuinely reaches ACCEPTED)."""
    with pytest.raises(ValidationError):
        KnowledgeRevision(**_knowledge_revision_kwargs(lifecycle_state="ACCEPTED"))


def test_knowledge_revision_accepted_with_accepted_by_at_is_accepted():
    km = KnowledgeRevision(**_knowledge_revision_kwargs(
        lifecycle_state="ACCEPTED", accepted_by="reviewer@example.com",
        accepted_at="2026-09-26T00:00:00Z"))
    assert km.lifecycle_state == "ACCEPTED"


# --- Finding ---------------------------------------------------------------

def _finding_kwargs(**overrides):
    kwargs = dict(
        id="find-0001",
        finding_type="GAP",
        subject_id="subj-0001",
        subject_version="v1",
        involved_claim_ids=(),
        missing_fields=("effective_start",),
        severity="MEDIUM",
        impact_scope={"applies_to": ["dept:finance"]},
        authority_required="finance_manager",
        lifecycle_state="OPEN",
        schema_version="1.0.0",
    )
    kwargs.update(overrides)
    return kwargs


def test_finding_happy_path_construction():
    finding = Finding(**_finding_kwargs())
    assert finding.finding_type == "GAP"


def test_finding_bad_finding_type_enum_is_rejected():
    with pytest.raises(ValidationError):
        Finding(**_finding_kwargs(finding_type="MADE_UP_TYPE"))


def test_finding_bad_severity_enum_is_rejected():
    with pytest.raises(ValidationError):
        Finding(**_finding_kwargs(severity="APOCALYPTIC"))


def test_finding_bad_lifecycle_state_enum_is_rejected():
    with pytest.raises(ValidationError):
        Finding(**_finding_kwargs(lifecycle_state="MADE_UP_STATE"))


def test_finding_missing_subject_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        Finding(**_finding_kwargs(subject_id=""))


def test_finding_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        Finding(**_finding_kwargs(schema_version="99.0.0"))


# --- HumanDecision -----------------------------------------------------

def _human_decision_kwargs(**overrides):
    kwargs = dict(
        id="dec-0001",
        actor="approver@example.com",
        permissions_snapshot=("finance_manager",),
        policy_version="policy@1",
        target_id="rule-0001",
        expected_version=1,
        action="APPROVE",
        rationale="Matches documented threshold.",
        evidence=(),
        scope={"applies_to": ["dept:finance"]},
        created_at="2026-09-26T00:00:00Z",
    )
    kwargs.update(overrides)
    return kwargs


def test_human_decision_happy_path_construction():
    decision = HumanDecision(**_human_decision_kwargs())
    assert decision.action == "APPROVE"


def test_human_decision_missing_actor_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        HumanDecision(**_human_decision_kwargs(actor=""))


def test_human_decision_negative_expected_version_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(expected_version=-1))


# --- AnalysisRun ---------------------------------------------------------

def _analysis_run_kwargs(**overrides):
    kwargs = dict(
        id="run-0001",
        workspace_id="ws-0001",
        pinned_source_revision_id=HARBOR_SOURCE_REVISION_ID,
        pinned_knowledge_revision_id=None,
        pinned_decision_epoch=1,
        purpose="rule_extraction",
        coverage={},
        omissions=(),
        status="QUEUED",
        provider_calls=(),
        prompt_digest="sha256:prompt",
        schema_digest="sha256:schema",
        input_hash="sha256:input",
        output_hash=None,
        attempts=0,
        created_at="2026-09-26T00:00:00Z",
        errors=(),
    )
    kwargs.update(overrides)
    return kwargs


def test_analysis_run_happy_path_construction():
    run = AnalysisRun(**_analysis_run_kwargs())
    assert run.status == "QUEUED"


def test_analysis_run_bad_status_enum_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisRun(**_analysis_run_kwargs(status="RETRYING_FOREVER"))


def test_analysis_run_missing_workspace_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        AnalysisRun(**_analysis_run_kwargs(workspace_id=""))


def test_analysis_run_negative_attempts_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisRun(**_analysis_run_kwargs(attempts=-1))


# --- PatchProposal -------------------------------------------------------

def _patch_proposal_kwargs(**overrides):
    kwargs = dict(
        id="patch-0001",
        target_source_revision_id=HARBOR_SOURCE_REVISION_ID,
        target_knowledge_revision_id="km-0001",
        approved_candidate_ids=("cand-0001",),
        edits=({"op": "replace", "path": "x"},),
        before_blob_hash="sha256:before",
        after_blob_hash="sha256:after",
        action_classification="config_edit",
        rationale="Aligns config with confirmed rule.",
        evidence=(),
        generated_by="analysis_run:run-0001",
        manual_work_required=False,
        dependency_ids=(),
        is_noop=False,
        schema_version="1.0.0",
    )
    kwargs.update(overrides)
    return kwargs


def test_patch_proposal_happy_path_construction():
    proposal = PatchProposal(**_patch_proposal_kwargs())
    assert proposal.is_noop is False


def test_patch_proposal_manual_work_required_wrong_type_is_rejected():
    with pytest.raises(ValidationError):
        PatchProposal(**_patch_proposal_kwargs(manual_work_required="yes"))


def test_patch_proposal_missing_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        PatchProposal(**_patch_proposal_kwargs(id=""))


def test_patch_proposal_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        PatchProposal(**_patch_proposal_kwargs(schema_version="99.0.0"))


# --- Approval --------------------------------------------------------------

def _approval_kwargs(**overrides):
    kwargs = dict(
        id="appr-0001",
        subject_type="PatchProposal",
        subject_id="patch-0001",
        composite_digest="sha256:composite",
        actor="approver@example.com",
        permissions_snapshot=("finance_manager",),
        decision="APPROVE",
        reason="Matches policy.",
        policy_version="policy@1",
        source_revision_id=HARBOR_SOURCE_REVISION_ID,
        knowledge_revision_id="km-0001",
        decision_epoch=1,
        review_bundle_hash="sha256:review",
        created_at="2026-09-26T00:00:00Z",
    )
    kwargs.update(overrides)
    return kwargs


def test_approval_happy_path_construction():
    approval = Approval(**_approval_kwargs())
    assert approval.decision == "APPROVE"


def test_approval_bad_decision_enum_is_rejected():
    with pytest.raises(ValidationError):
        Approval(**_approval_kwargs(decision="MAYBE"))


def test_approval_missing_composite_digest_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        Approval(**_approval_kwargs(composite_digest=""))


def test_approval_negative_decision_epoch_is_rejected():
    with pytest.raises(ValidationError):
        Approval(**_approval_kwargs(decision_epoch=-1))


# --- VerificationReport (contract C15) -------------------------------------

def _verification_report_kwargs(**overrides):
    kwargs = dict(
        id="verif-0001",
        expected_edits=(),
        actual_edits=(),
        unexpected_edits=(),
        missing_edits=(),
        parsing_result={},
        tests_result={},
        simulation_result={},
        evidence_result={},
        open_required_work=(),
        scope={},
        coverage={},
        after_source_revision_id=HARBOR_SOURCE_REVISION_ID,
        after_knowledge_revision_id="km-0001",
        status="PASSED",
        reasons=(),
    )
    kwargs.update(overrides)
    return kwargs


def test_verification_report_happy_path_construction():
    report = VerificationReport(**_verification_report_kwargs())
    assert report.status == "PASSED"


def test_verification_report_bad_status_enum_is_rejected():
    with pytest.raises(ValidationError):
        VerificationReport(**_verification_report_kwargs(status="DONE_ISH"))


def test_verification_report_missing_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        VerificationReport(**_verification_report_kwargs(id=""))


def test_verification_report_c15_passed_with_open_required_work_is_rejected():
    """Contract C15: no PASSED/complete status while
    open_required_work/missing_edits is non-empty. This is the
    should-be-REJECTED half of the pair."""
    with pytest.raises(ValidationError):
        VerificationReport(**_verification_report_kwargs(
            status="PASSED", open_required_work=("finish migration script",)))


def test_verification_report_c15_passed_with_missing_edits_is_rejected():
    with pytest.raises(ValidationError):
        VerificationReport(**_verification_report_kwargs(
            status="PASSED", missing_edits=({"path": "config/approval.yaml"},)))


def test_verification_report_c15_passed_with_empty_open_work_is_accepted():
    """Should-PASS half of the pair: PASSED with genuinely empty
    open_required_work and missing_edits is a valid, complete report."""
    report = VerificationReport(**_verification_report_kwargs(
        status="PASSED", open_required_work=(), missing_edits=()))
    assert report.status == "PASSED"


def test_verification_report_partial_with_open_required_work_is_accepted():
    """Non-PASSED statuses are NOT subject to the C15 emptiness gate --
    PARTIAL/FAILED/NOT_RUN may legitimately carry open required work."""
    report = VerificationReport(**_verification_report_kwargs(
        status="PARTIAL", open_required_work=("finish migration script",)))
    assert report.status == "PARTIAL"


# --- RecoveryProposal --------------------------------------------------

def _recovery_proposal_kwargs(**overrides):
    kwargs = dict(
        id="recov-0001",
        current_source_revision_id="sr-current-0001",
        target_historic_source_revision_id="sr-historic-0001",
        delta_preview={},
        affected_decision_ids=(),
        affected_knowledge_revision_ids=(),
        loss_warning=None,
        approval_id=None,
        restored_from="sr-historic-0001",
    )
    kwargs.update(overrides)
    return kwargs


def test_recovery_proposal_happy_path_construction():
    recov = RecoveryProposal(**_recovery_proposal_kwargs())
    assert recov.current_source_revision_id != recov.target_historic_source_revision_id


def test_recovery_proposal_same_current_and_target_is_rejected():
    with pytest.raises(ValidationError):
        RecoveryProposal(**_recovery_proposal_kwargs(
            current_source_revision_id="sr-same-0001",
            target_historic_source_revision_id="sr-same-0001"))


def test_recovery_proposal_missing_id_is_rejected():
    with pytest.raises((TypeError, ValidationError)):
        RecoveryProposal(**_recovery_proposal_kwargs(id=""))


# ===========================================================================
# ArtifactRevision: empty structural field + negative size.
# test_artifact_revision_requires_stable_artifact_id_and_revision_id
# and test_artifact_revision_unknown_source_role_is_rejected already exist
# above; negative byte_size and a second structural-field case
# (media_type) are covered separately here, not duplicated.
# ===========================================================================

def test_artifact_revision_negative_byte_size_is_rejected():
    with pytest.raises(ValidationError):
        ArtifactRevision(
            artifact_id="art-0002",
            revision_id="rev-0002",
            logical_path="config/approval.yaml",
            media_type="application/yaml",
            blob_hash="sha256:abc123",
            byte_size=-1,
            parser_version="yaml-parser@1",
            source_role="normative",
        )


def test_artifact_revision_empty_media_type_is_rejected():
    """A second, distinct structural-field-empty case beyond artifact_id
    -- media_type is one of the fields an adversarial probe
    (`media_type=''`) must find checked."""
    with pytest.raises(ValidationError):
        ArtifactRevision(
            artifact_id="art-0003",
            revision_id="rev-0003",
            logical_path="config/approval.yaml",
            media_type="",
            blob_hash="sha256:abc123",
            byte_size=128,
            parser_version="yaml-parser@1",
            source_role="normative",
        )


# ===========================================================================
# SourceRevision: unsorted manifest + manifest_hash
# trust-boundary finding (documented, not silently skipped)
# ===========================================================================

def test_source_revision_unsorted_manifest_is_rejected():
    """Contracts §2.1 'sorted manifest artifact revisions'. An unsorted
    manifest tuple must be rejected, not silently accepted in whatever
    order the caller supplied.

    Manifest entries are `ManifestEntry` objects, sorted by `(artifact_id, artifact_revision_id)`
    (`SourceRevision.__post_init__`'s `sort_key`). Two entries whose
    `artifact_id`s are given in reverse alphabetical order are still
    rejected by the sortedness gate, which runs before the digest check,
    so the placeholder `manifest_hash` value is never evaluated."""
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-0010",
            parent_id=None,
            manifest=(  # deliberately unsorted by artifact_id
                ManifestEntry(artifact_id="zzz-last", artifact_revision_id="ar-1",
                               blob_hash="sha256:blob-zzz"),
                ManifestEntry(artifact_id="aaa-first", artifact_revision_id="ar-2",
                               blob_hash="sha256:blob-aaa"),
            ),
            manifest_hash="sha256:whatever",
            created_by="tester@example.com",
            created_at="2026-09-26T00:00:00Z",
            trigger="import",
            restored_from=None,
        )


def test_source_revision_forged_manifest_hash_is_rejected():
    """A forged manifest_hash is rejected.

    `SourceRevision.__post_init__` VERIFIES
    the caller-supplied `manifest_hash` against `_compute_manifest_hash
    (manifest)` and raise `ValidationError` on any mismatch
    (`core/change_intelligence/v2/schema.py::SourceRevision.__post_init__`).
    A well-formatted but forged/mismatched digest (right shape, wrong value)
    for this two-entry manifest is REJECTED, not silently trusted.

    The manifest is a sorted tuple of `ManifestEntry` objects. The forged
    hash cannot match the recomputed digest of real content."""
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-0011",
            parent_id=None,
            manifest=(
                ManifestEntry(artifact_id="ar-aaa-0001", artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-aaa"),
                ManifestEntry(artifact_id="ar-bbb-0002", artifact_revision_id="rev-2",
                               blob_hash="sha256:blob-bbb"),
            ),
            manifest_hash="sha256:this-does-not-match-the-manifest-content-at-all",
            created_by="tester@example.com",
            created_at="2026-09-26T00:00:00Z",
            trigger="import",
            restored_from=None,
        )


# ===========================================================================
# Decimal-vs-string canonical hash regression: an explicit
# direct `canonical_hash(Decimal) != canonical_hash(str)` assertion.
# ===========================================================================

def test_canonical_hash_decimal_and_string_same_spelling_differ():
    assert canonical_hash(Decimal("1")) != canonical_hash("1")
    assert canonical_hash(Decimal("1000000")) != canonical_hash("1000000")


def test_canonical_hash_rule_candidate_decimal_value_vs_string_value_differ():
    """Same probe as above, but through a real RuleCandidate.value field
    (the original adversarial repro), not just a bare scalar."""
    c1 = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    object.__setattr__(c1, "value", Decimal("1"))
    c2 = _make_rule_candidate(scope={"applies_to": ["dept:finance"]})
    object.__setattr__(c2, "value", "1")
    assert canonical_hash(c1) != canonical_hash(c2)


# ===========================================================================
# Legacy session decode
# ===========================================================================

def test_decode_legacy_session_happy_path():
    legacy_session = {
        "session_id": "sess-0001",
        "baseline_id": "baseline-0001",
        "created_at": "2026-09-26T00:00:00Z",
        "durable_version": 3,
        "work": {"reset_epoch": 2},
        "source_revision_id": HARBOR_SOURCE_REVISION_ID,
        "knowledge_revision_id": "km-0001",
    }
    decoded = decode_legacy_session(legacy_session)
    assert decoded.session_id == "sess-0001"
    assert decoded.source_revision_id == HARBOR_SOURCE_REVISION_ID
    assert decoded.knowledge_revision_id == "km-0001"
    assert decoded.work == {"reset_epoch": 2}


def test_decode_legacy_session_unknown_field_is_preserved():
    """Unknown-field policy: PRESERVE, never silently drop -- same
    discipline as `decode_legacy_knowledge_record`."""
    legacy_session = {
        "session_id": "sess-0002",
        "baseline_id": "baseline-0002",
        "created_at": "2026-09-26T00:00:00Z",
        "durable_version": 1,
        "work": {},
        "source_revision_id": None,
        "knowledge_revision_id": None,
        "future_field_not_yet_modeled": "zzz",
    }
    decoded = decode_legacy_session(legacy_session)
    assert decoded.unknown_fields == {"future_field_not_yet_modeled": "zzz"}


def test_decode_legacy_session_missing_key_is_rejected():
    legacy_session = {
        "session_id": "sess-0003",
        "baseline_id": "baseline-0003",
        "created_at": "2026-09-26T00:00:00Z",
        "durable_version": 1,
        "work": {},
        # "source_revision_id" and "knowledge_revision_id" omitted entirely
    }
    with pytest.raises(KeyError):
        decode_legacy_session(legacy_session)


def test_decode_legacy_session_does_not_infer_revision_ids_from_work():
    """Task card: 'Extension chứa source/KM revisions đầy đủ, không âm
    thầm suy ra từ session counter'. An explicit `None` must be preserved
    as `None`, never silently replaced with a value derived from
    `work['reset_epoch']` or any other counter."""
    legacy_session = {
        "session_id": "sess-0004",
        "baseline_id": "baseline-0004",
        "created_at": "2026-09-26T00:00:00Z",
        "durable_version": 1,
        "work": {"reset_epoch": 7},
        "source_revision_id": None,
        "knowledge_revision_id": None,
    }
    decoded = decode_legacy_session(legacy_session)
    assert decoded.source_revision_id is None
    assert decoded.knowledge_revision_id is None


# ===========================================================================
# manifest_hash content-binding suite, Rule vs
# RuleCandidate, provenance/connector-ID, unsupported-version across every
# persisted DTO boundary, Decimal-vs-lookalike-dict collision regression.
# Covers the verified digest, the Rule type, provenance/connector fields,
# uniform schema_version, and NUL-tag Decimal escaping.
# ===========================================================================

# --- (a)-(e) manifest_hash content-binding suite ----------------------------

def test_manifest_hash_same_content_restore_same_hash_new_event_id():
    """(a) same-content restore: restoring the identical manifest content
    reproduces the SAME manifest_hash while still minting a NEW event id
    -- identity (id) vs content (manifest_hash) are independent, the
    original task-card requirement ('Source hash phân biệt event ID').

    Manifest entries are `ManifestEntry` objects."""
    manifest = (
        ManifestEntry(artifact_id="ar-restore-aaa-0001", artifact_revision_id="rev-1",
                       blob_hash="sha256:blob-restore-aaa"),
        ManifestEntry(artifact_id="ar-restore-bbb-0002", artifact_revision_id="rev-2",
                       blob_hash="sha256:blob-restore-bbb"),
    )
    real_hash = _compute_manifest_hash(manifest)
    original = SourceRevision(
        id="event-restore-orig",
        parent_id=None,
        manifest=manifest,
        manifest_hash=real_hash,
        created_by="tester@example.com",
        created_at="2026-09-26T00:00:00Z",
        trigger="import",
        restored_from=None,
    )
    restored = SourceRevision(
        id="event-restore-new",
        parent_id=original.id,
        manifest=manifest,
        manifest_hash=real_hash,
        created_by="tester@example.com",
        created_at="2026-09-26T02:00:00Z",
        trigger="restore",
        restored_from=original.id,
    )
    assert restored.manifest_hash == original.manifest_hash
    assert restored.id != original.id


def test_manifest_hash_single_artifact_content_change_differs():
    """(b) a REAL content change: the test changes a real blob hash with
    the appropriate identity held fixed. `artifact_id` (and
    `artifact_revision_id`) are held IDENTICAL between the two manifests;
    only `blob_hash` differs -- this is what the identity-vs-content
    decision (`_compute_manifest_hash`'s docstring) actually requires for
    a genuine content change to register: the digest is built from
    `artifact_id` + `blob_hash` only, so a bare ID swap would prove nothing about content identity at
    all -- it would just be a different
    ManifestEntry (different artifact) with unrelated bytes."""
    manifest_a = (ManifestEntry(
        artifact_id="ar-content-fixed-0001", artifact_revision_id="rev-1",
        blob_hash="sha256:blob-content-OLD"),)
    manifest_b = (ManifestEntry(
        artifact_id="ar-content-fixed-0001", artifact_revision_id="rev-1",
        blob_hash="sha256:blob-content-NEW"),)  # same artifact_id/revision_id, blob_hash changed
    hash_a = _compute_manifest_hash(manifest_a)
    hash_b = _compute_manifest_hash(manifest_b)
    assert hash_a != hash_b
    rev_a = SourceRevision(
        id="event-content-a", parent_id=None, manifest=manifest_a,
        manifest_hash=hash_a, created_by="tester@example.com",
        created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None)
    rev_b = SourceRevision(
        id="event-content-b", parent_id=None, manifest=manifest_b,
        manifest_hash=hash_b, created_by="tester@example.com",
        created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None)
    assert rev_a.manifest_hash != rev_b.manifest_hash


def test_manifest_hash_same_blob_hash_new_revision_id_same_artifact_is_unchanged():
    """(b2) Same
    `blob_hash`, a NEW `artifact_revision_id`, same `artifact_id` ->
    digest UNCHANGED. Proves `artifact_revision_id` does NOT participate
    in the digest (the documented identity-vs-content decision): a new
    revision label minted for byte-identical content must reuse the same
    manifest_hash, matching contracts §2.1's 'Source hash phân biệt event
    ID' worked example (restore reproduces the same hash)."""
    manifest_1 = (ManifestEntry(
        artifact_id="ar-samecontent-0001", artifact_revision_id="rev-A",
        blob_hash="sha256:blob-stable"),)
    manifest_2 = (ManifestEntry(
        artifact_id="ar-samecontent-0001", artifact_revision_id="rev-B-NEW",
        blob_hash="sha256:blob-stable"),)  # same artifact_id + blob_hash, new revision id
    assert _compute_manifest_hash(manifest_1) == _compute_manifest_hash(manifest_2)


def test_manifest_hash_same_blob_hash_new_artifact_id_differs():
    """(b3) Same
    `blob_hash`, a NEW `artifact_id` (a different, unrelated artifact that
    coincidentally has identical bytes) -> digest DIFFERS. Proves
    `artifact_id` DOES participate in the digest, so two unrelated
    artifacts with the same content are never confused with one another
    (this is exactly what a manifest of bare content hashes alone, with no
    artifact identity, could not distinguish)."""
    manifest_1 = (ManifestEntry(
        artifact_id="ar-shared-blob-A", artifact_revision_id="rev-1",
        blob_hash="sha256:blob-coincidentally-shared"),)
    manifest_2 = (ManifestEntry(
        artifact_id="ar-shared-blob-B", artifact_revision_id="rev-1",
        blob_hash="sha256:blob-coincidentally-shared"),)  # different artifact, same bytes
    assert _compute_manifest_hash(manifest_1) != _compute_manifest_hash(manifest_2)


def test_manifest_hash_ordering_is_order_sensitive_at_the_digest_function():
    """(c) manifest ORDERING. Read from the actual implementation
    (`core/change_intelligence/v2/schema.py::_compute_manifest_hash`): the
    function joins the manifest sequence AS GIVEN (`"\\n".join(manifest)`)
    -- it does NOT sort before hashing. `SourceRevision.__post_init__`
    separately REQUIRES the manifest to already be given in sorted order
    (`test_source_revision_unsorted_manifest_is_rejected`), so within a
    valid `SourceRevision` there is exactly one legal ordering (sorted) and
    ordering never varies at that boundary. This test pins the documented
    behaviour at the digest-function level directly: two permutations of
    the SAME id set produce DIFFERENT digests from `_compute_manifest_hash`
    itself (it is not order-invariant/canonicalizing on its own -- the
    caller/SourceRevision boundary is what enforces a single canonical
    (sorted) order, not the hash function).

    Manifest entries are `ManifestEntry` objects: three real, distinct entries (own
    `artifact_id`/`artifact_revision_id`/`blob_hash`). Re-verified against
    the ACTUAL current implementation, not assumed: `_compute_manifest_hash`
    builds `content = [{"artifact_id": e.artifact_id, "blob_hash":
    e.blob_hash} for e in manifest]` in the SAME order as the input
    sequence, then `canonical_dumps(content)` -- `canonical_dumps` sorts
    DICT KEYS but is not given a `list`/array-reordering step, so a JSON
    array's element order is preserved exactly as given. Two permutations
    of the same 3-entry set therefore serialize to different array
    orderings and hash differently, confirming the digest function itself
    is order-sensitive (not internally canonicalizing/sorting), exactly as
    this test documents."""
    entries = (
        ManifestEntry(artifact_id="ar-order-aaa-0001", artifact_revision_id="rev-a",
                       blob_hash="sha256:blob-order-aaa"),
        ManifestEntry(artifact_id="ar-order-bbb-0002", artifact_revision_id="rev-b",
                       blob_hash="sha256:blob-order-bbb"),
        ManifestEntry(artifact_id="ar-order-ccc-0003", artifact_revision_id="rev-c",
                       blob_hash="sha256:blob-order-ccc"),
    )
    sorted_order = tuple(sorted(entries, key=lambda e: (e.artifact_id, e.artifact_revision_id)))
    reversed_order = tuple(reversed(sorted_order))
    assert sorted_order != reversed_order  # sanity: genuinely different orderings
    assert _compute_manifest_hash(sorted_order) != _compute_manifest_hash(reversed_order)


def test_manifest_hash_multi_entry_ordering_same_entries_different_order_via_source_revision():
    """A multi-entry ORDERING test using real `ManifestEntry` tuples,
    checked against `SourceRevision`'s sorted-manifest gate: a manifest that is NOT
    sorted by `(artifact_id, artifact_revision_id)` is rejected outright
    by the sortedness check, which runs BEFORE the digest-verification
    check. Consequently there is exactly ONE legal input order for a
    given entry set at the `SourceRevision` boundary (the sorted order);
    a differently-ordered submission of the SAME entries is rejected, not
    silently accepted with a different (but still 'valid') hash. This is
    the ACTUAL documented behaviour -- not 'same canonical digest either
    way' (there is no re-sorting/canonicalization inside `SourceRevision`
    that would make an unsorted submission succeed with an equivalent
    hash); the caller must submit the one sorted order themselves."""
    entries = (
        ManifestEntry(artifact_id="ar-multi-aaa-0001", artifact_revision_id="rev-a",
                       blob_hash="sha256:blob-multi-aaa"),
        ManifestEntry(artifact_id="ar-multi-bbb-0002", artifact_revision_id="rev-b",
                       blob_hash="sha256:blob-multi-bbb"),
    )
    sorted_entries = tuple(sorted(entries, key=lambda e: (e.artifact_id, e.artifact_revision_id)))
    unsorted_entries = tuple(reversed(sorted_entries))
    assert sorted_entries != unsorted_entries  # sanity

    accepted = SourceRevision(
        id="event-multi-sorted", parent_id=None, manifest=sorted_entries,
        manifest_hash=_compute_manifest_hash(sorted_entries),
        created_by="tester@example.com", created_at="2026-09-26T00:00:00Z",
        trigger="import", restored_from=None)
    assert accepted.manifest == sorted_entries

    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-multi-unsorted", parent_id=None, manifest=unsorted_entries,
            manifest_hash=_compute_manifest_hash(unsorted_entries),
            created_by="tester@example.com", created_at="2026-09-26T00:00:00Z",
            trigger="import", restored_from=None)


def test_manifest_hash_malformed_digest_missing_prefix_is_rejected():
    """(d) malformed digest: wrong format (missing the required
    `"sha256:"` prefix `_compute_manifest_hash` always produces) is
    rejected -- it can never equal the recomputed expected digest, so the
    same verification gate that catches a forged digest also catches a
    malformed one."""
    manifest = (ManifestEntry(artifact_id="ar-malformed-aaa-0001",
                               artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-malformed-aaa"),)
    real_hash = _compute_manifest_hash(manifest)
    malformed = real_hash.split("sha256:", 1)[1]  # strip the prefix -> wrong format
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-malformed-0001", parent_id=None, manifest=manifest,
            manifest_hash=malformed, created_by="tester@example.com",
            created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None)


def test_manifest_hash_malformed_digest_non_hex_characters_is_rejected():
    """(d) malformed digest: right-shaped prefix but non-hex garbage body
    is also rejected by the same mismatch gate."""
    manifest = (ManifestEntry(artifact_id="ar-malformed-aaa-0002",
                               artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-malformed-bbb"),)
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-malformed-0002", parent_id=None, manifest=manifest,
            manifest_hash="sha256:not-hex-at-all-zzzz!!!!",
            created_by="tester@example.com",
            created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None)


# (e) forged-but-well-formatted digest: covered by
# test_source_revision_forged_manifest_hash_is_rejected above (the direct
# replacement for the old characterization test).


def test_manifest_hash_injective_encoding_embedded_structural_char_does_not_collide():
    """(f) Regression test for delimiter-unsafe manifest encoding. A
    naive encoding would collide `_compute_manifest_hash(('a\\nb',)) ==
    _compute_manifest_hash(('a', 'b'))` -- a one-entry manifest whose bare
    artifact-revision-id string embeds a literal newline would hash
    IDENTICALLY to a two-entry manifest of the two halves if bare strings
    were joined with `"\\n"` as an unescaped delimiter.

    The `manifest` is made of `ManifestEntry(artifact_id,
    artifact_revision_id, blob_hash)` objects, not bare strings, so that
    literal cannot be reproduced as-is. This test
    proves the underlying CLASS of bug (delimiter-unsafe encoding) is
    fixed, not just that one literal example is inexpressible: it
    constructs a `ManifestEntry`-shaped equivalent of the same risk -- one
    entry whose `artifact_id` embeds a literal newline (`"a\\nb"`) versus
    two entries with `artifact_id`s `"a"` and `"b"` (same `blob_hash` on
    all three, isolating the risk to the `artifact_id` field alone) -- and
    asserts they do NOT collide, because `canonical_dumps`'s JSON escaping
    gives every string field an explicit, unambiguous boundary (the
    embedded newline becomes the two-byte escape sequence `\\` + `n`
    inside its own quoted string), unlike raw delimiter-joining.
    """
    one_entry_with_embedded_newline = (ManifestEntry(
        artifact_id="a\nb", artifact_revision_id="rev-x", blob_hash="sha256:blob-x"),)
    two_entries_split_on_the_newline = (
        ManifestEntry(artifact_id="a", artifact_revision_id="rev-x", blob_hash="sha256:blob-x"),
        ManifestEntry(artifact_id="b", artifact_revision_id="rev-x", blob_hash="sha256:blob-x"),
    )
    assert _compute_manifest_hash(one_entry_with_embedded_newline) != _compute_manifest_hash(
        two_entries_split_on_the_newline)


# --- Rule vs RuleCandidate ---------------------------------------------

def _rule_kwargs(**overrides):
    kwargs = dict(
        rule_id="rule-0001",
        version=1,
        candidate_ids=("cand-0001",),
        normalized_semantics={"predicate": "requires_manager_approval_above"},
        confirmation_state="CONFIRMED",
        evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
        decision_ids=("dec-0001",),
        scope={"applies_to": ["dept:finance"]},
        schema_version="1.0.0",
    )
    kwargs.update(overrides)
    return kwargs


def test_rule_happy_path_construction_all_fields():
    rule = Rule(**_rule_kwargs())
    assert rule.rule_id == "rule-0001"
    assert rule.version == 1
    assert rule.candidate_ids == ("cand-0001",)
    assert rule.confirmation_state == "CONFIRMED"
    assert rule.evidence
    assert rule.decision_ids == ("dec-0001",)
    assert rule.scope == {"applies_to": ["dept:finance"]}


def test_rule_bad_confirmation_state_enum_is_rejected():
    with pytest.raises(ValidationError):
        Rule(**_rule_kwargs(confirmation_state="MADE_UP_STATE"))


def test_rule_confirmation_state_is_one_of_the_documented_values():
    """Pins the judgment-call enum set (schema.py's
    `RULE_CONFIRMATION_STATES`) so a future silent widening/narrowing is
    visible."""
    assert "CONFIRMED" in RULE_CONFIRMATION_STATES
    assert "MADE_UP_STATE" not in RULE_CONFIRMATION_STATES


def test_rule_missing_candidate_ids_is_rejected():
    """Rule must trace back to at least one RuleCandidate (candidate
    ancestry) -- empty candidate_ids is rejected."""
    with pytest.raises(ValidationError):
        Rule(**_rule_kwargs(candidate_ids=()))


def test_rule_empty_scope_is_rejected():
    """Same 'never applies to all' discipline as RuleCandidate.scope."""
    with pytest.raises(ValidationError):
        Rule(**_rule_kwargs(scope={}))


def test_rule_missing_scope_wrong_type_is_rejected():
    with pytest.raises(ValidationError):
        Rule(**_rule_kwargs(scope="applies-to-everything"))


def test_rule_confirmed_without_decision_ids_is_rejected():
    """Read from the ACTUAL implementation
    (`core/change_intelligence/v2/schema.py::Rule.__post_init__`):
    `confirmation_state == "CONFIRMED"` requires non-empty `decision_ids`
    (contracts §2.1 'decision refs' -- a confirmed rule must cite the
    human decision that confirmed it)."""
    with pytest.raises(ValidationError):
        Rule(**_rule_kwargs(confirmation_state="CONFIRMED", decision_ids=()))


def test_rule_draft_without_decision_ids_is_accepted():
    """Non-CONFIRMED states are NOT subject to the decision_ids-required
    gate -- a DRAFT rule may legitimately have no decision refs yet."""
    rule = Rule(**_rule_kwargs(confirmation_state="DRAFT", decision_ids=()))
    assert rule.decision_ids == ()


def test_rule_version_must_be_a_positive_int():
    with pytest.raises(ValidationError):
        Rule(**_rule_kwargs(version=0))


def test_rule_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        Rule(**_rule_kwargs(schema_version="99.0.0"))


# --- provenance / connector-ID ------------------------------------------

def test_artifact_revision_provenance_happy_path_derived():
    rev = ArtifactRevision(
        artifact_id="art-prov-0001", revision_id="rev-prov-0001",
        logical_path="config/approval.yaml", media_type="application/yaml",
        blob_hash="sha256:abc123", byte_size=128, parser_version="yaml-parser@1",
        source_role="normative", provenance="derived")
    assert rev.provenance == "derived"


def test_artifact_revision_provenance_defaults_to_original_upload():
    rev = ArtifactRevision(
        artifact_id="art-prov-0002", revision_id="rev-prov-0002",
        logical_path="config/approval.yaml", media_type="application/yaml",
        blob_hash="sha256:abc123", byte_size=128, parser_version="yaml-parser@1",
        source_role="normative")
    assert rev.provenance == "original_upload"


def test_artifact_revision_provenance_bad_enum_is_rejected():
    with pytest.raises(ValidationError):
        ArtifactRevision(
            artifact_id="art-prov-0003", revision_id="rev-prov-0003",
            logical_path="config/approval.yaml", media_type="application/yaml",
            blob_hash="sha256:abc123", byte_size=128, parser_version="yaml-parser@1",
            source_role="normative", provenance="fabricated_out_of_thin_air")


def test_artifact_revision_provenance_enum_values_pinned():
    assert ARTIFACT_PROVENANCE_VALUES == frozenset(
        {"original_upload", "derived", "restored"})


def test_source_revision_connector_id_happy_path():
    manifest = (ManifestEntry(artifact_id="ar-connector-0001", artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-connector-0001"),)
    rev = SourceRevision(
        id="event-connector-0001", parent_id=None, manifest=manifest,
        manifest_hash=_compute_manifest_hash(manifest), created_by="tester@example.com",
        created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None,
        source_connector_id="connector:harbor-import@1")
    assert rev.source_connector_id == "connector:harbor-import@1"


def test_source_revision_connector_id_none_default_is_accepted():
    """`source_connector_id` is optional, defaulting to
    `None` -- not every history event originates from an external
    connector (e.g. a restore's trigger is 'restore', not a fresh
    import)."""
    manifest = (ManifestEntry(artifact_id="ar-connector-0002", artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-connector-0002"),)
    rev = SourceRevision(
        id="event-connector-0002", parent_id=None, manifest=manifest,
        manifest_hash=_compute_manifest_hash(manifest), created_by="tester@example.com",
        created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None)
    assert rev.source_connector_id is None


def test_source_revision_connector_id_blank_string_is_rejected():
    manifest = (ManifestEntry(artifact_id="ar-connector-0003", artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-connector-0003"),)
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-connector-0003", parent_id=None, manifest=manifest,
            manifest_hash=_compute_manifest_hash(manifest), created_by="tester@example.com",
            created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None,
            source_connector_id="   ")


# --- unsupported schema_version across every DTO boundary (13 domain
# types + Rule = 14). Already covered elsewhere in this file:
# RuleCandidate (test_unsupported_schema_version_rejected_clearly),
# Observation, KnowledgeRevision, Finding, PatchProposal, Rule (above).
# Added here: ArtifactRevision, SourceRevision, RelationCandidate/Relation,
# HumanDecision, AnalysisRun, Approval, VerificationReport,
# RecoveryProposal -- every remaining schema_version-bearing type. ---------

def test_artifact_revision_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        ArtifactRevision(
            artifact_id="art-ver-0001", revision_id="rev-ver-0001",
            logical_path="config/approval.yaml", media_type="application/yaml",
            blob_hash="sha256:abc123", byte_size=128, parser_version="yaml-parser@1",
            source_role="normative", schema_version="99.0.0")


def test_source_revision_unsupported_schema_version_is_rejected():
    manifest = (ManifestEntry(artifact_id="ar-ver-0001", artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-ver-0001"),)
    with pytest.raises(SchemaVersionError):
        SourceRevision(
            id="event-ver-0001", parent_id=None, manifest=manifest,
            manifest_hash=_compute_manifest_hash(manifest), created_by="tester@example.com",
            created_at="2026-09-26T00:00:00Z", trigger="import", restored_from=None,
            schema_version="99.0.0")


def test_relation_candidate_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        RelationCandidate(**_relation_candidate_kwargs(schema_version="99.0.0"))


def test_relation_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        Relation(**_relation_candidate_kwargs(
            lifecycle_state="CONFIRMED", schema_version="99.0.0"), decision_id="dec-ver-0001")


def test_human_decision_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        HumanDecision(**_human_decision_kwargs(schema_version="99.0.0"))


def test_analysis_run_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        AnalysisRun(**_analysis_run_kwargs(schema_version="99.0.0"))


def test_approval_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        Approval(**_approval_kwargs(schema_version="99.0.0"))


def test_verification_report_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        VerificationReport(**_verification_report_kwargs(schema_version="99.0.0"))


def test_recovery_proposal_unsupported_schema_version_is_rejected():
    with pytest.raises(SchemaVersionError):
        RecoveryProposal(**_recovery_proposal_kwargs(schema_version="99.0.0"))


# --- Decimal-vs-lookalike-dict collision regression (permanent
# regression test) --------------------------

def test_canonical_hash_decimal_vs_lookalike_dict_collision_regression():
    """PERMANENT REGRESSION TEST for an adversarial probe: `canonical_hash(Decimal('1')) == canonical_hash({'__type__':
    'decimal','value':'1'})` was `True` before the NUL-embedding tag
    fix (a confirmed collision). Must be `False` -- a
    genuine user dict of that ordinary shape must never be mistaken for a
    real Decimal tag."""
    assert canonical_hash(Decimal("1")) != canonical_hash(
        {"__type__": "decimal", "value": "1"})


def test_canonical_hash_reserved_decimal_tag_key_escaping_still_distinguishes():
    """A plain user dict which happens to contain the ACTUAL reserved
    escape key (`_DECIMAL_TAG_KEY`,
    `core/change_intelligence/v2/serialize.py`) must still hash
    correctly/distinguishably from a real `Decimal` -- proving the
    escaping mechanism itself works, not just the tag-shape
    collision case above."""
    adversarial_dict = {_DECIMAL_TAG_KEY: "1"}
    assert canonical_hash(Decimal("1")) != canonical_hash(adversarial_dict)
    # The escape mechanism must also make two DIFFERENT adversarial dicts
    # (one mimicking the decimal tag, one mimicking the escape wrapper)
    # distinguishable from each other and from a real Decimal.
    adversarial_escape_dict = {_ESCAPE_KEY: {"anything": "1"}}
    assert canonical_hash(adversarial_dict) != canonical_hash(adversarial_escape_dict)
    assert canonical_hash(Decimal("1")) != canonical_hash(adversarial_escape_dict)


def test_canonical_hash_decimal_still_deterministic_after_escaping_fix():
    """Sanity: the escaping fix must not have broken basic
    determinism -- two independently constructed Decimals with the same
    value still hash identically."""
    assert canonical_hash(Decimal("42.50")) == canonical_hash(Decimal("42.50"))


# ===========================================================================
# Duplicate-artifact-ID
# manifest uniqueness gate, RuleCandidate.operator validation, malformed/
# impossible/missing-timezone/reversed/equal-instant effective intervals for
# RuleCandidate AND HumanDecision, and malformed required created_at on
# SourceRevision/HumanDecision/AnalysisRun/Approval.
# ===========================================================================


def _round6_rule_candidate_kwargs(**overrides):
    """Local helper, deliberately NOT reusing `_make_rule_candidate` --
    a shared helper that hardcodes a value under an
    active gate silently couples every caller to its exact edge behaviour,
    so a fresh, explicit kwargs dict is safer for a new batch of
    operator/effective-interval tests than threading new parameters through
    the existing helper."""
    kwargs = dict(
        candidate_id="cand-r6-0001",
        subject="purchase_approval_threshold",
        predicate="requires_manager_approval_above",
        value=Money(amount=Decimal("1000000"), currency="JPY"),
        applicability="RECOGNIZED",
        modality="must",
        source_revision_id=HARBOR_SOURCE_REVISION_ID,
        artifact_revision_id=HARBOR_ARTIFACT_REVISION_ID,
        evidence=(_make_evidence_ref(HARBOR_SOURCE_REVISION_ID, HARBOR_ARTIFACT_REVISION_ID),),
        scope={"applies_to": ["dept:finance"]},
        schema_version="1.0.0",
    )
    kwargs.update(overrides)
    return kwargs


# --- (1) SourceRevision manifest: duplicate artifact_id uniqueness gate ---

def test_source_revision_duplicate_artifact_id_different_revision_labels_is_rejected():
    """Counterexample shape:
    two manifest entries sharing one `artifact_id` but different
    `artifact_revision_id`/`blob_hash`. Sorted order, well-typed entries,
    and a correctly recomputed `manifest_hash` -- the ONLY thing wrong is
    the duplicate `artifact_id`, so a green result here would prove
    nothing if it came from the sortedness or digest gate instead. The
    `match=` regex pins the uniqueness gate's own message text
    (`core/change_intelligence/v2/schema.py`'s
    "at most one active revision per artifact_id"), not merely "some
    ValidationError fired"."""
    manifest = (
        ManifestEntry(artifact_id="ar-dup-0001", artifact_revision_id="rev-1",
                       blob_hash="sha256:blobA"),
        ManifestEntry(artifact_id="ar-dup-0001", artifact_revision_id="rev-2",
                       blob_hash="sha256:blobB"),
    )
    with pytest.raises(ValidationError, match="at most one active revision per artifact_id"):
        SourceRevision(
            id="event-dup-0001", parent_id=None, manifest=manifest,
            manifest_hash=_compute_manifest_hash(manifest),
            created_by="tester@example.com", created_at="2026-09-26T00:00:00Z",
            trigger="import", restored_from=None)


def test_source_revision_duplicate_artifact_id_reassigned_blobs_is_rejected():
    """Reassignment counterexample: two manifests
    with the SAME `(artifact_id, blob_hash)` multiset but revision labels/
    blob pairing swapped used to hash differently despite equal content
    sets. Both must now be REJECTED outright by the uniqueness gate (not
    accepted with two different hashes)."""
    manifest_1 = (
        ManifestEntry(artifact_id="a", artifact_revision_id="r1", blob_hash="blobA"),
        ManifestEntry(artifact_id="a", artifact_revision_id="r2", blob_hash="blobB"),
    )
    manifest_2 = (
        ManifestEntry(artifact_id="a", artifact_revision_id="r1", blob_hash="blobB"),
        ManifestEntry(artifact_id="a", artifact_revision_id="r2", blob_hash="blobA"),
    )
    for index, manifest in enumerate((manifest_1, manifest_2)):
        with pytest.raises(ValidationError, match="at most one active revision per artifact_id"):
            SourceRevision(
                id=f"event-dup-reassigned-{index}", parent_id=None, manifest=manifest,
                manifest_hash=_compute_manifest_hash(manifest),
                created_by="tester@example.com", created_at="2026-09-26T00:00:00Z",
                trigger="import", restored_from=None)


def test_source_revision_duplicate_artifact_id_unsorted_still_uniqueness_gate_first():
    """The uniqueness check runs BEFORE the sortedness check in
    `SourceRevision.__post_init__` (read from source, not assumed): even an
    UNSORTED duplicate-artifact_id manifest must fail with the uniqueness
    gate's own message, not the sortedness gate's ('must be sorted by
    (artifact_id, artifact_revision_id)') -- proving the failure really is
    the new uniqueness gate and not an incidental earlier/later check."""
    manifest = (
        ManifestEntry(artifact_id="a", artifact_revision_id="r2", blob_hash="blobB"),
        ManifestEntry(artifact_id="a", artifact_revision_id="r1", blob_hash="blobA"),
    )
    with pytest.raises(ValidationError, match="at most one active revision per artifact_id"):
        SourceRevision(
            id="event-dup-unsorted", parent_id=None, manifest=manifest,
            manifest_hash=_compute_manifest_hash(manifest),
            created_by="tester@example.com", created_at="2026-09-26T00:00:00Z",
            trigger="import", restored_from=None)


def test_source_revision_non_duplicate_manifest_still_accepted():
    """Control: a manifest with genuinely distinct `artifact_id`s (no
    duplication) is unaffected by the uniqueness gate -- proves the
    new check does not over-reject."""
    manifest = (
        ManifestEntry(artifact_id="ar-nodup-0001", artifact_revision_id="r1",
                       blob_hash="blobA"),
        ManifestEntry(artifact_id="ar-nodup-0002", artifact_revision_id="r1",
                       blob_hash="blobB"),
    )
    revision = SourceRevision(
        id="event-nodup-0001", parent_id=None, manifest=manifest,
        manifest_hash=_compute_manifest_hash(manifest),
        created_by="tester@example.com", created_at="2026-09-26T00:00:00Z",
        trigger="import", restored_from=None)
    assert revision.manifest == manifest


# --- (2) RuleCandidate.operator validation --------------------------------

@pytest.mark.parametrize("op", sorted([">", ">=", "<", "<=", "==", "!="]))
def test_rule_candidate_operator_each_valid_form_is_accepted(op):
    """Every one of the six documented strict/inclusive comparison
    operators is individually accepted and roundtrips unchanged."""
    candidate = RuleCandidate(**_round6_rule_candidate_kwargs(operator=op))
    assert candidate.operator == op


def test_rule_candidate_operator_none_default_is_accepted():
    """`operator` is optional -- not every predicate is a comparison."""
    candidate = RuleCandidate(**_round6_rule_candidate_kwargs())
    assert candidate.operator is None


def test_rule_candidate_operator_unsupported_string_is_rejected():
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(operator="=>"))


def test_rule_candidate_operator_case_variant_is_rejected():
    """Wrong spelling/casing of a real operator (e.g. a spelled-out word)
    is not silently normalized."""
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(operator="GTE"))


def test_rule_candidate_operator_wrong_type_int_is_rejected():
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(operator=1))


def test_rule_candidate_operator_unhashable_list_is_rejected():
    """A list is not only the wrong type but UNHASHABLE -- `in
    COMPARISON_OPERATORS` (a frozenset) would raise `TypeError` if it were
    ever reached with an unhashable value. The type check must short-
    circuit BEFORE the membership test, so this raises `ValidationError`,
    never an uncaught `TypeError`."""
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(operator=["=="]))


def test_rule_candidate_operator_unhashable_dict_is_rejected():
    """Same as above for a `dict` input, the other common unhashable
    type."""
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(operator={"op": ">"}))


def test_rule_candidate_operator_empty_string_is_rejected():
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(operator=""))


# --- (3) RuleCandidate effective interval validation ----------------------

def test_rule_candidate_effective_start_malformed_is_rejected():
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(effective_start="not-a-date"))


def test_rule_candidate_effective_start_impossible_date_is_rejected():
    """2026 is not a leap year -- Feb 30th does not exist in any year."""
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(
            effective_start="2026-02-30T00:00:00Z"))


def test_rule_candidate_effective_start_missing_timezone_is_rejected():
    """A local/date-only time cannot determine an instant; the
    `_AWARE_TIMESTAMP` regex requires an explicit `Z` or `+HH:MM`/`-HH:MM`
    offset."""
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(
            effective_start="2026-09-26T00:00:00"))


def test_rule_candidate_effective_interval_reversed_is_rejected():
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(
            effective_start="2026-09-26T00:00:00Z",
            effective_end="2026-09-25T00:00:00Z"))


def test_rule_candidate_effective_interval_equal_instant_one_exclusive_is_rejected():
    """An interval collapsed to a single instant is only valid when BOTH
    boundary flags are inclusive; any exclusive flag on an equal-instant
    interval makes it empty."""
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(
            effective_start="2026-09-26T00:00:00Z",
            effective_end="2026-09-26T00:00:00Z",
            effective_start_inclusive=True, effective_end_inclusive=False))


def test_rule_candidate_effective_interval_equal_instant_both_inclusive_is_accepted():
    candidate = RuleCandidate(**_round6_rule_candidate_kwargs(
        effective_start="2026-09-26T00:00:00Z",
        effective_end="2026-09-26T00:00:00Z",
        effective_start_inclusive=True, effective_end_inclusive=True))
    assert candidate.effective_start == candidate.effective_end
    assert candidate.effective_start_inclusive is True
    assert candidate.effective_end_inclusive is True


def test_rule_candidate_effective_interval_valid_forward_roundtrips_inclusive_flags():
    """A valid, non-degenerate forward interval preserves its EXACT
    inclusive/exclusive flag combination (not just accepted, but the
    boundary semantics survive construction unchanged)."""
    candidate = RuleCandidate(**_round6_rule_candidate_kwargs(
        effective_start="2026-09-26T00:00:00Z",
        effective_end="2026-12-31T23:59:59Z",
        effective_start_inclusive=False, effective_end_inclusive=True))
    assert candidate.effective_start_inclusive is False
    assert candidate.effective_end_inclusive is True


def test_rule_candidate_effective_interval_default_flags_are_half_open():
    """Documented default: `[start, end)` -- start inclusive, end
    exclusive -- when a caller does not override the flags."""
    candidate = RuleCandidate(**_round6_rule_candidate_kwargs(
        effective_start="2026-09-26T00:00:00Z",
        effective_end="2026-12-31T23:59:59Z"))
    assert candidate.effective_start_inclusive is True
    assert candidate.effective_end_inclusive is False


def test_rule_candidate_effective_interval_flag_wrong_type_is_rejected():
    with pytest.raises(ValidationError):
        RuleCandidate(**_round6_rule_candidate_kwargs(
            effective_start="2026-09-26T00:00:00Z",
            effective_end="2026-12-31T23:59:59Z",
            effective_start_inclusive="yes"))


# --- (4) HumanDecision effective interval + created_at validation ---------

def test_human_decision_effective_start_malformed_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(effective_start="not-a-date"))


def test_human_decision_effective_start_impossible_date_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(effective_start="2026-13-01T00:00:00Z"))


def test_human_decision_effective_start_missing_timezone_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(effective_start="2026-09-26T00:00:00"))


def test_human_decision_effective_interval_reversed_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(
            effective_start="2026-09-26T00:00:00Z",
            effective_end="2026-09-25T00:00:00Z"))


def test_human_decision_effective_interval_equal_instant_one_exclusive_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(
            effective_start="2026-09-26T00:00:00Z",
            effective_end="2026-09-26T00:00:00Z",
            effective_start_inclusive=True, effective_end_inclusive=False))


def test_human_decision_effective_interval_equal_instant_both_inclusive_is_accepted():
    decision = HumanDecision(**_human_decision_kwargs(
        effective_start="2026-09-26T00:00:00Z",
        effective_end="2026-09-26T00:00:00Z",
        effective_start_inclusive=True, effective_end_inclusive=True))
    assert decision.effective_start == decision.effective_end


def test_human_decision_effective_interval_valid_forward_roundtrips_inclusive_flags():
    decision = HumanDecision(**_human_decision_kwargs(
        effective_start="2026-09-26T00:00:00Z",
        effective_end="2026-12-31T23:59:59Z",
        effective_start_inclusive=False, effective_end_inclusive=True))
    assert decision.effective_start_inclusive is False
    assert decision.effective_end_inclusive is True


def test_human_decision_malformed_created_at_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(created_at="not-a-timestamp"))


def test_human_decision_created_at_missing_timezone_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(created_at="2026-09-26T00:00:00"))


def test_human_decision_created_at_impossible_date_is_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**_human_decision_kwargs(created_at="2026-02-30T00:00:00Z"))


# --- (5) Malformed required created_at on the other three event DTOs -----

def test_source_revision_malformed_created_at_is_rejected():
    manifest = (ManifestEntry(artifact_id="ar-created-at-0001",
                               artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-created-at"),)
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-created-at-0001", parent_id=None, manifest=manifest,
            manifest_hash=_compute_manifest_hash(manifest),
            created_by="tester@example.com", created_at="not-a-timestamp",
            trigger="import", restored_from=None)


def test_source_revision_created_at_missing_timezone_is_rejected():
    manifest = (ManifestEntry(artifact_id="ar-created-at-0002",
                               artifact_revision_id="rev-1",
                               blob_hash="sha256:blob-created-at-2"),)
    with pytest.raises(ValidationError):
        SourceRevision(
            id="event-created-at-0002", parent_id=None, manifest=manifest,
            manifest_hash=_compute_manifest_hash(manifest),
            created_by="tester@example.com", created_at="2026-09-26T00:00:00",
            trigger="import", restored_from=None)


def test_analysis_run_malformed_created_at_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisRun(**_analysis_run_kwargs(created_at="not-a-timestamp"))


def test_analysis_run_created_at_missing_timezone_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisRun(**_analysis_run_kwargs(created_at="2026-09-26T00:00:00"))


def test_approval_malformed_created_at_is_rejected():
    with pytest.raises(ValidationError):
        Approval(**_approval_kwargs(created_at="not-a-timestamp"))


def test_approval_created_at_missing_timezone_is_rejected():
    with pytest.raises(ValidationError):
        Approval(**_approval_kwargs(created_at="2026-09-26T00:00:00"))


def test_approval_created_at_impossible_date_is_rejected():
    with pytest.raises(ValidationError):
        Approval(**_approval_kwargs(created_at="2026-02-30T00:00:00Z"))
