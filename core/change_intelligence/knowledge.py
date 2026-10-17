# -*- coding: utf-8 -*-
"""The Knowledge Model. A model proposes; a script promotes.

The path, and no step is skippable:

    model proposes a candidate      (through the provider boundary, I11)
      -> evidence validated          does the quote really exist? (I4)
      -> schema validated            is it the declared shape?
      -> reconciled                  SCRIPT compares claims, never a model
             agree      -> rule record
             disagree   -> conflict record, BOTH sides kept, pending_human
             one side   -> Gap
      -> promoted

Reconciliation is deterministic on purpose. A model that both produces the
claims AND decides which of them wins has no check on it; the disagreement
between claims is the most valuable signal in the system and must not be
resolved by the thing that generated them (I15).

Nothing here imports a provider. A proposer is handed in, and with none the
layer produces exactly what deterministic extraction supports -- which is a
smaller Knowledge Model, honestly arrived at, rather than a silent fallback
to guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.alignment import (
    AlignmentOutcome,
    comparable,
    Gap,
    Representation,
    reconcile,
)
from core.change_intelligence.evidence import EvidenceRef, verify
from core.change_intelligence.states import AlignmentState, KnowledgeState

KNOWLEDGE_TYPES = ("rule", "constraint", "process", "gap", "convention")


class PromotionRefused(Exception):
    """A candidate failed a gate. Carries which one, so a failure is actionable."""


@dataclass(frozen=True)
class Candidate:
    """What a proposer suggests. Not yet knowledge."""

    knowledge_type: str
    semantic_id: str
    statement: str
    evidence: tuple[EvidenceRef, ...]
    derived_from: tuple[str, ...]
    confidence: float = 0.0
    proposer: str = ""


@dataclass(frozen=True)
class KnowledgeRecord:
    """Promoted knowledge. Every field that makes it checkable is required."""

    id: str
    knowledge_type: str
    semantic_id: str
    statement: str
    evidence: tuple[EvidenceRef, ...]
    derived_from: tuple[str, ...]
    state: KnowledgeState
    alignment: AlignmentState | None = None
    representations: tuple[Representation, ...] = ()
    gap_id: str = ""
    reason: str = ""
    km_version: int = 1


@dataclass
class KnowledgeModel:
    records: list[KnowledgeRecord] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)

    def by_semantic_id(self, semantic_id: str) -> list[KnowledgeRecord]:
        return [r for r in self.records if r.semantic_id == semantic_id]

    def conflicts(self) -> list[KnowledgeRecord]:
        return [r for r in self.records if r.alignment is AlignmentState.CONFLICT]


# --- the gates ---------------------------------------------------------------


def validate_schema(candidate: Candidate) -> str:
    """Shape, before anything else looks at meaning."""
    if candidate.knowledge_type not in KNOWLEDGE_TYPES:
        return f"knowledge_type {candidate.knowledge_type!r} is not one of {KNOWLEDGE_TYPES}"
    if not candidate.semantic_id:
        return "no semantic_id: the record could never be matched to a rule"
    if not candidate.statement.strip():
        return "no statement"
    if not candidate.evidence:
        return "no evidence (I2)"
    if not candidate.derived_from:
        return "no lineage (I2)"
    if not 0.0 <= candidate.confidence <= 1.0:
        return f"confidence {candidate.confidence} is outside [0, 1]"
    return ""


def validate_evidence(candidate: Candidate, snapshot) -> str:
    """Every cited quote must actually be there (I4).

    Checked before promotion rather than after, because a record in the
    Knowledge Model is something other components are entitled to trust.
    """
    for ref in candidate.evidence:
        failure = verify(ref, snapshot)
        if failure is not None:
            return f"evidence does not verify: {failure.why}"
    return ""


def promote(candidate: Candidate, snapshot, *, km_version: int = 1) -> KnowledgeRecord:
    """Run the gates in order and build the record, or refuse.

    Schema first, then evidence: a malformed candidate has no reliable
    evidence field to check, and reporting "evidence invalid" for something
    that was never the right shape sends the reader in the wrong direction.
    """
    for check, gate in ((validate_schema(candidate), "schema"),
                        (validate_evidence(candidate, snapshot), "evidence")):
        if check:
            raise PromotionRefused(f"{gate} gate: {check}")

    if candidate.knowledge_type == "gap":
        raise PromotionRefused(
            "a gap is not promoted as knowledge; record it with `open_gap` so it "
            "carries a Gap ID and a reason (I5)"
        )

    return KnowledgeRecord(
        id=f"KM-{candidate.semantic_id}",
        knowledge_type=candidate.knowledge_type,
        semantic_id=candidate.semantic_id,
        statement=candidate.statement,
        evidence=tuple(candidate.evidence),
        derived_from=tuple(candidate.derived_from),
        state=KnowledgeState.PROPOSED,
        km_version=km_version,
    )


# --- reconciliation ------------------------------------------------------------


def build_from_groups(resolution, snapshot, *, history=None) -> KnowledgeModel:
    """Turn resolved identity groups into knowledge, deterministically.

    This is the half of the Knowledge Model that needs no model at all: the
    groups already say which statements are about one rule, so what remains
    is comparing their values -- arithmetic, not judgement.

    `history` supplies recency, and its absence is meaningful rather than
    neutral: with no way to order two differing representations, the answer
    is CONFLICT, not a guess at DRIFT.
    """
    model = KnowledgeModel()

    for group in resolution.groups:
        representations = tuple(
            Representation(
                artifact=member.artifact,
                semantic_id=group.semantic_id,
                value=member.value,
                locator=member.locator,
                observed_at=_observed_at(member, history, group.semantic_id),
                snapshot_id=snapshot.snapshot_id,
                scope=member.scope,
                defers_to_config=member.defers_to_config,
                scope_field=member.scope_field,
                field_name=member.field_name,
            )
            for member in group.members
        )

        # Reconcile the representations that STATE a value. A prose
        # document covering the rule and a code site reading it from config
        # hold no position on the number, and counting their silence as a
        # differing value reported an ALIGNED rule as a CONFLICT.
        outcome: AlignmentOutcome = reconcile(comparable(list(representations)))

        evidence = tuple(
            EvidenceRef(artifact=m.artifact, locator=m.locator,
                        quote=m.text or str(m.value), snapshot_id=snapshot.snapshot_id)
            for m in group.members
        )

        model.records.append(KnowledgeRecord(
            id=f"KM-{group.semantic_id}",
            knowledge_type="rule",
            semantic_id=group.semantic_id,
            statement=_statement_for(group, outcome),
            evidence=evidence,
            derived_from=tuple(m.ref for m in group.members),
            state=KnowledgeState.VERIFIED,
            alignment=outcome.alignment,
            representations=representations,
            reason=outcome.reason,
        ))

    for gap in resolution.gaps:
        model.gaps.append(gap)

    model.records.sort(key=lambda r: r.semantic_id)
    return model


def _observed_at(member, history, semantic_id: str) -> str | None:
    """When this artifact last stated the rule, if anything can say.

    Prefers the artifact's own marker (a spreadsheet's `last_reviewed`, a
    document's review date) and falls back to change history. Returning None
    is a real answer: it is what turns a DRIFT into a CONFLICT.
    """
    if member.observed_at:
        return member.observed_at
    if history is None:
        return None
    for record in history:
        payload = getattr(record, "payload", {})
        if payload.get("type") != "change_event":
            continue
        if semantic_id in payload.get("semantic_ids", []) and \
                member.artifact in payload.get("artifacts", []):
            return payload.get("date")
    return None


def _statement_for(group, outcome: AlignmentOutcome) -> str:
    values = sorted(str(v) for v in group.values)
    if outcome.alignment is AlignmentState.ALIGNED and values:
        return f"{group.semantic_id} = {values[0]}, stated by {len(group.members)} artifacts"
    if outcome.alignment is AlignmentState.CONFLICT:
        return (f"{group.semantic_id} is stated as {' and '.join(values)} by different "
                f"artifacts, and no evidence orders them")
    if outcome.alignment is AlignmentState.DRIFT:
        return (f"{group.semantic_id} is stated as {' and '.join(values)}; "
                f"{outcome.reason}")
    return f"{group.semantic_id} could not be determined"


def open_gap(gap_id: str, reason: str) -> Gap:
    """I5: an UNKNOWN never travels without a Gap ID and a reason."""
    if not gap_id or not reason:
        raise PromotionRefused("a gap needs both an id and a reason (I5)")
    return Gap(gap_id=gap_id, reason=reason)


# --- the proposer boundary -------------------------------------------------------


def propose_and_promote(proposer, requests: list, snapshot, *,
                        km_version: int = 1) -> KnowledgeModel:
    """Run a model's proposals through every gate.

    Malformed output is REJECTED and recorded, never repaired. Repairing it
    would hide how often the model produces nonsense, and that rate is
    exactly what the negative evals at P9 measure.
    """
    model = KnowledgeModel()
    if proposer is None:
        return model

    for request in requests:
        try:
            candidate = proposer.propose(request)
        except Exception as exc:  # noqa: BLE001 -- a provider failure is not knowledge
            model.rejected.append((None, f"proposer failed: {type(exc).__name__}: {exc}"))
            continue

        if candidate is None:
            model.rejected.append((None, "proposer abstained"))
            continue

        try:
            model.records.append(promote(candidate, snapshot, km_version=km_version))
        except PromotionRefused as refusal:
            model.rejected.append((candidate, str(refusal)))

    model.records.sort(key=lambda r: r.semantic_id)
    return model
