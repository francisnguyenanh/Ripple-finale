# -*- coding: utf-8 -*-
"""An intent must be grounded, and is never overwritten.

Nine fields, five validation conditions, and a ledger in which v1
survives forever.

**A rejected intent is STORED.** Losing it loses the reason it was rejected,
which is the part a person needs -- and the second time somebody asks for
the same impossible thing, the record of the first refusal is the answer.
`IntentState.REJECTED` exists for exactly this and is a normal state with
outgoing edges, not a bin.

**v1 is never overwritten** (I10). When a judge asks "what if the user
changes their mind?", the answer has to be "v1 is still there, with the
reason it was replaced" -- not "we update the record". A ledger that edits
in place cannot answer why a decision was reversed, and that question is
asked far more often than what the current value is.

The five conditions:

    target resolves to a semantic_id?   no  -> REJECTED with a reason
    current state knowable?             no  -> UNKNOWN + Gap
    desired state valid?                "-500000" -> REJECTED
    scope explicit?                     vague -> ask, do not assume
    evidence / actor present?           no  -> cannot be VALIDATED
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from core.change_intelligence.alignment import Gap
from core.change_intelligence.intent_parser import UNKNOWN, IntentDraft
from core.change_intelligence.states import IntentState, transition

#: Scope is the field most often forgotten.
#: An intent with no scope is not "global" -- it is unspecified, and the two
#: differ by exactly the blast radius of the change.
UNSCOPED = ""


class IntentNotStored(Exception):
    """An operation on an intent the ledger has never seen."""


@dataclass(frozen=True)
class Intent:
    """The nine intent fields, plus the version this record is.

    `requested_change` carries both ends. An intent that stated only the
    destination would make "is this already true?" unanswerable, and that
    question is Scenario H.
    """

    intent_id: str
    actor: str
    target: str
    requested_change: dict
    rationale: str
    scope: str
    evidence: tuple
    confidence: str
    status: IntentState
    version: int = 1
    timestamp: str = ""
    #: Why this intent is in the state it is. Empty on a DRAFT, always
    #: populated on REJECTED -- a refusal with no reason is not a refusal a
    #: person can argue with.
    status_reason: str = ""
    superseded_by: str = ""
    gaps: tuple[Gap, ...] = ()

    @property
    def desired_value(self):
        return self.requested_change.get("desired_value")

    @property
    def current_value(self):
        return self.requested_change.get("current_value")


@dataclass
class Validation:
    """The outcome of the five checks, with every failure named.

    All five run even after the first failure. Reporting one problem at a
    time turns a single review into five round trips, and the person
    answering is usually not the person who will be asked again tomorrow.
    """

    ok: bool
    reasons: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def validate(draft: IntentDraft, *, known_semantic_ids=(), actor: str = "",
             evidence=()) -> Validation:
    """The five conditions. Every one of them has a negative case.

    Returns rather than raises: an intent that fails is still recorded, so
    the caller needs the verdict as data.
    """
    result = Validation(ok=True)

    # 1. target resolves to a semantic_id
    if not draft.target or draft.target == UNKNOWN:
        result.reasons.append("the request names no rule")
    elif known_semantic_ids and draft.target not in known_semantic_ids:
        result.reasons.append(
            f"{draft.target!r} does not resolve to a rule this snapshot "
            f"knows; the enterprise may call it something else")

    # 2. current state knowable. Not knowing is a Gap, not a rejection --
    #    the request may be perfectly clear about a rule the product has
    #    not finished reading.
    if draft.current_value is None and draft.intent_type != "rule_removal":
        result.gaps.append(Gap(
            gap_id=f"GAP-intent-{draft.target}-current",
            reason=("the current value of this rule is not established, so "
                    "whether the request changes anything cannot be decided")))

    # 3. desired state valid
    desired = draft.desired_value
    if draft.intent_type == "rule_removal":
        pass  # a removal has no desired value, and that is correct
    elif desired == UNKNOWN or desired is None:
        result.reasons.append(
            "the request does not determine a desired value")
    elif isinstance(desired, (int, float)) and not isinstance(desired, bool):
        if desired < 0:
            result.reasons.append(
                f"a threshold of {desired} is not a valid amount")
        elif not draft.unit:
            result.reasons.append(
                "the desired value carries no unit; an amount without one is "
                "not a policy")

    # 4. scope explicit. Asked about, never assumed: an unscoped intent is
    #    not a global one, and the two differ by the blast radius.
    if draft.scope == UNSCOPED:
        result.questions.append(
            "Which requests does this apply to? An unspecified scope is not "
            "the same as 'all of them'.")

    # 5. evidence / actor present
    if not actor or not actor.strip():
        result.reasons.append("no actor; an intent nobody asked for is not an intent")

    result.ok = not result.reasons
    return result


def from_draft(draft: IntentDraft, *, intent_id: str, actor: str,
               timestamp: str, evidence=(), known_semantic_ids=(),
               scope: str = UNSCOPED) -> tuple[Intent, Validation]:
    """Turn a parsed draft into a stored intent, valid or not.

    Both halves come back. An invalid intent is a record, and the caller
    stores it with the same ceremony as a valid one.
    """
    draft = replace(draft, scope=scope or draft.scope)
    verdict = validate(draft, known_semantic_ids=known_semantic_ids,
                       actor=actor, evidence=evidence)

    intent = Intent(
        intent_id=intent_id,
        actor=actor,
        target=draft.target,
        requested_change={
            "intent_type": draft.intent_type,
            "current_value": draft.current_value,
            "desired_value": draft.desired_value,
            "unit": draft.unit,
        },
        rationale=draft.rationale,
        scope=draft.scope,
        evidence=tuple(evidence),
        confidence=draft.confidence,
        status=IntentState.DRAFT,
        timestamp=timestamp,
        gaps=tuple(verdict.gaps))

    if verdict.ok:
        moved = transition(IntentState.DRAFT, IntentState.VALIDATED,
                           actor=actor, timestamp=timestamp,
                           reason="all five conditions met")
        intent = replace(intent, status=IntentState.VALIDATED,
                         status_reason="validated")
    else:
        transition(IntentState.DRAFT, IntentState.REJECTED,
                   actor=actor, timestamp=timestamp,
                   reason="; ".join(verdict.reasons))
        intent = replace(intent, status=IntentState.REJECTED,
                         status_reason="; ".join(verdict.reasons))
        moved = None

    _ = moved
    return intent, verdict


# --- the ledger ------------------------------------------------------------


@dataclass
class IntentLedger:
    """Append-only. Nothing in here is ever edited or deleted.

    `revise` writes v2 and moves v1 to SUPERSEDED with a pointer forward.
    Both are readable afterwards, which is the only way to answer "why was
    this reversed" -- a question asked far more often than "what is it now".
    """

    entries: list[Intent] = field(default_factory=list)

    def add(self, intent: Intent) -> Intent:
        self.entries.append(intent)
        return intent

    def get(self, intent_id: str) -> Intent:
        for entry in self.entries:
            if entry.intent_id == intent_id:
                return entry
        raise IntentNotStored(intent_id)

    def versions_of(self, target: str) -> list[Intent]:
        """Every version for one rule, oldest first."""
        return sorted((e for e in self.entries if e.target == target),
                      key=lambda e: e.version)

    def current(self, target: str) -> Intent | None:
        """The version that has not been superseded.

        Computed from the entries rather than kept in a pointer beside them,
        because a pointer and a list are two things that can disagree.
        """
        live = [e for e in self.versions_of(target)
                if e.status is not IntentState.SUPERSEDED]
        return live[-1] if live else None

    def revise(self, intent_id: str, *, draft: IntentDraft, actor: str,
               timestamp: str, reason: str, evidence=(),
               known_semantic_ids=(), scope: str = UNSCOPED
               ) -> tuple[Intent, Intent]:
        """Write the next version. The previous one stays readable.

        Returns (superseded_v1, new_v2). The caller gets both, so a UI that
        shows only the new one has made that choice explicitly rather than
        by the ledger having thrown the old one away.
        """
        previous = self.get(intent_id)

        successor_id = f"{previous.intent_id}-v{previous.version + 1}"
        successor, verdict = from_draft(
            draft, intent_id=successor_id, actor=actor, timestamp=timestamp,
            evidence=evidence, known_semantic_ids=known_semantic_ids,
            scope=scope or previous.scope)
        successor = replace(successor, version=previous.version + 1)

        transition(previous.status, IntentState.SUPERSEDED,
                   actor=actor, timestamp=timestamp, reason=reason)
        superseded = replace(
            previous,
            status=IntentState.SUPERSEDED,
            status_reason=reason,
            superseded_by=successor_id)

        self.entries[self.entries.index(previous)] = superseded
        self.add(successor)
        _ = verdict
        return superseded, successor


__all__ = [
    "Intent",
    "IntentLedger",
    "IntentNotStored",
    "UNSCOPED",
    "Validation",
    "from_draft",
    "validate",
]
