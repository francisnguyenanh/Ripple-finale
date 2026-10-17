# -*- coding: utf-8 -*-
"""Explaining a reconciliation, in three parts and in one order.

    1. WHAT DIFFERS            script. Values and locations, listed.
    2. WHY THESE MIGHT BE ONE RULE   model. Why identity resolution grouped
                                     four artifacts that share no wording.
    3. WHAT IS STILL UNCERTAIN       model + script. Gaps, unresolved
                                     candidates, and what the evidence
                                     cannot settle.

Part 3 is what separates a serious product from a demo. A system that
produces parts 1 and 2 and stops has not represented the limits of its own
knowledge, and a reader has no way to tell a confident answer from a correct
one.

Part 2 is asked ONE VALUE AT A TIME. Asking about the whole
finding at once was measured to fail: shown nine claims of which eight said
1,000,000, both Gemini tiers reported that the artifacts "all consistently
reference" it and dropped the 500,000 that makes the finding a finding. The
instruction could not outvote the evidence, so the evidence is split and the
join is arithmetic.

Part 1 is never written by a model, and part 2 never invents a fact. The
constraint is mechanical rather than a matter of prompting: an explanation
that names an artifact or a value absent from the evidence set is REJECTED,
not repaired. Repairing it would hide how often the model does this, and
that rate is exactly what the negative evals at P9 measure.

I19 lives here too. Artifact content reaches the model only inside a
labelled data block, never at instruction position, because this is the
second place -- after ingestion -- where text somebody else wrote is put in
front of a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.change_intelligence.evidence import as_data_block

#: Numbers that are structure rather than claim: a line number, a count of
#: artifacts, a section reference. Requiring the model to cite only values
#: present in the evidence must not make ordinary prose impossible.
_SMALL = 1000


class ExplanationRejected(Exception):
    """The explanation asserted something the evidence does not support."""


@dataclass(frozen=True)
class Explanation:
    what_differs: str
    why_one_rule: str
    still_uncertain: str
    grounded_in: tuple[str, ...] = ()
    proposer: str = ""

    def rendered(self) -> str:
        return (f"WHAT DIFFERS\n{self.what_differs}\n\n"
                f"WHY THESE MIGHT BE ONE RULE\n{self.why_one_rule}\n\n"
                f"WHAT IS STILL UNCERTAIN\n{self.still_uncertain}")


@dataclass
class ExplanationAttempt:
    """What came back, and why it was refused if it was.

    A refusal is recorded rather than swallowed: "the model produced an
    ungrounded explanation" is an operational fact, and a system that
    silently retries until something passes reports a quality it does not
    have.
    """

    explanation: Explanation | None = None
    rejected: list[str] = field(default_factory=list)


# --- part 1: deterministic ----------------------------------------------------


def what_differs(finding) -> str:
    """Values and locations. No judgement, so no model.

    Asking a model to write this part would put the one section a reader
    checks line by line at the mercy of paraphrase.
    """
    lines = [f"{finding.semantic_id} -- {finding.conflict_type or finding.kind}"]
    for claim in sorted(finding.claims, key=lambda c: (c.artifact, c.locator)):
        value = "states no value" if claim.value is None else f"states {claim.value}"
        when = f", last changed {claim.last_changed}" if claim.last_changed else ""
        lines.append(f"  {claim.artifact}:{claim.locator} ({claim.family}) "
                     f"{value}{when}")
    if finding.impact:
        lines.append(f"  impact: {finding.impact}")
    return "\n".join(lines)


def uncertainties(finding, *, resolution=None) -> list[str]:
    """What the evidence cannot settle, from the data alone.

    Derived before the model is asked, so that a model which says nothing
    still leaves this section populated. An empty part 3 must mean "nothing
    is uncertain", never "nobody produced text".
    """
    items = []
    for gap in finding.gaps:
        items.append(f"{gap.gap_id}: {gap.reason}")
    if finding.kind == "CONFLICT":
        items.append(
            "no evidence orders these representations, so which one reflects "
            "current intent is not derivable from the artifacts")
    undated = sorted(c.artifact for c in finding.claims if not c.last_changed)
    if undated:
        items.append(f"no recency marker on: {', '.join(undated)}")
    if resolution is not None:
        for candidate in getattr(resolution, "unresolved", []):
            items.append(
                f"{candidate.artifact}:{candidate.locator} states "
                f"{candidate.value} and could not be attached to any rule")
    return items


# --- the grounding check -------------------------------------------------------


def claims_vocabulary(claims) -> tuple[set[str], set[str]]:
    """The artifacts and values a set of claims permits an answer to name.

    Taken over CLAIMS rather than over a finding, because the per-value question asks about
    one value at a time and a sub-finding cannot be built: a CONFLICT
    carrying a single claim is rejected by `Finding` itself, and rightly --
    one claim is a claim, not a disagreement. The vocabulary is what the
    check actually needs, so the check takes that.
    """
    artifacts = {c.artifact for c in claims}
    values = {str(c.value) for c in claims if c.value is not None}
    values |= {f"{int(c.value):,}" for c in claims
               if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)}
    return artifacts, values


def evidence_vocabulary(finding) -> tuple[set[str], set[str]]:
    """The artifacts and values an explanation of a whole finding may name."""
    return claims_vocabulary(finding.claims)


def check_grounded(text: str, finding) -> list[str]:
    """Every artifact and every large number must come from the evidence.

    Checked mechanically because a model asked nicely not to invent things
    will still do it, and an explanation that names a fifth artifact reads
    exactly like one that names the four real ones.
    """
    return check_grounded_in_claims(text, finding.claims)


def check_grounded_in_claims(text: str, claims) -> list[str]:
    """`check_grounded`, against an explicit claim set. See `claims_vocabulary`."""
    artifacts, values = claims_vocabulary(claims)
    problems = []

    # Matched case-insensitively. A model writing prose capitalises a path
    # that starts a sentence -- "Application/config.py states..." -- and
    # that is English, not a different file. The question this check asks is
    # whether the model named an artifact OUTSIDE the evidence set, and
    # rejecting a real artifact over a capital letter would train a reader
    # to ignore the rejections.
    # Both loops below sort, because `problems` is text a person reads.
    # Two runs over one snapshot listing the same refusals in a
    # different order cannot be diffed, and a rejection nobody can diff
    # gets ignored.
    known = {a.lower() for a in artifacts}
    for named in sorted(set(re.findall(
            r"[\w./-]+\.(?:py|yaml|yml|md|xlsx|json)", text))):
        if named.lower() not in known:
            problems.append(f"names {named}, which is not in the evidence set")

    for literal in sorted(set(re.findall(r"\b\d[\d,]{2,}\b", text))):
        if literal in values:
            continue
        try:
            numeric = int(literal.replace(",", ""))
        except ValueError:
            continue
        if numeric < _SMALL:
            continue
        if str(numeric) in values or f"{numeric:,}" in values:
            continue
        problems.append(f"states {literal}, which no cited artifact states")

    return problems


def check_keeps_both_sides(text: str, finding) -> list[str]:
    """An explanation of a value conflict must state every value.

    Grounding is not enough. The first live Gemini answer on the hero
    conflict cited every artifact correctly, invented nothing, and said the
    sources "all consistently reference the exact numeric value 1,000,000"
    -- omitting the 500,000 that makes this a finding at all. Nothing in the
    grounding check could see it: every artifact was real and every number
    was in the evidence.

    A summariser drifts toward the majority, and eight of the nine claims
    say 1,000,000. That is the failure mode of putting a model anywhere near
    a disagreement, and it is why the classification is deterministic and
    only the narration is not.
    """
    if finding.conflict_type != "value_mismatch":
        return []

    stated = {c.value for c in finding.claims if c.value is not None}
    if len(stated) < 2:
        return []

    missing = []
    for value in sorted(stated, key=str):
        forms = {str(value)}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            forms.add(f"{int(value):,}")
        if not any(form in text for form in forms):
            missing.append(str(value))

    if missing:
        return [f"describes a value conflict without stating "
                f"{', '.join(missing)}: an explanation that loses a side has "
                f"lost the conflict"]
    return []


# --- the model's part ----------------------------------------------------------


def build_prompt(finding) -> str:
    """The request, with artifact content sealed in a labelled data block.

    The model is asked for ONE thing -- why these claims might describe a
    single rule -- and explicitly told not to decide which is correct. That
    is not politeness: a model that both generates the claims and picks a
    winner has no check on it, and the disagreement is the product (I15).
    """
    quoted = "\n\n".join(
        as_data_block(c.evidence.quote, artifact=f"{c.artifact}:{c.locator}")
        for c in sorted(finding.claims, key=lambda c: (c.artifact, c.locator)))

    return (
        "Several artifacts in one enterprise appear to state the same business "
        "rule in different words. Explain, in at most four sentences, why they "
        "might be describing a single rule.\n\n"
        "Rules for your answer:\n"
        "- Name only the artifacts and numbers that appear in the data below.\n"
        "- State EVERY differing value. Do not summarise them into the most "
        "common one: the disagreement is the point, and an explanation that "
        "mentions only the majority value has hidden it.\n"
        "- Do NOT say which artifact is correct. That is a person's decision.\n"
        "- If you are unsure they describe one rule, say so.\n\n"
        f"Rule identifier under discussion: {finding.semantic_id}\n\n"
        f"{quoted}\n"
    )


# --- one question per representation --------------------------------


def value_groups(finding) -> list[tuple[object, tuple]]:
    """The claims of a finding, split by the value they state.

    A group is the unit a model is allowed to see. Ordered by value so the
    joined explanation is byte-identical across runs -- a narration that
    reshuffles between two runs of one snapshot cannot be diffed, and a
    reader who cannot diff it stops reading it.
    """
    buckets: dict[object, list] = {}
    for claim in sorted(finding.claims, key=lambda c: (c.artifact, c.locator)):
        if claim.value is None:
            continue
        buckets.setdefault(claim.value, []).append(claim)
    return [(value, tuple(claims))
            for value, claims in sorted(buckets.items(), key=lambda kv: str(kv[0]))]


def build_representation_prompt(finding, value, claims) -> str:
    """Ask about ONE stated value, showing only the evidence for it.

    This is the whole of the fix. The single-prompt version showed the model
    all nine claims and asked for one explanation; eight of them said
    1,000,000, and both Gemini tiers wrote that the sources "all
    consistently reference" it -- dropping the 500,000 that makes this a
    finding at all. No wording of the instruction survived that, including a
    bullet stating in as many words that every differing value must appear.

    So the majority is removed from the question rather than argued with.
    Each call sees one value and the artifacts stating it, and is asked what
    those artifacts say -- not which is right, and not what they all agree
    on, because inside a group there is nothing left to summarise away.
    Joining the groups is arithmetic, and arithmetic has no majority to
    drift toward.
    """
    quoted = "\n\n".join(
        as_data_block(c.evidence.quote, artifact=f"{c.artifact}:{c.locator}")
        for c in claims)

    return (
        "The artifacts below all state the SAME value for one business rule. "
        "In at most two sentences, say what they require and under what "
        "conditions.\n\n"
        "Rules for your answer:\n"
        f"- The value these artifacts state is {value}. State it.\n"
        "- Name only the artifacts and numbers that appear in the data below. "
        "Other artifacts in this enterprise state a DIFFERENT value; they are "
        "deliberately not shown to you, so do not guess at them and do not "
        "claim these artifacts are consistent with anything you cannot see.\n"
        "- Do NOT say whether this value is correct. That is a person's "
        "decision.\n\n"
        f"Rule identifier under discussion: {finding.semantic_id}\n\n"
        f"{quoted}\n"
    )


def explain_by_representation(finding, *, proposer) -> tuple[str, list[str]]:
    """One model call per stated value, joined by a script.

    Returns the joined part 2 and the refusals. A group whose answer fails
    its own grounding check is dropped and recorded; the remaining groups
    are still reported, because losing one side's NARRATION is a smaller
    loss than losing the finding -- part 1 states every value regardless of
    what any model said.
    """
    groups = value_groups(finding)
    rejected: list[str] = []
    paragraphs: list[str] = []

    for value, claims in groups:
        # Grounded against this group only. A group answer that reaches for
        # the majority value is naming a number outside its own evidence,
        # which is the same defect as inventing one -- and, unlike in the
        # single-prompt version, it is now visible to the existing check.
        try:
            answer = proposer.explain(
                build_representation_prompt(finding, value, claims))
        except Exception as exc:  # noqa: BLE001 -- a provider failure is not an answer
            rejected.append(f"{value}: proposer failed: {type(exc).__name__}: {exc}")
            continue

        if not answer or not answer.strip():
            rejected.append(f"{value}: proposer returned nothing")
            continue

        problems = check_grounded_in_claims(answer, claims)
        if problems:
            rejected.append(f"{value}: not grounded: " + "; ".join(problems))
            continue

        named = sorted({c.artifact for c in claims})
        verb = "states" if len(named) == 1 else "state"
        paragraphs.append(f"{', '.join(named)} {verb} {value}. {answer.strip()}")

    if not paragraphs:
        return "", rejected

    # The sentence carrying the disagreement is written here, not by a model.
    # It is the one sentence that has to be true.
    header = (f"{len(groups)} different values are stated for "
              f"{finding.semantic_id}: "
              f"{', '.join(str(v) for v, _ in groups)}.")
    return header + "\n\n" + "\n\n".join(paragraphs), rejected


def explain(finding, *, proposer=None, resolution=None,
            per_representation=None) -> ExplanationAttempt:
    """Assemble the three parts, refusing an ungrounded middle section.

    With no proposer the explanation is still produced: parts 1 and 3 are
    deterministic, and part 2 says plainly that no model was available. A
    smaller answer, honestly arrived at, beats a fabricated one -- and it is
    also the behaviour a judge can check by unplugging the provider.
    """
    attempt = ExplanationAttempt()
    part1 = what_differs(finding)
    part3 = "\n".join(f"- {item}" for item in uncertainties(
        finding, resolution=resolution)) or "- nothing further is uncertain"
    artifacts, _ = evidence_vocabulary(finding)

    if proposer is None:
        attempt.explanation = Explanation(
            what_differs=part1,
            why_one_rule=("No semantic proposer is configured, so no "
                          "explanation of the grouping is available. The "
                          "grouping itself was made deterministically."),
            still_uncertain=part3,
            grounded_in=tuple(sorted(artifacts)),
            proposer="none")
        return attempt

    if per_representation is None:
        # Default ON exactly where the single-prompt version was measured to
        # fail: a value conflict with more than one value to lose.
        per_representation = (finding.conflict_type == "value_mismatch"
                              and len({c.value for c in finding.claims
                                       if c.value is not None}) > 1)

    if per_representation:
        part2, refusals = explain_by_representation(finding, proposer=proposer)
        attempt.rejected.extend(refusals)
        if not part2:
            return attempt
    else:
        try:
            part2 = proposer.explain(build_prompt(finding))
        except Exception as exc:  # noqa: BLE001 -- a provider failure is not an answer
            attempt.rejected.append(f"proposer failed: {type(exc).__name__}: {exc}")
            return attempt

        if not part2 or not part2.strip():
            attempt.rejected.append("proposer returned nothing")
            return attempt

    # Both checks run on the FINAL text, whichever route produced it. The
    # per-representation join satisfies `check_keeps_both_sides` by
    # construction, and it is checked anyway -- a property guaranteed by
    # construction and never asserted survives exactly until someone edits
    # the constructor.
    problems = check_grounded(part2, finding) + check_keeps_both_sides(part2, finding)
    if problems:
        attempt.rejected.append("explanation is not grounded: " + "; ".join(problems))
        return attempt

    attempt.explanation = Explanation(
        what_differs=part1,
        why_one_rule=part2.strip(),
        still_uncertain=part3,
        grounded_in=tuple(sorted(artifacts)),
        proposer=getattr(proposer, "name", proposer.__class__.__name__))
    return attempt
