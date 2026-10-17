# -*- coding: utf-8 -*-
"""Legacy compatibility DTOs.

Decodes legacy shapes into lossless v2-facing DTOs, without rewriting old
serialized enum values in place. Requirements: an old record decodes
without losing fields; legacy enum values are not rewritten in place; legacy
session objects get compatibility DTOs whose extension carries full
source/KM revisions and never infers them silently from a session counter.

`decode_legacy_session` decodes legacy sessions, and
`decode_legacy_knowledge_record` never silently discards unrecognised input
fields (see "Unknown-field policy" below).

Unknown-field policy (documented, chosen deliberately): PRESERVE, never
silently discard. Any input key not in a decoder's known field set is
carried into the returned DTO's `unknown_fields` dict verbatim, keyed by
the original field name. This satisfies "không mất trường" for fields this
schema does not anticipate, without requiring every legacy caller to
be re-validated against an exact closed field set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Legacy top-level fields (`core/change_intelligence/knowledge.py::
#: KnowledgeRecord`). All are required: a legacy record missing one of
#: these (most notably `derived_from`, which carries lineage/I2) is
#: rejected rather than silently backfilled with an empty default that
#: would hide real data loss in the legacy store.
_LEGACY_KM_FIELDS = (
    "id", "knowledge_type", "semantic_id", "statement", "evidence",
    "derived_from", "state", "alignment", "representations", "gap_id",
    "reason", "km_version",
)


@dataclass(frozen=True)
class LegacyKnowledgeRecordDTO:
    """Lossless carrier for a legacy `KnowledgeRecord` dict. Every legacy
    field is exposed unchanged by attribute name; `state`/`alignment` keep
    whatever old string spelling the legacy record had -- this DTO never
    rewrites them to a new v2 spelling. `unknown_fields` carries any input
    key this decoder did not recognise, verbatim, so a caller extending the
    legacy shape later does not lose data through this DTO (unknown-field
    policy: PRESERVE, see module docstring).
    """

    id: str
    knowledge_type: str
    semantic_id: str
    statement: str
    evidence: List[Any]
    derived_from: List[str]
    state: str
    alignment: str
    representations: List[Any]
    gap_id: str
    reason: str
    km_version: int
    unknown_fields: Dict[str, Any] = field(default_factory=dict)


def decode_legacy_knowledge_record(record: dict) -> LegacyKnowledgeRecordDTO:
    """Decode a legacy serialized `KnowledgeRecord` dict into
    `LegacyKnowledgeRecordDTO`, preserving every field's value verbatim.

    Raises `KeyError` if a required legacy field is missing -- this is
    deliberate: a legacy record missing e.g. `derived_from` is a data-loss
    bug in the legacy store, not something to paper over with a silent
    default. Extra/unrecognised keys are NOT dropped -- they are collected
    into `unknown_fields` (see module docstring's unknown-field policy).
    """
    missing = [key for key in _LEGACY_KM_FIELDS if key not in record]
    if missing:
        raise KeyError(
            f"legacy knowledge record missing required field(s): {missing}")
    unknown = {key: value for key, value in record.items()
               if key not in _LEGACY_KM_FIELDS}
    return LegacyKnowledgeRecordDTO(
        **{key: record[key] for key in _LEGACY_KM_FIELDS},
        unknown_fields=unknown,
    )


# ---------------------------------------------------------------------------
# Legacy session compatibility DTO
# ---------------------------------------------------------------------------

#: Legacy session fields this decoder recognises by name, mirroring
#: `app/api/sessions.py::JudgeSession`'s own attributes (`session_id`,
#: `baseline_id`, `created_at`, `durable_version`, `work`) PLUS two
#: extension fields that must be explicit, not inferred:
#: `source_revision_id` and `knowledge_revision_id`. These two are NOT
#: derived from `work["reset_epoch"]` or any other session counter --
#: `JudgeSession` itself carries no source/KM revision pointer at all
#: today (it only has `overlay`/`lineage`/`work`), so a caller decoding a
#: legacy session into this DTO must supply the current source/KM revision
#: ids explicitly as part of the input dict; a missing one is rejected,
#: never silently defaulted to `None` or guessed from `work`'s contents.
_LEGACY_SESSION_FIELDS = (
    "session_id", "baseline_id", "created_at", "durable_version", "work",
    "source_revision_id", "knowledge_revision_id",
)


@dataclass(frozen=True)
class LegacySessionDTO:
    """Lossless carrier for a legacy `JudgeSession`-shaped input dict.

    `work` is preserved as a raw dict, exactly as given -- this DTO does
    not attempt to decode `session.work`'s values through
    `app/api/codec.py` (that is a separate, session-storage-layer concern,
    not a schema-compatibility one). `source_revision_id` and
    `knowledge_revision_id` are explicit, required extension fields (see
    `_LEGACY_SESSION_FIELDS` docstring above) -- they are never silently
    inferred from `work["reset_epoch"]` or `_counter`-style session state.
    `unknown_fields` preserves any input key this decoder does not
    recognise (same PRESERVE policy as `LegacyKnowledgeRecordDTO`).
    """

    session_id: str
    baseline_id: str
    created_at: str
    durable_version: int
    work: Dict[str, Any]
    source_revision_id: Optional[str]
    knowledge_revision_id: Optional[str]
    unknown_fields: Dict[str, Any] = field(default_factory=dict)


def decode_legacy_session(record: dict) -> LegacySessionDTO:
    """Decode a legacy session-shaped dict into `LegacySessionDTO`.

    Every key in `_LEGACY_SESSION_FIELDS` must be PRESENT in `record`
    (though `source_revision_id`/`knowledge_revision_id` may carry an
    explicit `None` value -- 'the workspace has not pinned one yet' is a
    real, valid state; a missing KEY is a caller bug and is rejected, same
    as `decode_legacy_knowledge_record`'s `derived_from` gate). Keys not in
    `_LEGACY_SESSION_FIELDS` are preserved into `unknown_fields`, never
    silently dropped.
    """
    missing = [key for key in _LEGACY_SESSION_FIELDS if key not in record]
    if missing:
        raise KeyError(
            f"legacy session record missing required field(s): {missing}")
    if not isinstance(record["work"], dict):
        raise ValueError("legacy session record's 'work' must be a dict")
    unknown = {key: value for key, value in record.items()
               if key not in _LEGACY_SESSION_FIELDS}
    return LegacySessionDTO(
        session_id=record["session_id"],
        baseline_id=record["baseline_id"],
        created_at=record["created_at"],
        durable_version=record["durable_version"],
        work=dict(record["work"]),
        source_revision_id=record["source_revision_id"],
        knowledge_revision_id=record["knowledge_revision_id"],
        unknown_fields=unknown,
    )
