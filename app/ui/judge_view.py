# -*- coding: utf-8 -*-
"""What a judge sees, in the order they see it. A view model, not a screen.

The thesis is one sentence -- *change one thing, see what follows* -- and the
job of the first screen is to make that concrete in about five seconds,
without a paragraph of introduction and without an empty chat box.

Three rules the layout enforces, each fixed by the product contract:

**No alignment score on the first screen**. A percentage invites
"by what standard?" as the reader's first thought, and that question pulls
attention away from the specific finding. Counted facts -- four conflicts,
three unknowns -- are not argued with. The score lives behind Technical
Evidence.

**No ground truth on the first screen either.** A "seeded 3 / detected 3"
table is powerful with a technical reader, but as the FIRST thing on the
page the message inverts: *we hid the answers and then found them*. The
first screen answers "what did it find"; the evidence of our own strictness
is one click away, where it reads as rigour rather than as staging.

**Nothing here recommends a decision**. The screen presents what
is known, what is unknown, and what follows. The moment it advises, a
person's approval stops meaning they judged it.

This module builds data. It renders no HTML and holds no verdict -- every
number on it was computed by the deterministic core and is passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The judge's path, in order. Each step is a question they can answer by
#: looking, not a feature name.
JOURNEY = (
    ("current reality", "what does the system say today?"),
    ("intended change", "what does someone want to change?"),
    ("detected impact", "what would move if they did?"),
    ("contradictions", "what would disagree afterwards?"),
    ("prepared changeset", "what exactly would be done?"),
    ("human approval", "who has to agree, and on what evidence?"),
    ("verification", "did it actually land?"),
    ("external drift", "what if someone changes it outside the system?"),
    ("reconciliation", "what does the system do about that?"),
)

#: Things that may never appear on the first screen. Each has a reason and
#: a test.
NOT_ON_FIRST_SCREEN = (
    "alignment_score",
    "ground_truth",
    "seeded",
    "precision",
    "recall",
    "confidence",
)


class FirstScreenViolation(Exception):
    """Something reached the first screen that belongs behind a click."""


@dataclass(frozen=True)
class Headline:
    """One specific finding, pushed at the reader rather than waited for.

    A blank prompt asks the reader to invent a question about a domain they
    met four seconds ago. This is the alternative: state a real finding and
    offer the next moves.
    """

    rule: str
    summary: str
    #: `(artifact, stated value, note)` -- the disagreement itself, which is
    #: the whole argument for the product in three rows.
    statements: tuple = ()
    consequence: str = ""

    def rendered(self) -> str:
        out = [f"  > AI FOUND A CONFLICT", f"    {self.summary}"]
        for artifact, value, note in self.statements:
            suffix = f"   <- {note}" if note else ""
            out.append(f"       {artifact:<14} {value}{suffix}")
        if self.consequence:
            out.append(f"    {self.consequence}")
        return "\n".join(out)


@dataclass
class FirstScreen:
    """The second thing a judge sees, and the one that has to earn the rest."""

    operation_name: str
    headline: Headline | None = None
    #: Counted, never scored. Nobody asks "four conflicts by what standard?"
    counts: dict = field(default_factory=dict)
    actions: tuple = ()
    notes: list = field(default_factory=list)

    def __post_init__(self) -> None:
        for banned in NOT_ON_FIRST_SCREEN:
            if banned in self.counts:
                raise FirstScreenViolation(
                    f"{banned!r} is on the first screen. A percentage invites "
                    f"'by what standard?' before the reader has seen the "
                    f"finding, and ground truth first reads as 'we hid the "
                    f"answers and found them'. Both belong behind "
                    f"[Technical Evidence]")

    def rendered(self) -> str:
        out = [f"{self.operation_name}", ""]
        if self.headline:
            out.append(self.headline.rendered())
            out.append("")
        if self.counts:
            out.append("  " + "     ".join(
                f"{value} {name}" for name, value in sorted(self.counts.items())))
            out.append("")
        if self.actions:
            out.append("  " + "  ".join(f"[{a}]" for a in self.actions))
        out.append("")
        out.append("  " + "." * 35 + "  [Technical Evidence]")
        return "\n".join(out)


@dataclass
class TechnicalEvidence:
    """Behind one click: the score, the answer key, and our own misses.

    A submission willing to show its false positives and the case it missed
    earns a kind of trust that no amount of polish does. So the misses are a
    field of this object rather than something a caller may forget to pass.
    """

    alignment_score: float | None = None
    ground_truth: tuple = ()
    false_positives: tuple = ()
    missed: tuple = ()
    metric_scope: str = ""
    notes: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ground_truth and not self.metric_scope:
            raise FirstScreenViolation(
                "a ground-truth table with no stated scope reads as a claim "
                "about systems in general")

    def rendered(self) -> str:
        out = ["TECHNICAL EVIDENCE", ""]
        if self.ground_truth:
            out.append("  SEEDED GROUND TRUTH            DETECTED")
            for label, seeded, detected, note in self.ground_truth:
                mark = "OK" if seeded == detected else f"{seeded - detected} missed"
                suffix = f"  ({note})" if note else ""
                out.append(f"  {label:<30} {detected} / {seeded}    "
                           f"{mark}{suffix}")
            out.append("")
        out.append(f"  False positives                {len(self.false_positives)}"
                   + (f"  ({', '.join(self.false_positives)})"
                      if self.false_positives else ""))
        out.append(f"  Missed                         {len(self.missed)}"
                   + (f"  ({', '.join(self.missed)})" if self.missed else ""))
        if self.alignment_score is not None:
            out.append("")
            out.append(f"  Alignment score                "
                       f"{self.alignment_score:.0%}")
        if self.metric_scope:
            out.append("")
            out.append(f"  Measured on: {self.metric_scope}")
        for note in self.notes:
            out.append(f"  note: {note}")
        return "\n".join(out)


def build_first_screen(*, operation_name, headline=None, counts=None,
                       actions=()) -> FirstScreen:
    return FirstScreen(
        operation_name=operation_name, headline=headline,
        counts=dict(counts or {}), actions=tuple(actions))


def journey_steps() -> tuple:
    return JOURNEY


def check_no_recommendation(text: str) -> list:
    """Phrases the interface may not use. Returns what it found.

    Returned rather than raised so a caller can report several at once; the
    test raises.
    """
    banned = ("we recommend", "you should approve", "safe to approve",
              "recommended action", "best option", "suggested decision")
    lowered = text.lower()
    return [phrase for phrase in banned if phrase in lowered]


__all__ = [
    "FirstScreen",
    "FirstScreenViolation",
    "Headline",
    "JOURNEY",
    "NOT_ON_FIRST_SCREEN",
    "TechnicalEvidence",
    "build_first_screen",
    "check_no_recommendation",
    "journey_steps",
]
