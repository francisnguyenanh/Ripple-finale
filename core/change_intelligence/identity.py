# -*- coding: utf-8 -*-
"""Cross-artifact identity resolution.

The task the phase turns on. Four representations of one rule:

    "approval threshold"              SOP
    "manager approval limit"          manager guide
    "approval matrix threshold"       spreadsheet header
    condition.amount >= 1000000       workflow config
    evaluate_approval(request, config) software

If the system cannot see that these are ONE rule, there is no conflict to
find, and WOW #1 does not exist. If it groups two DIFFERENT rules together,
every impact computed afterwards is wrong -- which is why precision is
treated as more important than recall here, against the usual instinct.

Three tiers, in descending order of trust. LLM-only matching is forbidden
here, and the reason is visible in the tier structure: the model is
allowed to PROPOSE and never to DECIDE.

    tier 1  DETERMINISTIC      an explicit rule id in the artifact
    tier 2  STRUCTURAL         same field, same unit, same operator
    tier 3  BOUNDED SEMANTIC   a model proposes; a script accepts or refuses

A candidate that reaches none of the three becomes UNKNOWN with a Gap, not
a guess. Nothing in this module calls a model directly -- a proposer is
handed in, and with none the tier degrades to abstention rather than to
silence (I11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from core.change_intelligence.alignment import Gap

_RULE_ID = re.compile(r"\bBR-\d{3}\b")

#: Tier 3 has to clear this before a script will accept its proposal. Set
#: high on purpose: the cost of a missed group is one rule reported as two,
#: which a person notices. The cost of a wrong group is an impact analysis
#: that is confidently, invisibly wrong.
SEMANTIC_ACCEPT_THRESHOLD = 0.85


@dataclass(frozen=True)
class Candidate:
    """One artifact's statement of something that might be a rule."""

    ref: str
    artifact: str
    family: str
    locator: str
    value: object = None
    unit: str = ""
    field_name: str = ""
    op: str = ""
    explicit_rule_ids: tuple[str, ...] = ()
    semantic_hint: str = ""
    text: str = ""
    observed_at: str | None = None
    #: A guard that narrows WHEN this claim applies. Empty means the claim
    #: is unconditional, which is a position, not an absence: an artifact
    #: that states a threshold and no scope claims the rule applies to
    #: everything.
    scope: str = ""
    #: This artifact READS the value from another rather than stating one.
    #: Distinct from silence: a site that defers cannot disagree about the
    #: value, so counting it as a missing value would report an
    #: existence_mismatch every time code is correctly parameterised.
    defers_to_config: bool = False
    #: The field the scope guard tests. Kept apart from `scope` so the
    #: check "is this restriction already the rule's own condition" is a
    #: comparison, not a string search.
    scope_field: str = ""


@dataclass(frozen=True)
class Group:
    """Candidates the system believes state one rule, and why it believes it."""

    semantic_id: str
    members: tuple[Candidate, ...]
    tier: str
    basis: str

    @property
    def families(self) -> set[str]:
        return {c.family for c in self.members}

    @property
    def values(self) -> set:
        return {c.value for c in self.members if c.value is not None}


@dataclass
class Resolution:
    groups: list[Group] = field(default_factory=list)
    unresolved: list[Candidate] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def by_semantic_id(self, semantic_id: str) -> Group | None:
        for group in self.groups:
            if group.semantic_id == semantic_id:
                return group
        return None


class SemanticProposer(Protocol):
    """Tier 3's boundary. A model lives behind this and nowhere else."""

    def propose(self, candidate: Candidate, options: list[str]) -> tuple[str, float, str]:
        """Return (semantic_id, confidence, reason). Confidence may be 0."""
        ...


# --- tier 1 -----------------------------------------------------------------


def _tier1(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    """Group by an explicit rule id written in the artifact.

    The most trustworthy signal there is, and the reason real organisations
    number their policies. Where the seed carries BR-001 in a config field
    or a comment, no inference is needed at all.
    """
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        ids = list(candidate.explicit_rule_ids) or _RULE_ID.findall(candidate.text or "")
        if len(set(ids)) == 1:
            grouped.setdefault(ids[0], []).append(candidate)
    return grouped


# --- tier 2 -----------------------------------------------------------------


def _structural_key(candidate: Candidate) -> tuple | None:
    """Same field, same unit, same operator -> the same decision.

    Requires a comparison to exist: a candidate with no operator is a
    statement, not a rule boundary, and joining statements by field name
    alone would put every mention of `amount` in one group.
    """
    if not candidate.field_name or not candidate.op:
        return None
    return (candidate.field_name, candidate.unit or "", candidate.op)


def _tier2(candidates: list[Candidate]) -> dict[tuple, list[Candidate]]:
    grouped: dict[tuple, list[Candidate]] = {}
    for candidate in candidates:
        key = _structural_key(candidate)
        if key is not None:
            grouped.setdefault(key, []).append(candidate)
    return grouped


# --- resolution --------------------------------------------------------------


def resolve(
    candidates: list[Candidate],
    *,
    known_semantic_ids: list[str] | None = None,
    rule_id_to_semantic_id: dict[str, str] | None = None,
    proposer: SemanticProposer | None = None,
    accept_threshold: float = SEMANTIC_ACCEPT_THRESHOLD,
) -> Resolution:
    """Group candidates into rules, by the strongest evidence available.

    Every candidate is settled by exactly one tier, and the tier is recorded
    on the group. A reader of the output can therefore ask "how do you know
    these are the same rule?" and get a different answer for each group --
    which is the difference between a grouping and a claim.
    """
    known = list(known_semantic_ids or [])
    id_map = dict(rule_id_to_semantic_id or {})
    resolution = Resolution()
    settled: set[str] = set()

    # --- tier 1
    for rule_id, members in sorted(_tier1(candidates).items()):
        semantic_id = id_map.get(rule_id, rule_id)
        resolution.groups.append(Group(
            semantic_id=semantic_id, members=tuple(members), tier="deterministic",
            basis=f"every member names {rule_id} explicitly",
        ))
        settled.update(c.ref for c in members)

    # --- tier 2
    remaining = [c for c in candidates if c.ref not in settled]
    for key, members in sorted(_tier2(remaining).items(), key=lambda kv: str(kv[0])):
        if len(members) < 2:
            continue
        # A hint, where the members agree on one, names the group. Where they
        # do not agree, the structural key itself is the name -- inventing a
        # semantic name the evidence does not support would be the kind of
        # confident guess this module exists to avoid.
        hints = {c.semantic_hint for c in members if c.semantic_hint}
        semantic_id = hints.pop() if len(hints) == 1 else f"{key[0]}{key[2]}[{key[1] or 'unitless'}]"
        existing = resolution.by_semantic_id(semantic_id)
        if existing is not None:
            resolution.groups.remove(existing)
            members = list(existing.members) + members
            tier, basis = existing.tier, existing.basis + "; plus structural match"
        else:
            tier = "structural"
            basis = f"same field {key[0]!r}, same unit {key[1] or 'none'!r}, same operator {key[2]!r}"
        resolution.groups.append(Group(
            semantic_id=semantic_id, members=tuple(members), tier=tier, basis=basis))
        settled.update(c.ref for c in members)

    # --- tier 2b: a declared hint that names a group we already have
    #
    # Still tier 2, not tier 3. The hint was produced by a keyword rule in
    # the document adapter -- deterministic, inspectable, and reproducible.
    # Attaching it to a group whose name it matches exactly adds no
    # judgement; it only declines to throw away a signal we already computed.
    #
    # Prose carries no field or operator, so the ordinary structural key
    # cannot see it. Without this, every policy document in the seed goes to
    # a Gap and the hero rule shows two representations instead of nine --
    # which would understate the product's own evidence.
    known_names = {g.semantic_id for g in resolution.groups} | set(known)
    for candidate in [c for c in candidates if c.ref not in settled]:
        if candidate.semantic_hint and candidate.semantic_hint in known_names:
            group = resolution.by_semantic_id(candidate.semantic_hint)
            if group is not None:
                resolution.groups.remove(group)
                resolution.groups.append(Group(
                    semantic_id=group.semantic_id,
                    members=group.members + (candidate,),
                    tier=group.tier,
                    basis=f"{group.basis}; plus a declared hint from "
                          f"{candidate.artifact}",
                ))
            else:
                resolution.groups.append(Group(
                    semantic_id=candidate.semantic_hint, members=(candidate,),
                    tier="structural",
                    basis=f"declared hint {candidate.semantic_hint!r} from "
                          f"{candidate.artifact}"))
            settled.add(candidate.ref)

    # --- tier 3
    options = sorted({g.semantic_id for g in resolution.groups} | set(known))
    for candidate in [c for c in candidates if c.ref not in settled]:
        semantic_id, confidence, reason = _propose(candidate, options, proposer)

        if semantic_id and confidence >= accept_threshold and semantic_id in options:
            group = resolution.by_semantic_id(semantic_id)
            if group is not None:
                resolution.groups.remove(group)
                resolution.groups.append(Group(
                    semantic_id=semantic_id,
                    members=group.members + (candidate,),
                    tier=group.tier,
                    basis=f"{group.basis}; plus one bounded-semantic match "
                          f"(confidence {confidence:.2f}: {reason})",
                ))
            else:
                resolution.groups.append(Group(
                    semantic_id=semantic_id, members=(candidate,),
                    tier="bounded_semantic",
                    basis=f"model proposal accepted at {confidence:.2f}: {reason}",
                ))
            settled.add(candidate.ref)
            continue

        # Not settled. This is a real answer, not a failure to produce one.
        resolution.unresolved.append(candidate)
        resolution.gaps.append(Gap(
            gap_id=f"GAP-IDENTITY-{candidate.ref}",
            reason=(
                f"{candidate.artifact} at {candidate.locator} states a value that could "
                f"not be attached to a known rule: no explicit rule id, no structural "
                f"match, and "
                + (f"the semantic proposal scored {confidence:.2f}, below "
                   f"{accept_threshold:.2f} ({reason})" if semantic_id
                   #: The proposer's own words, not a generic line. "no
                   #: proposer configured" and "the provider raised" are
                   #: very different operational facts (R10), and a Gap
                   #: that flattens them tells an operator nothing about
                   #: whether the system is degraded or merely unsure.
                   else f"tier 3 did not place it: {reason}")
            ),
        ))

    resolution.groups.sort(key=lambda g: g.semantic_id)
    return resolution


def _propose(candidate: Candidate, options: list[str],
             proposer: SemanticProposer | None) -> tuple[str, float, str]:
    """Ask tier 3, and treat its absence as abstention rather than as zero.

    With no proposer configured, the candidate goes to a Gap. It does NOT
    fall through to a looser rule: a system that silently degrades to
    guessing when the model is unavailable is worse than one that says so.
    """
    if proposer is None:
        return ("", 0.0, "no semantic proposer configured")
    try:
        semantic_id, confidence, reason = proposer.propose(candidate, options)
    except Exception as exc:  # noqa: BLE001 -- a provider failure is an abstention
        return ("", 0.0, f"semantic proposer failed: {type(exc).__name__}")
    return (semantic_id or "", float(confidence or 0.0), reason or "")


# --- measurement --------------------------------------------------------------


@dataclass(frozen=True)
class IdentityScore:
    """Precision and recall against the seed manifest's `semantic_id`s.

    Both are reported. A single number would let a system with perfect
    recall and terrible precision -- one that puts everything in one group --
    look successful.
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    wrongly_grouped: tuple[tuple[str, str], ...] = ()

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 1.0


def score(resolution: Resolution, truth: dict[str, set[str]]) -> IdentityScore:
    """Score grouping against ground truth.

    The unit is a PAIR of candidates that ground truth says belong together,
    which is the only unit that penalises both splitting one rule into two
    and merging two rules into one. Counting groups would reward a system
    that produced the right number of groups with the wrong members in them.
    """
    def pairs(members: set[str]) -> set[frozenset[str]]:
        ordered = sorted(members)
        return {frozenset((a, b)) for i, a in enumerate(ordered) for b in ordered[i + 1:]}

    expected: set[frozenset[str]] = set()
    for refs in truth.values():
        expected |= pairs(set(refs))

    # Only candidates ground truth has an opinion about are scored. A
    # candidate the answer key never mentions cannot be judged right or
    # wrong, and counting its pairs as errors measures the COMPLETENESS of
    # the manifest while reporting it as the PRECISION of the resolver.
    #
    # That is not a hypothetical: it scored this resolver at 0.79 when every
    # grouping it made was correct and two real representations were simply
    # missing from the key. A metric that moves when the answer key has a
    # hole is a metric that will be argued about.
    scored = {ref for refs in truth.values() for ref in refs}

    actual: set[frozenset[str]] = set()
    for group in resolution.groups:
        actual |= pairs({c.ref for c in group.members if c.ref in scored})

    ref_to_truth = {ref: sid for sid, refs in truth.items() for ref in refs}
    wrong = tuple(sorted(
        tuple(sorted(pair)) for pair in (actual - expected)
        if len({ref_to_truth.get(r) for r in pair if r in ref_to_truth}) > 1
    ))

    return IdentityScore(
        true_positives=len(actual & expected),
        false_positives=len(actual - expected),
        false_negatives=len(expected - actual),
        wrongly_grouped=wrong,
    )
