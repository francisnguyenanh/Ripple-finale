# -*- coding: utf-8 -*-
"""Discovery: what the enterprise does not know about itself.

Three detectors over one Knowledge Model, and a conflict that cannot be
closed by anything here.

    undocumented    behaviour encoded in one artifact family with no
                    representation in any knowledge family
    unimplemented   its mirror: a document states a rule nothing implements
    misalignment    families that disagree -- DRIFT when evidence orders
                    them, CONFLICT when nothing does

Every decision in this module is a SCRIPT decision. A model may have helped
group the claims (identity tier 3) and may later explain them (see explain), but the
comparison of two values and the verdict that follows are arithmetic. This is
the architectural difference from RAG-over-documents: the disagreement between
sources is the product, so the thing that generated the claims must not be the
thing that settles them (I15).

The asymmetry is deliberate and worth stating: UNDOCUMENTED and UNIMPLEMENTED
are the same shape seen from two sides, and a detector that only looks one way
finds UN-001 while missing UN-004. A rule nobody wrote down and a rule nobody
built are both gaps between intent and operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.alignment import (
    Gap,
    Representation,
    comparable,
    reconcile,
)
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.states import AlignmentState

#: Families that encode BEHAVIOUR -- what the system actually does.
BEHAVIOUR_FAMILIES = frozenset({"software", "workflow", "spreadsheet"})

#: Families that record KNOWLEDGE -- what people believe the system does.
KNOWLEDGE_FAMILIES = frozenset({"docs"})

#: `tests` and `history` are neither. A test is evidence about intent rather
#: than a statement of policy, and history orders claims without making one.
#: Counting either as knowledge would let a green test "document" a rule no
#: person ever wrote down, which is exactly the finding UN-003 must surface.
CORROBORATING_FAMILIES = frozenset({"tests", "history"})

CONFLICT_TYPES = ("value_mismatch", "scope_mismatch", "existence_mismatch")

PENDING_HUMAN = "PENDING_HUMAN"


@dataclass(frozen=True)
class Claim:
    """One artifact's position, with everything needed to check it."""

    artifact: str
    family: str
    locator: str
    value: object
    evidence: EvidenceRef
    last_changed: str | None = None


@dataclass(frozen=True)
class Finding:
    """Something a person needs to look at, and the evidence for it.

    There is no `resolved_value` and no `winner`, by construction rather than
    by convention: `resolution_status` starts at PENDING_HUMAN and the only
    function that can move it requires a decision record (I15).
    """

    finding_id: str
    kind: str
    semantic_id: str
    claims: tuple[Claim, ...]
    why: str
    alignment: AlignmentState | None = None
    conflict_type: str = ""
    resolution_status: str = PENDING_HUMAN
    impact: str = ""
    gaps: tuple[Gap, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "CONFLICT":
            if len(self.claims) < 2:
                raise ValueError(
                    "a CONFLICT with one claim is a claim, not a conflict")
            if self.conflict_type not in CONFLICT_TYPES:
                raise ValueError(
                    f"conflict_type {self.conflict_type!r} not in {CONFLICT_TYPES}: "
                    "the three kinds need three different repairs at P7, so an "
                    "unclassified conflict cannot be acted on")
        if not self.claims and not self.gaps:
            raise ValueError(
                f"finding {self.finding_id} cites nothing (I2/I4)")

    @property
    def families(self) -> set[str]:
        return {c.family for c in self.claims}

    @property
    def values(self) -> set:
        return {c.value for c in self.claims if c.value is not None}


@dataclass
class DiscoveryReport:
    findings: list[Finding] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def by_semantic_id(self, semantic_id: str) -> list[Finding]:
        return [f for f in self.findings if f.semantic_id == semantic_id]

    @property
    def conflicts(self) -> list[Finding]:
        return self.of_kind("CONFLICT")


# --- undocumented and unimplemented ----------------------------------


def detect_undocumented(model, *, history=None) -> list[Finding]:
    """Behaviour with no knowledge representation, and its mirror.

    `history` enriches a finding into a story -- which change introduced the
    behaviour, what the message said, whether anything reverted it. Enrichment
    never changes the verdict: a rule is undocumented because no document
    states it, not because history is silent about it.
    """
    findings: list[Finding] = []

    for record in sorted(model.records, key=lambda r: r.semantic_id):
        claims = _claims_of(record)
        if not claims:
            continue
        families = {c.family for c in claims}
        behaviour = families & BEHAVIOUR_FAMILIES
        knowledge = families & KNOWLEDGE_FAMILIES

        # A restriction that exists only in code is undocumented even when
        # the rule it restricts is documented in full. BR-004 is written
        # down everywhere -- "capital expenditure of 5,000,000 or more goes
        # to Finance" appears in two documents -- and not one of them says
        # the code applies it only to requests already categorised as capex.
        # The rule is documented; its scope is not.
        #
        # A guard on the field the rule ALREADY tests is that condition
        # restated in code, not an extra restriction: BR-005 is
        # `supplier_risk == "high"` in the workflow and in the policy prose
        # alike, so code testing the same field documents nothing new.
        # BR-004 is `amount >= 5,000,000` everywhere and `category ==
        # "capex"` only in code -- a different field, and the one nobody
        # wrote down.
        stated_fields = {r.field_name for r in record.representations if r.field_name}
        scopes = {r.scope for r in record.representations
                  if r.scope and r.scope_field not in stated_fields}
        if scopes and knowledge:
            findings.append(Finding(
                finding_id=f"UN-{record.semantic_id}-scope",
                kind="UNDOCUMENTED",
                semantic_id=record.semantic_id,
                claims=claims,
                why=(f"{record.semantic_id} applies only when "
                     f"{'; '.join(sorted(scopes))} in code, and no document "
                     f"states that restriction"),
                impact=_origin_story(record.semantic_id, claims, history),
            ))

        if behaviour and not knowledge:
            kind, why = "UNDOCUMENTED", (
                f"{record.semantic_id} is encoded in "
                f"{', '.join(sorted(behaviour))} but no document in the "
                f"knowledge families {sorted(KNOWLEDGE_FAMILIES)} states it")
        elif knowledge and not behaviour:
            kind, why = "UNIMPLEMENTED", (
                f"{record.semantic_id} is stated in "
                f"{', '.join(sorted(knowledge))} but nothing in "
                f"{sorted(BEHAVIOUR_FAMILIES)} implements it")
        else:
            continue

        findings.append(Finding(
            finding_id=f"UN-{record.semantic_id}",
            kind=kind,
            semantic_id=record.semantic_id,
            claims=claims,
            why=why,
            impact=_origin_story(record.semantic_id, claims, history),
        ))

    return findings


def _origin_story(semantic_id: str, claims: tuple[Claim, ...], history) -> str:
    """Turn a finding into something a person can act on.

    A finding that says "this is undocumented" invites the reply "so what?".
    A finding that says "introduced in 2020 as a temporary measure, never
    reverted, five years in operation" does not.
    """
    if history is None:
        return ""
    artifacts = {c.artifact for c in claims}
    for entry in history:
        payload = getattr(entry, "payload", entry if isinstance(entry, dict) else {})
        if payload.get("type") != "change_event":
            continue
        if semantic_id not in payload.get("semantic_ids", []):
            continue
        if artifacts.isdisjoint(payload.get("artifacts", [])):
            continue
        parts = [f"introduced {payload.get('date', 'unknown date')}"]
        if payload.get("message"):
            parts.append(f'"{payload["message"]}"')
        if payload.get("temporary") and not payload.get("reverted"):
            parts.append("declared temporary; no reverting change found")
        return "; ".join(parts)
    return ""


#: A rule that exists in a document and nowhere else, before anybody has
#: named it. Not a placeholder to be filled in automatically -- the absence
#: of a name IS the finding's state, and it travels with a Gap (I5).
UNNAMED = ""


def detect_unimplemented_documented(records, *, snapshot_id: str = "") -> list[Finding]:
    """Normative sentences that attach to no rule at all.

    The mirror of `detect_undocumented`, and the half that was missing.
    That function finds behaviour no document states; this one finds a
    statement nothing implements. UN-004 -- "a request that has waited more
    than two working days escalates" -- is invisible to the first and
    obvious to the second, because a rule with no code has no candidate, no
    group and no Knowledge Model record to detect anything about.

    Each finding carries `semantic_id = UNNAMED` and a Gap saying so. The
    tempting shortcut is to derive an id from the sentence and group the
    matches; two documents here do state the same escalation rule, and
    grouping them on shared words would be identity resolution by
    coincidence. A person names it, and until they do the product says it
    does not know.
    """
    findings: list[Finding] = []

    for record in sorted(records or (),
                         key=lambda r: getattr(r, "id", "")):
        payload = getattr(record, "payload", {}) or {}
        if payload.get("type") != "unattached_rule":
            continue
        provenance = record.provenance[0] if record.provenance else None
        if provenance is None:
            continue

        artifact = payload.get("document", "")
        locator = f"{getattr(provenance, 'sheet', 'body')}!{getattr(provenance, 'row', '')}"
        evidence = EvidenceRef(artifact=artifact, locator=locator,
                               quote=getattr(provenance, "quote", "") or "",
                               snapshot_id=snapshot_id)
        claim = Claim(artifact=artifact, family="docs", locator=locator,
                      value=None, evidence=evidence,
                      last_changed=payload.get("last_reviewed") or None)

        findings.append(Finding(
            finding_id=f"UNIMPL-{artifact}:{locator}",
            kind="UNIMPLEMENTED",
            semantic_id=UNNAMED,
            claims=(claim,),
            why=(f"{artifact} states a rule that nothing in "
                 f"{sorted(BEHAVIOUR_FAMILIES)} implements, and it matches no "
                 f"rule this snapshot knows about"),
            gaps=(Gap(gap_id=f"GAP-{artifact}:{locator}",
                      reason=("this statement has no semantic_id: nobody has "
                              "named the rule, so it cannot be grouped with "
                              "any other statement of it")),),
        ))

    return findings


# --- cross-artifact alignment ----------------------------------------


def detect_misalignment(model) -> list[Finding]:
    """Disagreement between families, classified but never settled.

    DRIFT and CONFLICT are not degrees of the same thing. DRIFT means the
    evidence orders the claims, so there is a later one and an earlier one.
    CONFLICT means nothing orders them -- and the hero case is CONFLICT
    precisely because one dated side does not order two sides. Naming this
    module after DRIFT would have locked it to the answer it must derive.
    """
    findings: list[Finding] = []

    for record in sorted(model.records, key=lambda r: r.semantic_id):
        reps = _comparable(record.representations)
        if len(reps) < 2:
            continue
        outcome = reconcile(reps)
        if outcome.alignment is AlignmentState.ALIGNED:
            # Agreeing on every number is not agreeing. If one artifact
            # applies the rule only to capex and another states no scope at
            # all, they disagree about who the rule is for -- and the axis
            # that disagreement lives on is not the one `reconcile` compares.
            # All representations, not the comparable ones: the artifact
            # that carries the scope is usually the one that defers on the
            # value, so the value filter removes exactly the evidence this
            # check needs.
            scoped = detect_scope_mismatch(record, list(record.representations))
            if scoped is not None:
                findings.append(scoped)
            continue

        #: The same filter the comparison used. Classifying a conflict from
        #: claims the comparison never saw would label the hero case an
        #: existence_mismatch -- and send P7 to add a missing rule when what
        #: is actually wrong is two rules holding different numbers.
        #: Filtered per CLAIM, not per artifact: one document can both state
        #: the threshold on one line and cover the rule qualitatively on
        #: another, and keying on the filename lets the qualitative one back
        #: in -- which reads as a silent side and turns the hero case into
        #: an existence_mismatch.
        claims = tuple(c for c in _claims_of(record)
                       if c.value is not None
                       or c.family not in KNOWLEDGE_FAMILIES)
        if outcome.alignment is AlignmentState.UNKNOWN:
            findings.append(Finding(
                finding_id=f"UNK-{record.semantic_id}",
                kind="UNKNOWN",
                semantic_id=record.semantic_id,
                claims=claims,
                why=outcome.reason,
                alignment=outcome.alignment,
                gaps=tuple(outcome.gaps),
            ))
            continue

        kind = ("CONFLICT" if outcome.alignment is AlignmentState.CONFLICT
                else "DRIFT")
        findings.append(Finding(
            finding_id=f"{'CONF' if kind == 'CONFLICT' else 'DRIFT'}-"
                       f"{record.semantic_id}",
            kind=kind,
            semantic_id=record.semantic_id,
            claims=claims,
            why=outcome.reason,
            alignment=outcome.alignment,
            conflict_type=(classify_conflict(claims) if kind == "CONFLICT" else ""),
            impact=operational_impact(record.semantic_id, claims),
        ))

    return findings


def detect_scope_mismatch(record, reps: list[Representation]) -> Finding | None:
    """Same value everywhere, different answer to "who is this for".

    `approval_service` runs BR-004 only when the request is capex; the
    workflow states 5,000,000 with no category and the documents state no
    category either. Every number matches, so value comparison calls the
    rule ALIGNED -- and a five-million non-capex request goes to a line
    manager in the code and to Finance in the workflow.

    Reported as a CONFLICT rather than a DRIFT on purpose: nothing here says
    which scope was intended. The code may be enforcing a restriction the
    policy forgot to write down, or applying one nobody agreed to, and the
    evidence is the same either way (I15).
    """
    scoped = {r.scope for r in reps if r.scope}
    if not scoped:
        return None
    unscoped = [r for r in reps if not r.scope]
    if not unscoped and len(scoped) < 2:
        return None

    claims = tuple(c for c in _claims_of(record)
                   if c.value is not None or c.family not in KNOWLEDGE_FAMILIES)
    narrowing = sorted(r.artifact for r in reps if r.scope)
    silent = sorted(r.artifact for r in unscoped)
    return Finding(
        finding_id=f"CONF-{record.semantic_id}-scope",
        kind="CONFLICT",
        semantic_id=record.semantic_id,
        claims=claims,
        why=(f"{', '.join(narrowing)} applies {record.semantic_id} only when "
             f"{'; '.join(sorted(scoped))}, while "
             f"{', '.join(silent) or 'the other artifacts'} state the rule "
             f"with no such restriction"),
        alignment=AlignmentState.CONFLICT,
        conflict_type="scope_mismatch",
        impact=(f"a request that satisfies {record.semantic_id} but not "
                f"{'; '.join(sorted(scoped))} is handled by "
                f"{', '.join(narrowing)} and by {', '.join(silent)} "
                f"differently, and the values agree so nothing flags it"),
    )


def _comparable(representations) -> list[Representation]:
    """Drop the claims that cover a rule without stating a value.

    A policy sentence saying high-risk suppliers go to the Operations
    Director states BR-005 completely and names no amount. It belongs in
    the coverage question -- is this rule documented at all -- and must stay
    out of the comparison question, because treating "says nothing numeric"
    as a value produces a disagreement between a document and itself.

    The same is NOT true of a behaviour family. A workflow exception that
    names a handler and states no ceiling has delegated the rule, and that
    silence is a real position: it is what makes CONF-003 an
    existence_mismatch rather than agreement.
    """
    return comparable(list(representations))


def classify_conflict(claims: tuple[Claim, ...]) -> str:
    """Three kinds of conflict, because P7 repairs them three different ways.

    An unclassified conflict is one a proposal engine cannot act on: changing
    a value, clarifying a scope and adding a missing rule are not variations
    of one edit.
    """
    stated = [c for c in claims if c.value is not None]
    silent = [c for c in claims if c.value is None]
    if silent and stated:
        return "existence_mismatch"
    values = {c.value for c in stated}
    if len(values) > 1:
        return "value_mismatch"
    return "scope_mismatch"


def operational_impact(semantic_id: str, claims: tuple[Claim, ...]) -> str:
    """The line that turns a technical finding into a business problem.

    Derived from the data -- the span between the stated values and the
    artifacts that disagree -- rather than written by a model. A sentence a
    model composed here could not be checked against anything, and this line
    is the one a judge reads first.
    """
    numeric = sorted(c.value for c in claims
                     if isinstance(c.value, (int, float))
                     and not isinstance(c.value, bool))
    if len(numeric) < 2 or numeric[0] == numeric[-1]:
        artifacts = sorted({c.artifact for c in claims})
        return (f"{semantic_id} is applied differently depending on which of "
                f"{len(artifacts)} sources is consulted")
    low, high = numeric[0], numeric[-1]
    return (f"requests between {low:,} and {high:,} are handled differently "
            f"depending on whether {semantic_id} is read from "
            f"{_artifact_of(claims, low)} or {_artifact_of(claims, high)}")


def _artifact_of(claims: tuple[Claim, ...], value) -> str:
    for claim in claims:
        if claim.value == value:
            return claim.artifact
    return "an unknown artifact"


# --- the conflict cannot be closed here ------------------------------


class UnresolvedWithoutHuman(Exception):
    """Raised by the only path that could ever close a conflict."""


@dataclass(frozen=True)
class HumanDecision:
    """I12: an answer is an audit record or it did not happen."""

    actor: str
    timestamp: str
    finding_id: str
    answer: str
    reason: str
    session_id: str

    def __post_init__(self) -> None:
        missing = [f for f in ("actor", "timestamp", "finding_id",
                               "answer", "session_id")
                   if not getattr(self, f)]
        if missing:
            raise UnresolvedWithoutHuman(
                f"a decision record without {missing} cannot be audited (I12)")


def resolve_with_human(finding: Finding, decision: HumanDecision) -> Finding:
    """The ONLY way a finding stops being PENDING_HUMAN.

    There is deliberately no `auto_resolve`, no `pick_newest` and no
    `prefer_source`. A newer spreadsheet is not a more correct spreadsheet --
    it may be the record of someone changing a threshold without approval,
    which is the scenario this product exists to catch. Choosing a winner
    from recency would silently convert that finding into a fact.
    """
    if decision.finding_id != finding.finding_id:
        raise UnresolvedWithoutHuman(
            f"decision names {decision.finding_id}, finding is "
            f"{finding.finding_id}")
    if decision.answer == "unclear":
        gap = Gap(gap_id=f"GAP-{finding.finding_id}",
                  reason=f"{decision.actor} could not say: {decision.reason}")
        return Finding(**{**finding.__dict__,
                          "gaps": finding.gaps + (gap,)})
    return Finding(**{**finding.__dict__,
                      "resolution_status": f"RESOLVED_BY:{decision.actor}"})


# --- the report ---------------------------------------------------------------


def discover(model, *, history=None) -> DiscoveryReport:
    report = DiscoveryReport()
    report.findings.extend(detect_undocumented(model, history=history))
    report.findings.extend(detect_unimplemented_documented(
        history, snapshot_id=getattr(model, "snapshot_id", "")))
    report.findings.extend(detect_misalignment(model))
    report.gaps.extend(model.gaps)
    report.findings.sort(key=lambda f: (f.kind, f.semantic_id, f.finding_id))
    return report


def _claims_of(record) -> tuple[Claim, ...]:
    by_artifact = {e.artifact: e for e in record.evidence}
    claims = []
    for rep in record.representations:
        evidence = by_artifact.get(rep.artifact)
        if evidence is None:
            continue
        claims.append(Claim(
            artifact=rep.artifact,
            family=_family_of(rep.artifact),
            locator=rep.locator,
            value=rep.value,
            evidence=evidence,
            last_changed=rep.observed_at,
        ))
    return tuple(claims)


def _family_of(artifact: str) -> str:
    """Family from path, matching `reference_system/reset/baseline.py`."""
    head = artifact.split("/", 1)[0]
    return {"application": "software", "workflow": "workflow",
            "docs": "docs", "data": "spreadsheet", "tests": "tests",
            "history": "history"}.get(head, "unknown")
