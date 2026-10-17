# -*- coding: utf-8 -*-
"""Simulation results become a sentence an operations manager reads.

    OPERATIONAL DIFF   approval threshold  <old> -> <new>

      <N> scenarios simulated
      <M> would be handled differently
      <K> unchanged

      Operational impact  [SIMULATED]
        manager approvals / month:  ~<a>  ->  ~<b>

      INCONSISTENCY FOUND
        two routes decide one request differently after this change

"Manager approvals go from a to b a month" is the sentence that makes a
business reader understand the consequence. It is worth more than any list
of files, and it is also the easiest number in the product to fake -- which
is why no figure is written in this module, not even in this example.

Three rules, each with a test:

**Nothing here is a constant.** Every count comes from the transaction
corpus the caller supplies. A test walks the AST of this package looking for
those integers as literals and expects none. Change one record in the corpus
and the numbers move -- also a test.

**Every operational figure carries `[SIMULATED]`.** The reference operation
is modelled, not observed. If a reader believes these are production
measurements, that is a false claim, and when they discover it every other
number in the submission loses its value too. The label is part of the
string, not a footnote somebody might drop when pasting it into a slide.

**The per-month rate says what it divided by.** A monthly figure derived
from nine months of data and presented as though it were a year is a
different number. `months_observed` comes from the timestamps in the corpus,
and travels with the figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Mandatory on every derived operational figure. Not a flag a caller may
#: turn off -- a caller who wants it off wants to make a claim the product
#: cannot support.
SIMULATED = "[SIMULATED]"


@dataclass(frozen=True)
class OutcomeShift:
    """One case that would be handled differently, with both answers."""

    case_id: str
    before: str
    after: str
    amount: object = None
    detail: str = ""


@dataclass(frozen=True)
class Rate:
    """A per-month figure, with the arithmetic that produced it.

    `basis` exists because the number is meaningless without it, and
    because the label and the source travel together or not at all.
    """

    label: str
    before_per_month: float
    after_per_month: float
    months_observed: int
    basis: str

    def rendered(self) -> str:
        return (f"{self.label}: ~{self.before_per_month:.0f}  ->  "
                f"~{self.after_per_month:.0f}   {SIMULATED}")


@dataclass
class OperationalDiff:
    target: str
    current_value: object
    proposed_value: object
    total: int = 0
    changed: int = 0
    unchanged: int = 0
    shifts: tuple[OutcomeShift, ...] = ()
    rates: tuple[Rate, ...] = ()
    inconsistencies: tuple = ()
    roles_affected: tuple = ()
    source: str = ""
    notes: list = field(default_factory=list)

    @property
    def changed_fraction(self) -> float:
        return self.changed / self.total if self.total else 0.0


def _months_observed(corpus) -> int:
    """Distinct year-months present in the data.

    Counting records and dividing by twelve would report a rate per month
    that no month in the data actually resembles, and the corpus does not
    have to span a year.
    """
    months = set()
    for case in corpus:
        stamp = str(case.get("timestamp") or "")
        if len(stamp) >= 7:
            months.add(stamp[:7])
    return len(months) or 1


def _outcome_of(outcomes, effective: str) -> str:
    answers = {o.handler: o.outcome for o in outcomes if not o.error}
    for handler, outcome in answers.items():
        if effective and handler.endswith(effective):
            return outcome
    return next(iter(answers.values()), "")


def build_diff(result, corpus, *, target: str, current_value, proposed_value,
               effective_handler=None, counted_outcome: str = "",
               role_of=None, source: str = "") -> OperationalDiff:
    """Turn a SimulationResult into the operational story.

    `counted_outcome` is the decision whose frequency the rate describes --
    "manager approvals per month" counts cases ending in manager approval,
    not cases that changed. Those are different numbers and the second one
    is not a workload.
    """
    by_case = {c.case_id: c for c in result.cases}
    corpus_by_id = {str(c.get("historical_case_id") or c.get("id")): c
                    for c in corpus}

    shifts = []
    for case in result.changed:
        raw = corpus_by_id.get(case.case_id, {})
        effective = (effective_handler(raw) if callable(effective_handler)
                     else (effective_handler or ""))
        shifts.append(OutcomeShift(
            case_id=case.case_id,
            before=_outcome_of(case.current, effective),
            after=_outcome_of(case.shadow, effective),
            amount=raw.get("amount"),
            detail=(f"{raw.get('process', 'request')} "
                    f"{raw.get('amount', '')} {raw.get('category', '')}").strip()))
    shifts.sort(key=lambda s: s.case_id)

    months = _months_observed(corpus)
    rates = ()
    if counted_outcome:
        before = after = 0
        for case in result.cases:
            raw = corpus_by_id.get(case.case_id, {})
            effective = (effective_handler(raw) if callable(effective_handler)
                         else (effective_handler or ""))
            before += _outcome_of(case.current, effective) == counted_outcome
            after += _outcome_of(case.shadow, effective) == counted_outcome
        rates = (Rate(
            label=f"{counted_outcome.replace('_', ' ')} / month",
            before_per_month=before / months,
            after_per_month=after / months,
            months_observed=months,
            basis=(f"{len(result.cases)} scenarios simulated over "
                   f"{months} months of the reference operation's seeded "
                   f"history")),)

    roles = ()
    if role_of is not None:
        roles = tuple(sorted({
            role_of(s.after) for s in shifts if role_of(s.after)
        } - {role_of(s.before) for s in shifts if role_of(s.before)}))
        if not roles:
            roles = tuple(sorted({role_of(s.after) for s in shifts
                                  if role_of(s.after)}))

    diff = OperationalDiff(
        target=target,
        current_value=current_value,
        proposed_value=proposed_value,
        total=result.total,
        changed=len(result.changed),
        unchanged=len(result.unchanged),
        shifts=tuple(shifts),
        rates=rates,
        inconsistencies=tuple(result.new_inconsistencies()),
        roles_affected=roles,
        source=source)

    diff.notes.append(
        f"every operational figure here is {SIMULATED} on {result.total} "
        f"scenarios"
        + (f" from {diff.source}" if diff.source else "")
        + "; these are modelled outcomes for a reference operation, not "
          "measurements of a live one")
    _ = by_case
    return diff


def render(diff: OperationalDiff) -> str:
    """The block a business reader sees. Deterministic from the same inputs."""
    out = [
        f"OPERATIONAL DIFF   {diff.target}  "
        f"{diff.current_value} -> {diff.proposed_value}",
        "",
        f"  {diff.total:>3} scenarios simulated",
        f"  {diff.changed:>3} would be handled differently",
        f"  {diff.unchanged:>3} unchanged",
    ]

    if diff.shifts:
        out += ["", "  Examples:"]
        for shift in diff.shifts[:3]:
            out.append(f"    {shift.case_id}  {shift.detail}")
            out.append(f"      now:    {shift.before}")
            out.append(f"      shadow: {shift.after}")

    if diff.rates:
        out += ["", f"  Operational impact  {SIMULATED}"]
        for rate in diff.rates:
            out.append(f"    {rate.rendered()}")
            out.append(f"      source: {rate.basis}")

    if diff.roles_affected:
        out.append(f"    roles affected: {', '.join(diff.roles_affected)}")

    if diff.inconsistencies:
        out += ["", "  INCONSISTENCY INTRODUCED BY THIS CHANGE"]
        for case in diff.inconsistencies:
            answers = sorted({o.outcome for o in case.shadow if not o.error})
            out.append(
                f"    {case.case_id}: {len(case.disagreeing_handlers())} routes "
                f"decide this request, and they return {', '.join(answers)}")
        out.append(
            "      one of these routes does not read the changed value; it "
            "needs a CODE change, not a configuration change")

    return "\n".join(out)


__all__ = [
    "OperationalDiff",
    "OutcomeShift",
    "Rate",
    "SIMULATED",
    "build_diff",
    "render",
]
