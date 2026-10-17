# -*- coding: utf-8 -*-
"""Metrics that state what they measured and against what.

A number without its denominator is a claim; a number with its denominator,
its answer key and its scope is a measurement. Every `Metric` here carries
all four, and `Metric` cannot be constructed without them.

**Scope is mandatory and it is stated in the output**, not in a footnote. A
result measured on one seed corpus reported as "system accuracy" is the
single most common dishonesty in an evaluation, and the only reliable
defence is to make the scope impossible to drop: it travels inside the
object, `rendered()` prints it, and a test asserts it appears.

**Negative cases sit beside positive ones in the same table.** Any system
finds something if nobody measures how often it finds things that are not
there. The abstention and no-change metrics are the ones that make the
recall figures worth reading.

**Nothing here is scored by a model.** Gemini may explain a finding; it does
not decide whether the finding was correct. The answer keys are hand-written
and the comparison is exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Where a measurement applies. There is no "general" member on purpose.
SEED_CORPUS = "the reference seed corpus, as committed"
SCENARIO_KEY = "the 24 hand-written scenarios in scenarios/corpus.yaml"
IMPACT_KEY = "the hand-written impact answer key IMPACT-001"
SESSION_TESTS = "the session-isolation tests in this suite"
SCOPES = (SEED_CORPUS, SCENARIO_KEY, IMPACT_KEY, SESSION_TESTS)


class UnscopedMetric(Exception):
    """A number reported without saying what it was measured on."""


@dataclass(frozen=True)
class Metric:
    """One measurement, with everything needed to disbelieve it."""

    name: str
    numerator: int
    denominator: int
    answer_key: str
    scope: str
    question: str
    #: Cases the system was RIGHT to stay silent about. Reported separately
    #: from the numerator: folding them in would let an abstention inflate
    #: a recall figure, which is the opposite of what it should do.
    abstained: int = 0
    notes: tuple = ()

    def __post_init__(self) -> None:
        if not self.answer_key:
            raise UnscopedMetric(
                f"{self.name} has no answer key; then 'correct' means "
                f"'whatever the system said'")
        if self.scope not in SCOPES:
            raise UnscopedMetric(
                f"{self.name} claims scope {self.scope!r}, which is not one "
                f"of the declared scopes. A metric whose scope is free text "
                f"is a metric that will be quoted as universal")
        if self.denominator < 0 or self.numerator < 0:
            raise UnscopedMetric(f"{self.name}: counts cannot be negative")
        if self.numerator > self.denominator:
            raise UnscopedMetric(
                f"{self.name}: {self.numerator} of {self.denominator} is "
                f"more than all of them")

    @property
    def value(self) -> float | None:
        """`None` when nothing was measured. Never 1.0, never 0.0.

        Zero of zero is not perfect and it is not a failure; it is an
        absence, and returning a number for it would put a figure on a
        slide that no data supports.
        """
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    @property
    def measured(self) -> bool:
        return self.denominator > 0

    def rendered(self) -> str:
        if not self.measured:
            return (f"{self.name:<34} not measured -- no case in "
                    f"{self.scope}")
        figure = f"{self.value:.2f}"
        abstained = f"  ({self.abstained} correct abstentions)" \
            if self.abstained else ""
        return (f"{self.name:<34} {figure}   "
                f"{self.numerator}/{self.denominator}{abstained}\n"
                f"{'':<34} key: {self.answer_key}\n"
                f"{'':<34} scope: {self.scope}")


@dataclass
class Report:
    """A set of metrics and the lineage of the data they ran on."""

    metrics: tuple = ()
    baseline_id: str = ""
    corpus_lineage: str = ""
    notes: list = field(default_factory=list)

    def by_name(self, name: str) -> Metric:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        raise KeyError(name)

    @property
    def measured(self) -> tuple:
        return tuple(m for m in self.metrics if m.measured)

    @property
    def unmeasured(self) -> tuple:
        return tuple(m for m in self.metrics if not m.measured)

    def rendered(self) -> str:
        out = ["EVALUATION REPORT", ""]
        if self.corpus_lineage:
            out.append(f"  corpus lineage: {self.corpus_lineage}")
        if self.baseline_id:
            out.append(f"  baseline: {self.baseline_id[:12]}")
        out.append("")
        for metric in self.metrics:
            out.append("  " + metric.rendered().replace("\n", "\n  "))
            out.append("")
        for note in self.notes:
            out.append(f"  note: {note}")
        out.append("")
        out.append("  Every figure above is scoped to the dataset named "
                   "beside it.")
        out.append("  None of them is a claim about systems in general.")
        return "\n".join(out)


# --- the metric definitions ---------------------------------------------------
#
# Each function takes observations and an answer key and returns a Metric.
# None of them look anything up for themselves: a metric that fetched its
# own ground truth could fetch it from the thing it is measuring.


def precision(name, *, found, expected, answer_key, scope, question) -> Metric:
    """Of what the system reported, how much was real."""
    found, expected = set(found), set(expected)
    return Metric(name=name, numerator=len(found & expected),
                  denominator=len(found), answer_key=answer_key, scope=scope,
                  question=question)


def recall(name, *, found, expected, answer_key, scope, question) -> Metric:
    """Of what was real, how much the system reported."""
    found, expected = set(found), set(expected)
    return Metric(name=name, numerator=len(found & expected),
                  denominator=len(expected), answer_key=answer_key,
                  scope=scope, question=question)


def unnecessary_change_rate(*, proposed, required, answer_key, scope) -> Metric:
    """Actions proposed that nothing required.

    Deliberately phrased so that LOW is good and the numerator counts the
    system's mistakes. A metric that rewards proposing more is a metric a
    planner learns to game by proposing everything.
    """
    proposed, required = set(proposed), set(required)
    unnecessary = proposed - required
    return Metric(
        name="unnecessary-change rate",
        numerator=len(unnecessary), denominator=len(proposed),
        answer_key=answer_key, scope=scope,
        question="does it invent work?",
        notes=tuple(sorted(unnecessary)))


def abstention(*, ambiguous_cases, abstained_on, answer_key, scope) -> Metric:
    """Cases where the right answer was to say nothing, and it did.

    The counterweight to recall. Without this, the cheapest way to score
    well everywhere else is to answer every question confidently.
    """
    ambiguous, abstained = set(ambiguous_cases), set(abstained_on)
    return Metric(
        name="correct abstention",
        numerator=len(ambiguous & abstained), denominator=len(ambiguous),
        answer_key=answer_key, scope=scope,
        question="does it know when it does not know?",
        abstained=len(abstained))


def introduced_vs_inherited(*, classified, key, answer_key, scope) -> Metric:
    """Did it attribute each contradiction to the right cause?

    `classified` and `key` are `{case_id: "introduced" | "inherited"}`. The
    failure this measures is blaming a proposal for a problem it inherited,
    which makes a reviewer reject a correct change.
    """
    agree = sum(1 for case, verdict in key.items()
                if classified.get(case) == verdict)
    return Metric(
        name="introduced-vs-inherited accuracy",
        numerator=agree, denominator=len(key), answer_key=answer_key,
        scope=scope,
        question="does it blame the change for what it inherited?")


def exact_match(name, *, observed, expected, answer_key, scope,
                question) -> Metric:
    """`{case: value}` against a key, compared exactly."""
    agree = sum(1 for case, value in expected.items()
                if observed.get(case) == value)
    return Metric(name=name, numerator=agree, denominator=len(expected),
                  answer_key=answer_key, scope=scope, question=question)


# --- the runner ---------------------------------------------------------------


def build_report(observations: dict, keys: dict, *, baseline_id: str = "",
                 corpus_lineage: str = "") -> Report:
    """Assemble the report from observations and hand-written keys.

    Both are passed in. This function fetches nothing, so it cannot fetch a
    key from the system it is scoring -- the one mistake that would make
    every figure here meaningless while leaving them all looking fine.
    """
    metrics = []

    if "impact_found" in observations:
        metrics.append(precision(
            "impact precision", found=observations["impact_found"],
            expected=keys["impact_expected"], answer_key=IMPACT_KEY,
            scope=IMPACT_KEY, question="is what-if trustworthy?"))
        metrics.append(recall(
            "impact recall", found=observations["impact_found"],
            expected=keys["impact_expected"], answer_key=IMPACT_KEY,
            scope=IMPACT_KEY, question="does it miss things?"))

    if "conflicts_found" in observations:
        metrics.append(recall(
            "contradiction detection", found=observations["conflicts_found"],
            expected=keys["conflicts_expected"],
            answer_key="seeded conflicts in seed_manifest.yaml",
            scope=SEED_CORPUS, question="does it find the real conflicts?"))

    if "actions_proposed" in observations:
        metrics.append(unnecessary_change_rate(
            proposed=observations["actions_proposed"],
            required=keys["actions_required"],
            answer_key="hand-written required-action list",
            scope=SEED_CORPUS))

    if "abstained_on" in observations:
        metrics.append(abstention(
            ambiguous_cases=keys["ambiguous_cases"],
            abstained_on=observations["abstained_on"],
            answer_key="hand-written ambiguous-rule list",
            scope=SEED_CORPUS))

    if "contradiction_classification" in observations:
        metrics.append(introduced_vs_inherited(
            classified=observations["contradiction_classification"],
            key=keys["contradiction_key"],
            answer_key="hand-written introduced/inherited key",
            scope=SEED_CORPUS))

    if "verification_verdicts" in observations:
        metrics.append(exact_match(
            "verification correctness",
            observed=observations["verification_verdicts"],
            expected=keys["verification_key"],
            answer_key="hand-written verification outcomes",
            scope=SEED_CORPUS,
            question="does it agree with a person about what happened?"))

    if "drift_found" in observations:
        metrics.append(recall(
            "drift detection", found=observations["drift_found"],
            expected=keys["drift_expected"],
            answer_key="hand-written out-of-band edit list",
            scope=SEED_CORPUS,
            question="does it notice a change nobody approved?"))

    if "isolation_observed" in observations:
        metrics.append(exact_match(
            "session isolation",
            observed=observations["isolation_observed"],
            expected=keys["isolation_key"],
            answer_key="one session must never observe another's state",
            scope=SESSION_TESTS,
            question="can two people use it at once?"))

    report = Report(metrics=tuple(metrics), baseline_id=baseline_id,
                    corpus_lineage=corpus_lineage)
    if report.unmeasured:
        report.notes.append(
            f"{len(report.unmeasured)} metric(s) had no case in scope and "
            f"are reported as not measured rather than as a perfect score")
    return report


def summary() -> dict:
    """What the public `evaluation.report` operation returns.

    Every entry carries its scope, so a caller cannot quote a figure
    without quoting what it was measured on.
    """
    return {
        "metrics": [
            {"name": name, "question": question, "scope": scope}
            for name, question, scope in (
                ("impact precision", "is what-if trustworthy?", IMPACT_KEY),
                ("impact recall", "does it miss things?", IMPACT_KEY),
                ("contradiction detection", "does it find the real ones?",
                 SEED_CORPUS),
                ("unnecessary-change rate", "does it invent work?",
                 SEED_CORPUS),
                ("correct abstention", "does it know when it does not know?",
                 SEED_CORPUS),
                ("introduced-vs-inherited accuracy",
                 "does it blame the change for what it inherited?",
                 SEED_CORPUS),
                ("verification correctness",
                 "does it agree about what happened?", SEED_CORPUS),
                ("drift detection", "does it notice an unapproved change?",
                 SEED_CORPUS),
                ("session isolation", "can two people use it at once?",
                 SESSION_TESTS),
            )
        ],
        "disclaimer": ("every figure is scoped to the dataset named beside "
                       "it and is not a claim about systems in general"),
    }


__all__ = [
    "IMPACT_KEY",
    "Metric",
    "Report",
    "SCENARIO_KEY",
    "SCOPES",
    "SEED_CORPUS",
    "SESSION_TESTS",
    "UnscopedMetric",
    "abstention",
    "build_report",
    "exact_match",
    "introduced_vs_inherited",
    "precision",
    "recall",
    "summary",
    "unnecessary_change_rate",
]
