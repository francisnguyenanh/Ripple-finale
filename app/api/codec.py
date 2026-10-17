# -*- coding: utf-8 -*-
"""Versioned JSON DTOs for the closed set of session domain objects.

Tuples and enums carry explicit tags and survive actual JSON transport.
Decoding uses static type registries and rejects unsupported schema versions.
Simulation handler callables are not persisted; their recorded outcomes are.
_IntentRef is retained only to decode historical B2 records. New proposals
carry core.intent.Intent, never a second API intent model.
"""

from __future__ import annotations

import dataclasses
import enum

from core.change_intelligence.action_plan import Action, ActionPlan
from core.change_intelligence.apply_gate import ApprovalRecord
from core.change_intelligence.behavioral_diff import OperationalDiff, OutcomeShift, Rate
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.impact import ImpactNode, ImpactResult
from core.change_intelligence.risk import Dimension, RiskAssessment
from core.change_intelligence.shadow_change import ShadowChange
from core.change_intelligence.simulation import CaseResult, Outcome, SimulationResult
from core.change_intelligence.states import AlignmentState, ChangeSetState, Transition
from core.change_intelligence.structured_diff import StructuredEdit
from core.change_intelligence.verification import (
    EditOutcome,
    UnexpectedChange,
    VerificationResult,
)

from core.change_intelligence.intent_candidate import IntentCandidate
from core.change_intelligence.intent import Intent
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.alignment import AlignmentOutcome, Representation, Gap
from core.change_intelligence.events import Event
from core.change_intelligence.states import IntentState, ChangeDecision

SCHEMA_VERSION = 1


class UnknownRecordKind(Exception):
    """`decode_record`/`from_portable` was handed a type this codec does not
    whitelist, or an envelope this codec did not produce."""


@dataclasses.dataclass(frozen=True)
class _IntentRef:
    """Legacy B2 DTO, read compatibility only; never created by handlers."""

    intent_id: str
    version: int
    target: str
    desired_value: object
    reason: str
    requested_by: str
    requested_at: str


#: The closed set of dataclasses this codec may reconstruct, by NAME --
#: never by importing whatever string happens to be stored. Adding a type
#: here is a code change reviewed like any other; decode can never reach
#: outside it.
_DATACLASSES = (
    Action, ActionPlan, ApprovalRecord, CaseResult, ChangeSet, Dimension,
    EditOutcome, ImpactNode, ImpactResult, _IntentRef, OperationalDiff,
    Outcome, OutcomeShift, Rate, RiskAssessment, ShadowChange,
    StructuredEdit, Transition, UnexpectedChange, VerificationResult,
    SimulationResult, IntentCandidate, Intent, EvidenceRef, AlignmentOutcome, Representation, Gap, Event,
)

#: The closed set of enums this codec may reconstruct. Same reasoning.
_ENUMS = (AlignmentState, ChangeSetState, IntentState, ChangeDecision)

_TYPES_BY_NAME: dict = {cls.__name__: cls for cls in _DATACLASSES}
_ENUMS_BY_NAME: dict = {cls.__name__: cls for cls in _ENUMS}

assert len(_TYPES_BY_NAME) == len(_DATACLASSES), "duplicate dataclass name in codec whitelist"
assert len(_ENUMS_BY_NAME) == len(_ENUMS), "duplicate enum name in codec whitelist"


def to_portable(obj):
    """Recursively turn dataclasses/enums/tuples into a JSON-shaped tree.

    Lists and dicts are walked but not tagged: they already round-trip
    through `json` on their own, and tagging them would only make the
    envelope harder to read for no gain.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        name = type(obj).__name__
        if name not in _TYPES_BY_NAME or _TYPES_BY_NAME[name] is not type(obj):
            raise UnknownRecordKind(
                f"{type(obj)!r} is not on the codec's dataclass whitelist; "
                f"add it to app/api/codec.py::_DATACLASSES deliberately "
                f"before storing it in session.work")
        return {
            "__dataclass__": name,
            "fields": {f.name: to_portable(getattr(obj, f.name))
                       for f in dataclasses.fields(obj)},
        }
    if isinstance(obj, enum.Enum):
        name = type(obj).__name__
        if name not in _ENUMS_BY_NAME or _ENUMS_BY_NAME[name] is not type(obj):
            raise UnknownRecordKind(
                f"{type(obj)!r} is not on the codec's enum whitelist")
        return {"__enum__": name, "value": obj.value}
    if isinstance(obj, tuple):
        return {"__tuple__": [to_portable(x) for x in obj]}
    if isinstance(obj, list):
        return [to_portable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_portable(v) for k, v in obj.items()}
    return obj


def from_portable(obj):
    """The inverse of `to_portable`. Reconstructs real objects, not dicts.

    A decoded dataclass is built via `cls(**fields)`, so its own
    `__post_init__` runs again -- a `ChangeSet` or `StructuredEdit` that
    comes back from this function is held to exactly the same invariants
    as one built directly in code, not a degraded stand-in. `cls` is
    looked up in the static `_TYPES_BY_NAME` whitelist; a name that is not
    in it is refused rather than imported.
    """
    if isinstance(obj, dict):
        if "__dataclass__" in obj:
            name = obj["__dataclass__"]
            if name not in _TYPES_BY_NAME:
                raise UnknownRecordKind(
                    f"{name!r} is not on the codec's dataclass whitelist; "
                    f"refusing to reconstruct it")
            cls = _TYPES_BY_NAME[name]
            fields = {k: from_portable(v) for k, v in obj["fields"].items()}
            return cls(**fields)
        if "__enum__" in obj:
            name = obj["__enum__"]
            if name not in _ENUMS_BY_NAME:
                raise UnknownRecordKind(
                    f"{name!r} is not on the codec's enum whitelist")
            return _ENUMS_BY_NAME[name](obj["value"])
        if "__tuple__" in obj:
            return tuple(from_portable(x) for x in obj["__tuple__"])
        return {k: from_portable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_portable(x) for x in obj]
    return obj


def encode_record(kind: str, obj) -> dict:
    """Wrap one object as a versioned, labelled envelope for `session.work`."""
    return {"schema_version": SCHEMA_VERSION, "kind": kind,
            "payload": to_portable(obj)}


def decode_record(record: dict, *, expected_kind: str | None = None):
    """Unwrap an envelope `encode_record` produced.

    Refuses a record whose `kind` does not match, rather than silently
    decoding it as if it were the expected shape -- a `kind` mismatch means
    something else was stored under this key, and guessing would produce an
    object that LOOKS right and answers every question wrong.
    """
    if ("schema_version" not in record or "kind" not in record
            or "payload" not in record):
        raise UnknownRecordKind(
            f"{record!r} is not an envelope this codec produced; expected "
            f"schema_version/kind/payload")
    if record["schema_version"] != SCHEMA_VERSION:
        raise UnknownRecordKind("unsupported schema version")
    if expected_kind is not None and record["kind"] != expected_kind:
        raise UnknownRecordKind(
            f"expected a {expected_kind!r} record, found {record['kind']!r}")
    return from_portable(record["payload"])


__all__ = [
    "SCHEMA_VERSION",
    "UnknownRecordKind",
    "_IntentRef",
    "decode_record",
    "encode_record",
    "from_portable",
    "to_portable",
]
