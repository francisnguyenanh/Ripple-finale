# -*- coding: utf-8 -*-
"""The four screens, as a view model a renderer cannot argue with.

    1. pick a rule        from the Knowledge Model, with evidence
    2. change the value   validated for unit and sign
    3. see the result     impact (DIRECT first) + operational diff + risk
    4. decide             propose -> P7, or discard

A view model rather than a web page, for the same reason the discovery
report is: a screen whose only renderer is a browser can only be reviewed by
looking at it, and "looking at it" is not a test. The same structure renders
to text here and to HTML at P9.

Two ordering rules, and both are about what a reader does with the screen:

**DIRECT comes first.** A reader given nine artifacts in arbitrary order
reads the first three. Those three had better be the ones where the change
is actually made, or the ordering has quietly decided what they will look
at and got it wrong.

**The inconsistency comes before the decision buttons.** A person who has
already clicked "propose" has stopped reading. The one thing they must see
is that part of the system will not follow the new value.

P6 answers "what if". It does NOT create a ChangeSet -- that is P7's job,
and `decide()` returns a request rather than performing one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.impact import DIRECT, INDIRECT, UNKNOWN
from core.change_intelligence.risk import render as render_risk


class InvalidProposedValue(Exception):
    """A value the screen refuses to run a simulation on.

    Caught here rather than at the engine, because the useful message is
    about the input a person typed and the engine has no idea what they
    typed it into.
    """


@dataclass(frozen=True)
class RuleChoice:
    """Screen 1. A rule a person may run a what-if on."""

    semantic_id: str
    current_value: object
    unit: str
    stated_in: tuple
    evidence: tuple
    alignment: str = ""

    @property
    def is_contested(self) -> bool:
        """More than one value among its representations.

        Worth surfacing on the PICK screen: running a what-if on a rule
        whose current value is disputed produces a "from" column that is
        already wrong, and the person should know before they start.
        """
        return len({v for v in self.stated_in if v is not None}) > 1


def choices_from(model) -> tuple:
    """Every rule with a value, from the Knowledge Model. No free text."""
    out = []
    for record in sorted(model.records, key=lambda r: r.semantic_id):
        values = [rep.value for rep in record.representations if rep.value is not None]
        if not values:
            continue
        out.append(RuleChoice(
            semantic_id=record.semantic_id,
            current_value=values[0],
            unit=next((getattr(rep, "unit", "") for rep in record.representations
                       if getattr(rep, "unit", "")), ""),
            stated_in=tuple(values),
            evidence=tuple(f"{e.artifact}:{e.locator}" for e in record.evidence),
            alignment=getattr(record.alignment, "name", "") or ""))
    return tuple(out)


def validate_value(proposed, *, current, unit: str = "") -> object:
    """Screen 2. Reject what cannot be a policy before anything runs.

    A simulation on a negative threshold burns a second and produces a
    confident answer to a question that does not make sense, and the
    person then has to work out which of the two numbers was the problem.
    """
    if proposed is None or proposed == "":
        raise InvalidProposedValue("no value was entered")

    if isinstance(current, (int, float)) and not isinstance(current, bool):
        try:
            value = type(current)(proposed)
        except (TypeError, ValueError) as exc:
            raise InvalidProposedValue(
                f"{proposed!r} is not a number, and {unit or 'this rule'} "
                f"is stated as one") from exc
        if value < 0:
            raise InvalidProposedValue(
                f"{value} is negative; a threshold cannot be below zero")
        return value

    return proposed


@dataclass
class WhatIfView:
    """Screens 3 and 4, assembled. Nothing here has been proposed yet."""

    target: str
    current_value: object
    proposed_value: object
    session_id: str
    impact: object = None
    diff: object = None
    risk: object = None
    notes: list = field(default_factory=list)

    @property
    def ordered_impact(self) -> tuple:
        """DIRECT, then INDIRECT, then UNKNOWN. Never by artifact name.

        A reader reads the first three rows. They must be the ones where
        the change is actually made.
        """
        if self.impact is None:
            return ()
        return (self.impact.tier(DIRECT)
                + self.impact.tier(INDIRECT)
                + self.impact.tier(UNKNOWN))

    @property
    def has_inconsistency(self) -> bool:
        return bool(self.diff is not None and self.diff.inconsistencies)


def build_view(*, target, current_value, proposed_value, session_id,
               impact=None, diff=None, risk=None) -> WhatIfView:
    view = WhatIfView(target=target, current_value=current_value,
                      proposed_value=proposed_value, session_id=session_id,
                      impact=impact, diff=diff, risk=risk)
    if impact is None:
        view.notes.append("impact not computed")
    if diff is None:
        view.notes.append("no simulation was run, so no operational figures")
    return view


@dataclass(frozen=True)
class ProposalRequest:
    """Screen 4. What P7 receives -- and it is a REQUEST, not a ChangeSet.

    P6 answers "what if". P7 answers "how". Building a ChangeSet here would
    mean the act of LOOKING at a change had started making one.
    """

    target: str
    current_value: object
    proposed_value: object
    session_id: str
    actor: str
    timestamp: str
    risk_level: str
    impact_summary: str
    carries_inconsistency: bool


def decide(view: WhatIfView, *, accept: bool, actor: str,
           timestamp: str) -> ProposalRequest | None:
    """Propose, or discard. Discarding returns None and leaves no trace."""
    if not accept:
        return None
    if not actor or not timestamp:
        raise InvalidProposedValue(
            "a proposal carries the name of whoever made it (I12)")

    direct = len(view.impact.tier(DIRECT)) if view.impact else 0
    total = len(view.impact.nodes) if view.impact else 0
    return ProposalRequest(
        target=view.target,
        current_value=view.current_value,
        proposed_value=view.proposed_value,
        session_id=view.session_id,
        actor=actor,
        timestamp=timestamp,
        risk_level=getattr(view.risk, "level", ""),
        impact_summary=f"{direct} direct, {total} total",
        carries_inconsistency=view.has_inconsistency)


def render(view: WhatIfView) -> str:
    """Screens 3 and 4 as text. Deterministic from the same inputs."""
    out = [
        f"WHAT IF   {view.target}   "
        f"{view.current_value} -> {view.proposed_value}",
        "",
    ]

    if view.impact is not None:
        out.append("IMPACT")
        current_tier = None
        for node in view.ordered_impact:
            if node.tier != current_tier:
                current_tier = node.tier
                out.append(f"  [{current_tier}]")
            out.append(f"    {node.identifier}  ({node.kind})")
            out.append(f"        {node.reason}")

    if view.diff is not None:
        from core.change_intelligence.behavioral_diff import render as render_diff
        out += ["", render_diff(view.diff)]

    if view.risk is not None:
        out += ["", render_risk(view.risk)]

    if view.has_inconsistency:
        # Before the buttons, on purpose. A person who has already clicked
        # "propose" has stopped reading.
        out += ["", "  ^ part of the system will NOT follow the new value"]

    out += ["", "  [ Propose Change ]   [ Discard ]"]
    for note in view.notes:
        out.append(f"  note: {note}")
    return "\n".join(out)


__all__ = [
    "InvalidProposedValue",
    "ProposalRequest",
    "RuleChoice",
    "WhatIfView",
    "build_view",
    "choices_from",
    "decide",
    "render",
    "validate_value",
]
