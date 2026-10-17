# -*- coding: utf-8 -*-
"""The first screen, and the one number that is allowed on it.

    PROCUREMENT & ORDERS -- System Archaeology

      > approval_threshold appears in 8 places with 2 different values
        (1000000, 500000). Gap: 500,000.                  [see evidence]

      3  undocumented
      4  stale
      3  conflicts        <- 2 of them touch money
      2  unknown
      3  high-impact

Three decisions shape this module, and each was made against the obvious
alternative.

**No health score.** `alignment = ALIGNED / total` is still computed, and it
is not on this screen. "82% by what standard?" is the first question a
reader asks, and answering it costs the attention that a specific finding
would have had. `3 undocumented, 2 conflicts` are counts nobody argues
with. A composite number always needs explaining; a list of findings is
already the evidence. The score goes to the technical section, with ground
truth and eval metrics, where it serves the audience it was built for.

**`stale` does not mean "older".** The tempting definition -- the
representation with the earlier date is the stale one -- is `pick_newest`
wearing a different name, and `discovery` refuses that by construction
(I15). A newer spreadsheet is not a more correct spreadsheet; it may be the
record of somebody changing a threshold without approval, which is the
scenario this product exists to catch.

So staleness here is EVIDENCED, not inferred: a representation is stale when
a recorded change said in writing that it was temporary or that something
else would be synchronised separately, and no later change carries that out.
Somebody wrote down an intention and it did not happen. That is a fact about
the changelog, not a verdict about which artifact is right.

**There is no LOW.** Everything here is already a finding: it cleared
detection, it carries evidence, and nobody has looked at it yet. The
classification answers "which of these first", not "which may I skip", and
LOW reads as the second. The first version of this module ranked an
undocumented approval waiver -- declared temporary in 2020, raised by
governance in 2021, still running -- as LOW, because it states no monetary
value and every term in the original sketch was shaped like money. The label
was wrong before the formula was, and the formula was wrong too: it now
scores recorded intentions that nobody carried out.

I9: everything here renders deterministically from the Knowledge Model and
the findings. Nothing is asked of a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Points needed before a finding is called high-impact. Written down rather
#: than tuned, because "which of these should I read first" is the most
#: useful classification on the screen and a reader is entitled to check it.
HIGH_IMPACT_THRESHOLD = 3

#: A rule stated in more than this many artifact families is one whose
#: change has to be coordinated across teams.
MANY_FAMILIES = 3

#: Reach at which reviewing one artifact stops being enough.
WIDE_REACH = 5

_MONEY = re.compile(r"\b(JPY|USD|EUR|GBP|yen|¥|\$)\b", re.IGNORECASE)
_COMPLIANCE = re.compile(
    r"\b(audit|compliance|control|regulat\w+|sign-?off|segregation)\b", re.IGNORECASE)

UNKNOWN_IMPACT = "UNKNOWN"

#: There is deliberately NO "low". Everything on this screen is already a
#: finding -- it cleared detection, it has evidence, and a person has not
#: yet looked at it. The classification answers "which of these first",
#: not "which of these may I skip", and LOW reads as permission to skip.
#: The first version of this module ranked an undocumented approval waiver,
#: declared temporary in 2020 and still running, as LOW because it states
#: no monetary value. The label was wrong before the formula was.
IMPACT_LEVELS = ("HIGH", "MEDIUM", UNKNOWN_IMPACT)


@dataclass(frozen=True)
class ImpactTerm:
    """One named term of the formula, with what it was measured from."""

    name: str
    fired: bool
    detail: str
    measured: bool = True


@dataclass(frozen=True)
class ImpactAssessment:
    """Why a finding is or is not worth reading first.

    Carries the terms rather than only the verdict. A ranking a reader
    cannot audit is a ranking they will either trust blindly or ignore, and
    both are worse than a list in arbitrary order.
    """

    finding_id: str
    level: str  # one of IMPACT_LEVELS
    points: int
    terms: tuple[ImpactTerm, ...]

    @property
    def why(self) -> str:
        fired = [t.detail for t in self.terms if t.fired]
        if not fired:
            # Never rendered as an error or a dash. Zero terms firing means
            # this finding is narrower than the others, not that it is
            # spurious -- it still has evidence and still needs a person.
            return "no impact term fired; listed because it is still a finding"
        return "; ".join(fired)

    @property
    def unmeasured(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.terms if not t.measured)


def _numeric_values(finding) -> list[float]:
    return [c.value for c in finding.claims
            if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)]


def value_gap(finding) -> float:
    """The distance between the extremes of what is stated.

    Zero is a real answer: a scope or existence mismatch has no value gap
    and is not thereby unimportant. It simply does not score on this term.
    """
    values = _numeric_values(finding)
    return (max(values) - min(values)) if len(values) > 1 else 0.0


def assess_impact(finding, *, graph=None, stale=()) -> ImpactAssessment:
    """The formula, written out.

        high_impact = f(artifact families affected,
                        touches money or compliance,
                        size of the value gap,
                        how far the rule reaches,
                        recorded intentions never carried out)

    The last term is not in the original sketch and was added because
    the sketch ranked the wrong finding first. `strategic_customer_exemption`
    waives approval entirely, was declared temporary in November 2020, was
    raised by governance in 2021 with "no follow-up recorded", and states no
    number at all -- so every money-shaped term missed it. A rule somebody
    has twice written down an intention to fix is a rule that has been
    abandoned, and that is exactly what archaeology is for.

    Each term is evidence-derived rather than keyed off a rule name: money
    and compliance are detected in the QUOTES, so the formula keeps working
    on an enterprise whose rules are not called what this seed calls them.
    """
    families = {c.family for c in finding.claims}
    gap = value_gap(finding)
    quotes = " ".join(c.evidence.quote or "" for c in finding.claims)

    terms = [
        ImpactTerm(
            name="families",
            fired=len(families) >= MANY_FAMILIES,
            detail=(f"stated in {len(families)} artifact families "
                    f"({', '.join(sorted(families))})")),
        ImpactTerm(
            name="money",
            fired=bool(_MONEY.search(quotes)) and bool(_numeric_values(finding)),
            detail="the evidence is denominated in money"),
        ImpactTerm(
            name="compliance",
            fired=bool(_COMPLIANCE.search(quotes)),
            detail="an audit or compliance artifact states this rule"),
        ImpactTerm(
            name="value_gap",
            fired=gap > 0,
            detail=f"the stated values differ by {gap:,.0f}"),
    ]

    mine = [item for item in stale if item.semantic_id == finding.semantic_id]
    temporary = [i for i in mine if "temporary" in i.why]
    unsynchronised = [i for i in mine if "separately" in i.why]
    terms.append(ImpactTerm(
        name="declared_temporary",
        fired=bool(temporary),
        detail=("a change declared this temporary and no revert was "
                "recorded: " + ", ".join(i.change_id for i in temporary))
               if temporary else "no change declared this temporary"))
    terms.append(ImpactTerm(
        name="declared_unsynchronised",
        fired=bool(unsynchronised),
        detail=("a change said another representation would be updated "
                "separately and none was: "
                + ", ".join(i.change_id for i in unsynchronised))
               if unsynchronised else "no unsynchronised change recorded"))

    if graph is None:
        # Not measured, and said so. Counting an unmeasured term as zero is
        # how a finding nobody assessed comes out looking safe.
        terms.append(ImpactTerm(
            name="reach", fired=False, measured=False,
            detail="reach not measured: no enterprise graph was supplied"))
    else:
        scope = graph.impact_scope(finding.semantic_id)
        reach = len(scope.get("direct", [])) + len(scope.get("indirect", []))
        terms.append(ImpactTerm(
            name="reach",
            fired=reach >= WIDE_REACH,
            detail=f"{reach} artifacts and call sites pass through this rule"))

    points = sum(1 for t in terms if t.fired)
    unmeasured = [t for t in terms if not t.measured]

    if points >= HIGH_IMPACT_THRESHOLD:
        level = "HIGH"
    elif unmeasured or finding.gaps:
        # I5 again, in the place it is easiest to forget: a finding with an
        # open gap, or one scored on incomplete inputs, is not low-impact.
        # It is UNASSESSED, and any other label sends a reader past it.
        level = UNKNOWN_IMPACT
    else:
        level = "MEDIUM"

    return ImpactAssessment(finding_id=finding.finding_id, level=level,
                            points=points, terms=tuple(terms))


# --- staleness -------------------------------------------------------------------


@dataclass(frozen=True)
class StaleRepresentation:
    """An intention somebody recorded and nobody carried out."""

    semantic_id: str
    change_id: str
    date: str
    author: str
    why: str
    artifacts: tuple[str, ...]


def stale_from_history(records) -> tuple[StaleRepresentation, ...]:
    """Changes that declared themselves unfinished.

    Reads only what the changelog SAYS about itself. The alternative -- call
    the older representation stale -- would decide every conflict in the
    product by date, which is the one thing `discovery` is built not to do.
    """
    stale = []
    for record in records or ():
        payload = getattr(record, "payload", {}) or {}
        if payload.get("type") != "change_event":
            continue
        reasons = []
        if payload.get("declares_temporary"):
            reasons.append("declared temporary, with no revert recorded")
        if payload.get("declares_unsynchronised"):
            reasons.append("declared that another representation would be "
                           "updated separately, and none was")
        if not reasons:
            continue
        for semantic_id in payload.get("semantic_ids") or ():
            stale.append(StaleRepresentation(
                semantic_id=semantic_id,
                change_id=payload["change_id"],
                date=payload.get("date", ""),
                author=payload.get("author", ""),
                why="; ".join(reasons)
                    + (f" -- {payload['note']}" if payload.get("note") else ""),
                artifacts=tuple(payload.get("artifacts") or ())))

    return tuple(sorted(stale, key=lambda s: (s.date, s.change_id, s.semantic_id)))


# --- the score that is not on the screen --------------------------------------------


def alignment_score(model) -> tuple[int, int, float]:
    """`ALIGNED / total`, computed and deliberately not headlined.

    Returned as its own parts as well as the ratio, so whoever displays it
    in the technical section shows the reader what it is a ratio OF.
    """
    from core.change_intelligence.states import AlignmentState

    records = [r for r in model.records if r.semantic_id]
    aligned = sum(1 for r in records if r.alignment == AlignmentState.ALIGNED)
    total = len(records)
    return aligned, total, (aligned / total if total else 1.0)


# --- the screen -----------------------------------------------------------------------


@dataclass
class DiscoveryScreen:
    title: str
    headline: str = ""
    counts: dict = field(default_factory=dict)
    ranked: tuple = ()
    impacts: dict = field(default_factory=dict)
    stale: tuple = ()
    technical: dict = field(default_factory=dict)


def _headline(finding, assessment) -> str:
    """One sentence, about one finding, with the number in it.

    The screen leads with a FINDING rather than a summary, because the
    sentence a reader repeats afterwards is the one they read first.
    """
    places = len(finding.claims)
    values = sorted({str(c.value) for c in finding.claims if c.value is not None})
    gap = value_gap(finding)
    sentence = (f"{finding.semantic_id} appears in {places} places with "
                f"{len(values)} different values ({', '.join(values)}).")
    if gap:
        sentence += f" Gap: {gap:,.0f}."
    return sentence


def build_screen(report, *, model=None, records=None, graph=None,
                 title="System Archaeology") -> DiscoveryScreen:
    """Render the first screen from the findings. No model is consulted."""
    stale = stale_from_history(records)
    impacts = {f.finding_id: assess_impact(f, graph=graph, stale=stale)
               for f in report.findings}

    # Ranked by impact, then by the counts a reader can verify. Sorted
    # rather than "most recent first": a stable order is what lets two runs
    # of the same snapshot be compared at all (I9).
    order = {"HIGH": 0, UNKNOWN_IMPACT: 1, "MEDIUM": 2}
    ranked = tuple(sorted(
        report.findings,
        key=lambda f: (order[impacts[f.finding_id].level],
                       -impacts[f.finding_id].points,
                       f.kind, f.semantic_id)))

    conflicts = [f for f in report.findings if f.kind == "CONFLICT"]
    money_conflicts = [f for f in conflicts
                       if any(t.name == "money" and t.fired
                              for t in impacts[f.finding_id].terms)]

    counts = {
        "undocumented": len([f for f in report.findings if f.kind == "UNDOCUMENTED"]),
        "stale": len({(s.semantic_id, s.change_id) for s in stale}),
        "conflicts": len(conflicts),
        "conflicts_touching_money": len(money_conflicts),
        "unknown": len(report.gaps) + len([f for f in report.findings
                                           if impacts[f.finding_id].level == UNKNOWN_IMPACT]),
        "high_impact": len([f for f in report.findings
                            if impacts[f.finding_id].level == "HIGH"]),
    }

    headline = ""
    if ranked:
        headline = _headline(ranked[0], impacts[ranked[0].finding_id])

    technical = {}
    if model is not None:
        aligned, total, score = alignment_score(model)
        # Here, and nowhere near the headline.
        technical["alignment"] = {"aligned": aligned, "total": total,
                                  "score": round(score, 4)}

    return DiscoveryScreen(title=title, headline=headline, counts=counts,
                           ranked=ranked, impacts=impacts, stale=stale,
                           technical=technical)


def render(screen: DiscoveryScreen) -> str:
    """The screen as text, deterministic from the same inputs (I9)."""
    out = [f"{screen.title.upper()}", "=" * len(screen.title)]
    if screen.headline:
        out += ["", f"  > {screen.headline}", "    [see evidence]"]

    out.append("")
    for key in ("undocumented", "stale", "conflicts", "unknown", "high_impact"):
        line = f"  {screen.counts[key]:>2}  {key.replace('_', '-')}"
        if key == "conflicts" and screen.counts["conflicts_touching_money"]:
            line += (f"        <- {screen.counts['conflicts_touching_money']} "
                     f"of them touch money")
        out.append(line)

    out.append("")
    for finding in screen.ranked:
        assessment = screen.impacts[finding.finding_id]
        out.append(f"  [{assessment.level:<7}] {finding.kind:<13} {finding.semantic_id}")
        out.append(f"            {assessment.why}")
        for name in assessment.unmeasured:
            out.append(f"            (term '{name}' was not measured)")

    if screen.stale:
        out += ["", "  STALE -- recorded intentions nobody carried out"]
        for item in screen.stale:
            out.append(f"    {item.change_id} ({item.date}, {item.author}) "
                       f"{item.semantic_id}")
            out.append(f"      {item.why}")

    return "\n".join(out)
