# -*- coding: utf-8 -*-
"""Risk from signals that can be recounted, not from a model's mood.

Six dimensions:

    scope | customer impact | financial impact
    compliance / audit exposure | workflow disruption | unknown

A risk label an LLM assigns freely is the first thing a judge will question,
and correctly: "why is this HIGH?" has to have an answer made of countable
things. So the level is decided by a script and every dimension reports the
number it was computed from. A model may narrate the result; it does not
choose it.

**The hard rule: any UNKNOWN in the impact set floors the level at MEDIUM.**
A region nobody has assessed is not a safe region. Reporting it as LOW is
how a system tells somebody a change is fine when what it means is that it
did not look.

Unlike the discovery screen, LOW exists here and is meaningful: a
change with an empty impact set genuinely is low risk, and saying so is
`NO_CHANGE_REQUIRED`'s companion. The two screens answer different
questions -- that one ranks findings a person must read, this one assesses
a change a person is considering.
"""

from __future__ import annotations

from dataclasses import dataclass

LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"

#: Ordered, so "at least MEDIUM" is expressible as arithmetic rather than
#: as a chain of conditionals somebody will later reorder.
LEVELS = (LOW, MEDIUM, HIGH)

#: The six dimensions, named so a test can assert none was quietly dropped.
DIMENSIONS = (
    "scope",
    "customer_impact",
    "financial_impact",
    "compliance_exposure",
    "workflow_disruption",
    "unknown",
)

#: Points at which the overall level moves. Written down because "why is
#: this HIGH" must have an answer a reader can check against the numbers
#: printed beside it.
MEDIUM_AT = 2
HIGH_AT = 4


@dataclass(frozen=True)
class Dimension:
    """One axis of risk, its verdict, and the count behind it."""

    name: str
    level: str
    detail: str
    measured: bool = True

    @property
    def points(self) -> int:
        return {LOW: 0, MEDIUM: 1, HIGH: 2}[self.level]

    @property
    def is_floor_only(self) -> bool:
        """MEDIUM because nobody looked, rather than because of a finding."""
        return not self.measured


@dataclass
class RiskAssessment:
    level: str
    points: int
    dimensions: tuple[Dimension, ...]
    floors: tuple = ()

    @property
    def why(self) -> str:
        raised = [d for d in self.dimensions if d.level != LOW]
        if not raised:
            return "no dimension is above LOW"
        return "; ".join(f"{d.name}: {d.detail}" for d in raised)

    def dimension(self, name: str) -> Dimension:
        for dimension in self.dimensions:
            if dimension.name == name:
                return dimension
        raise KeyError(name)


def _at_least(level: str, floor: str) -> str:
    return LEVELS[max(LEVELS.index(level), LEVELS.index(floor))]


def assess(*, impact=None, simulation=None, diff=None,
           touches_public_api: bool = False) -> RiskAssessment:
    """Compute the level from countable signals.

        risk = f(artifact families affected,
                 touches a public API or contract,
                 tests that would go red,
                 an inconsistency the change introduces,
                 an audit control affected,
                 an UNKNOWN anywhere in the impact set)

    Every argument is optional and every absence is reported as unmeasured,
    because a risk assessment computed on nothing must not look like a
    reassuring one.
    """
    dimensions: list[Dimension] = []
    floors: list[str] = []

    # --- scope ---------------------------------------------------------
    if impact is None:
        dimensions.append(Dimension(
            "scope", MEDIUM, "no impact set supplied; scope not measured",
            measured=False))
        floors.append("scope was not measured")
    else:
        kinds = len(impact.kinds_touched)
        artifacts = len(impact.artifacts)
        level = HIGH if kinds >= 5 else MEDIUM if kinds >= 3 else LOW
        dimensions.append(Dimension(
            "scope", level,
            f"{artifacts} artifacts across {kinds} kinds of object"))

    # --- customer impact -------------------------------------------------
    if diff is None:
        dimensions.append(Dimension(
            "customer_impact", MEDIUM,
            "no operational diff supplied; effect on requests not measured",
            measured=False))
        floors.append("customer impact was not measured")
    else:
        fraction = diff.changed_fraction
        level = HIGH if fraction >= 0.3 else MEDIUM if fraction > 0.0 else LOW
        dimensions.append(Dimension(
            "customer_impact", level,
            f"{diff.changed} of {diff.total} requests "
            f"({fraction:.0%}) would be handled differently"))

    # --- financial impact -------------------------------------------------
    if diff is None:
        dimensions.append(Dimension(
            "financial_impact", MEDIUM, "not measured", measured=False))
    else:
        amounts = [s.amount for s in diff.shifts
                   if isinstance(s.amount, (int, float))]
        total = sum(amounts)
        level = MEDIUM if amounts else LOW
        dimensions.append(Dimension(
            "financial_impact", level,
            (f"{len(amounts)} requests worth {total:,.0f} in total change "
             f"their approval path" if amounts else
             "no request changes its approval path")))

    # --- compliance / audit exposure ----------------------------------------
    controls = impact.kind("audit_control") if impact is not None else ()
    if impact is None:
        dimensions.append(Dimension(
            "compliance_exposure", MEDIUM, "not measured", measured=False))
    else:
        level = HIGH if (controls and touches_public_api) else \
            MEDIUM if controls else LOW
        dimensions.append(Dimension(
            "compliance_exposure", level,
            (f"{len(controls)} audit control(s) state this value: "
             f"{', '.join(c.identifier for c in controls)}" if controls else
             "no audit control references this rule")))

    # --- workflow disruption -------------------------------------------------
    #
    # An inconsistency the change INTRODUCES is the strongest signal
    # available: it means part of the system will not follow the new value,
    # so the change is not the one-line edit it appears to be.
    introduced = list(simulation.new_inconsistencies()) if simulation else []
    failing_tests = len(impact.kind("test")) if impact is not None else 0
    if simulation is None and impact is None:
        dimensions.append(Dimension(
            "workflow_disruption", MEDIUM, "not measured", measured=False))
    else:
        level = HIGH if introduced else MEDIUM if failing_tests else LOW
        dimensions.append(Dimension(
            "workflow_disruption", level,
            (f"{len(introduced)} request(s) would be decided differently by "
             f"two routes; something does not read the changed value"
             if introduced else
             f"{failing_tests} test file(s) assert the old value"
             if failing_tests else
             "no route or test contradicts the change")))

    # --- unknown --------------------------------------------------------------
    #
    # With no impact set at all, this dimension is UNMEASURED -- not
    # "contains UNKNOWN". Reporting the latter states something about an
    # impact set that was never computed, and it awarded a point for a
    # finding nobody made. The floor is raised either way; only the claim
    # in the detail line and the point differ, and both were wrong.
    if impact is None:
        has_unknown = True
        dimensions.append(Dimension(
            "unknown", MEDIUM,
            "no impact set supplied, so nothing was classified at all",
            measured=False))
        floors.append("nothing was classified")
    else:
        has_unknown = impact.has_unknown()
        dimensions.append(Dimension(
            "unknown", MEDIUM if has_unknown else LOW,
            ("the impact set contains UNKNOWN entries; a region nobody "
             "assessed is not a safe region" if has_unknown else
             "every part of the impact set was classified")))
        if has_unknown:
            floors.append("the impact set contains UNKNOWN")

    # MEASURED dimensions only. An unmeasured one raises the FLOOR to
    # MEDIUM and contributes no points, because "nobody looked" is not
    # evidence of danger. Counting it as danger produced HIGH for a change
    # nothing had analysed -- a false alarm, and false alarms teach people
    # to ignore the label just as surely as false reassurance does.
    points = sum(d.points for d in dimensions if d.measured)
    level = HIGH if points >= HIGH_AT else MEDIUM if points >= MEDIUM_AT else LOW

    # The hard rule, applied last so nothing below can undo it.
    if has_unknown or any(not d.measured for d in dimensions):
        level = _at_least(level, MEDIUM)

    return RiskAssessment(level=level, points=points,
                          dimensions=tuple(dimensions), floors=tuple(floors))


def render(assessment: RiskAssessment) -> str:
    out = [f"RISK  {assessment.level}   ({assessment.points} points)"]
    for dimension in assessment.dimensions:
        marker = " " if dimension.measured else "?"
        out.append(f"  {marker} {dimension.name:<22} {dimension.level:<7} "
                   f"{dimension.detail}")
    for floor in assessment.floors:
        out.append(f"    floor: {floor}, so this cannot be reported below MEDIUM")
    return "\n".join(out)


__all__ = [
    "DIMENSIONS",
    "Dimension",
    "HIGH",
    "HIGH_AT",
    "LEVELS",
    "LOW",
    "MEDIUM",
    "MEDIUM_AT",
    "RiskAssessment",
    "assess",
    "render",
]
