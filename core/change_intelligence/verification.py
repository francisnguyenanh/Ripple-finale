# -*- coding: utf-8 -*-
"""What actually happened, read off the artifacts.

The whole of this module rests on refusing to trust one thing: its own
input. A ChangeSet says what was meant to happen. An approval says somebody
agreed to it. An apply result says a write was attempted. **None of them is
evidence that the artifact says what it should say now.** Only the artifact
is that, and the only way to know is to open it.

That distinction sounds pedantic until the first time an apply half
succeeds. Then "the ChangeSet says APPLIED" and "the spreadsheet still says
the old number" are both true, and a verifier that read the first one
reports success on a system that is inconsistent.

So verification answers five questions, from snapshots and nothing else:

    what was expected to change?      from the approved structured edits
    what actually changed?            snapshot_before.diff_from(after)
    what did NOT change that should?  MISSING -- never "verified"
    what changed that should not?     UNEXPECTED
    was anything changed afterwards?  REVERSE DRIFT

**No auto-repair.** This module detects, explains, and hands a person a
next action. It never writes. A verifier that quietly fixed what it found
would make the drift it exists to surface invisible -- and the next time
somebody edited a value out of band, the system would tidy it away and
report that everything is fine.

**No second semantics.** DIRECT/INDIRECT/UNKNOWN, contradiction detection
and rule identity all come from P6 through the objects passed in. Nothing
here re-derives them. Two implementations of the same business rule
disagree eventually, and the disagreement surfaces as a verdict a person
cannot explain.

**No clock, no randomness.** `actor` and `timestamp` are arguments, never
defaults. Given the same two snapshots and the same ChangeSet, the result
is the same object -- which is what makes "verify again" a safe operation
and what lets a replay test mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.states import AlignmentState

#: The six dimensions reconciled after a change. Ordered as a reader asks
#: about them: what runs, then what describes it, then what checks it.
DIMENSIONS = ("workflow", "software", "data", "documents", "tests", "evidence")

#: Every way an expected edit can turn out. `MISSING` is the one that
#: matters: an intended change that did not land is NOT a pass, however
#: green everything around it looks.
APPLIED = "APPLIED"
MISSING = "MISSING"
DIVERGED = "DIVERGED"
#: Present, but this verifier cannot read it -- a binary format with no
#: adapter supplied, a file that no longer parses. Deliberately NOT folded
#: into MISSING: "the old value is still there" and "nobody can tell what
#: is there" are different facts, and only the first tells a person what to
#: do. It blocks a pass either way, which is the part that must not depend
#: on getting the distinction right.
UNREADABLE = "UNREADABLE"
OUTCOMES = (APPLIED, MISSING, DIVERGED, UNREADABLE)

#: Why a snapshot's artifact differs when no approved edit asked it to.
UNEXPECTED = "UNEXPECTED"
REVERSE_DRIFT = "REVERSE_DRIFT"


class CannotVerify(Exception):
    """The inputs do not describe a verifiable situation."""


class VerificationWrites(Exception):
    """Reserved: raised if a repair path is ever added here.

    Named so that the prohibition is a symbol a test can look for, rather
    than a sentence in a docstring that a future change quietly contradicts.
    """


@dataclass(frozen=True)
class EditOutcome:
    """One approved edit, and what the artifact says now."""

    artifact: str
    path: str
    expected: object
    actual: object
    outcome: str
    detail: str = ""

    @property
    def landed(self) -> bool:
        return self.outcome == APPLIED

    def rendered(self) -> str:
        if self.landed:
            return f"{self.artifact}  {self.path}: {self.actual}  OK"
        return (f"{self.artifact}  {self.path}: expected {self.expected}, "
                f"found {self.actual}  {self.outcome}")


@dataclass(frozen=True)
class UnexpectedChange:
    """An artifact that moved without an approved edit asking it to."""

    artifact: str
    kind: str
    related_to_change: bool
    reason: str
    introduces_contradiction: bool = False

    def rendered(self) -> str:
        relation = ("related to the approved change"
                    if self.related_to_change else
                    "NOT related to the approved change")
        contradiction = ("; it contradicts the approved value"
                         if self.introduces_contradiction else "")
        return f"{self.artifact}  {self.kind}  {relation}{contradiction}"


@dataclass
class VerificationResult:
    """What is true now, why, and what is still open.

    `alignment` is a P6 `AlignmentState` -- the same four values, not a
    parallel vocabulary. A result carrying ALIGNED with a non-empty
    `unresolved` is a contradiction in terms, and `__post_init__` refuses
    to construct one.
    """

    changeset_id: str
    snapshot_before: str
    snapshot_after: str
    alignment: AlignmentState
    edits: tuple = ()
    unexpected: tuple = ()
    dimensions: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)
    blocked_by: list = field(default_factory=list)
    reason: str = ""
    notes: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if (self.alignment is AlignmentState.ALIGNED
                and (self.unresolved or self.blocked_by)):
            raise CannotVerify(
                f"ALIGNED was reported with {len(self.unresolved)} unresolved "
                f"item(s) and {len(self.blocked_by)} blocker(s); a clean "
                f"verdict that carries open problems is the one result "
                f"nobody would check")

    @property
    def passed(self) -> bool:
        """True only for ALIGNED. Nothing else may reach VERIFIED."""
        return self.alignment is AlignmentState.ALIGNED

    @property
    def missing(self) -> tuple:
        return tuple(e for e in self.edits if e.outcome == MISSING)

    @property
    def diverged(self) -> tuple:
        return tuple(e for e in self.edits if e.outcome == DIVERGED)

    @property
    def reverse_drift(self) -> tuple:
        return tuple(u for u in self.unexpected if u.kind == REVERSE_DRIFT)

    @property
    def dimension_summary(self) -> tuple:
        return tuple((name, self.dimensions.get(name, "UNKNOWN"))
                     for name in DIMENSIONS)


# --- reading the world -------------------------------------------------------


class _Unreadable:
    """Sentinel: the artifact is there and this verifier cannot parse it."""

    def __init__(self, why: str) -> None:
        self.why = why


def _read(snapshot, artifact, parse):
    """The parsed artifact, `None` if absent, `_Unreadable` if unparseable.

    Three outcomes rather than two, because "not in the snapshot" and
    "there but unreadable" lead a person to different places: the first is
    an apply that did not happen, the second is a missing adapter.
    """
    try:
        raw = snapshot.read(artifact)
    except KeyError:
        return None
    try:
        return parse(artifact, raw)
    except Exception as exc:
        return _Unreadable(f"{type(exc).__name__}: {exc}")


def check_edits(snapshot_after, edits, *, parse, read_at) -> tuple:
    """Did each approved edit actually land? Read from the artifact.

    `parse` and `read_at` are injected rather than imported: the artifact's
    format and its locator grammar belong to the adapter layer, and a
    verifier that knew about YAML would be a verifier that silently skipped
    everything else.
    """
    outcomes = []
    for edit in sorted(edits, key=lambda e: (e.artifact, e.path)):
        document = _read(snapshot_after, edit.artifact, parse)
        if document is None:
            outcomes.append(EditOutcome(
                artifact=edit.artifact, path=edit.path,
                expected=edit.proposed, actual=None, outcome=MISSING,
                detail="the artifact is not present in the snapshot"))
            continue
        if isinstance(document, _Unreadable):
            outcomes.append(EditOutcome(
                artifact=edit.artifact, path=edit.path,
                expected=edit.proposed, actual=None, outcome=UNREADABLE,
                detail=(f"no reader for this artifact, so whether the change "
                        f"landed cannot be established ({document.why})")))
            continue
        try:
            actual = read_at(document, edit.path)
        except Exception as exc:  # the locator no longer resolves
            outcomes.append(EditOutcome(
                artifact=edit.artifact, path=edit.path,
                expected=edit.proposed, actual=None, outcome=DIVERGED,
                detail=f"the location no longer exists: {exc}"))
            continue

        if actual == edit.proposed:
            outcome, detail = APPLIED, ""
        elif actual == edit.expected_current:
            outcome, detail = MISSING, "the artifact still holds the old value"
        else:
            outcome = DIVERGED
            detail = ("the artifact holds neither the old value nor the "
                      "approved one")
        outcomes.append(EditOutcome(
            artifact=edit.artifact, path=edit.path, expected=edit.proposed,
            actual=actual, outcome=outcome, detail=detail))
    return tuple(outcomes)


def check_unexpected(snapshot_before, snapshot_after, *, expected_artifacts,
                     approved_values=None, parse=None, read_at=None,
                     paths_by_artifact=None) -> tuple:
    """Artifacts that moved with no approved edit asking them to.

    Two kinds, and they are not the same finding:

    `UNEXPECTED`     something changed during this apply that the ChangeSet
                     did not authorise
    `REVERSE_DRIFT`  an artifact the change DOES cover now holds a value
                     that is neither the old nor the approved one -- i.e.
                     somebody edited it outside the approval path

    Whether a change is "related" is decided by whether the artifact is in
    the approved set, not by resemblance. A verifier that decided relatedness
    by similarity would absorb an out-of-band edit into the approved change
    the moment it touched the same file, which is the one thing it must not do.
    """
    expected = set(expected_artifacts)
    moved = set(snapshot_after.diff_from(snapshot_before))
    findings = []

    for artifact in sorted(moved - expected):
        findings.append(UnexpectedChange(
            artifact=artifact, kind=UNEXPECTED, related_to_change=False,
            reason=("this artifact changed during the apply and no approved "
                    "edit names it")))

    # Reverse drift: inside the approved set, but not at the approved value.
    if approved_values and parse and read_at:
        for artifact, expected_value in sorted(approved_values.items()):
            document = _read(snapshot_after, artifact, parse)
            if document is None or isinstance(document, _Unreadable):
                continue
            for path in (paths_by_artifact or {}).get(artifact, ()):
                try:
                    actual = read_at(document, path)
                except Exception:
                    continue
                if actual != expected_value:
                    findings.append(UnexpectedChange(
                        artifact=artifact, kind=REVERSE_DRIFT,
                        related_to_change=True,
                        reason=(f"{path} holds {actual!r}; the approved value "
                                f"is {expected_value!r}, and no ChangeSet "
                                f"authorised the difference"),
                        introduces_contradiction=True))
    return tuple(findings)


# --- the regression guard ----------------------------------------------------

#: The five conditions, each of which alone prevents VERIFIED. Named in one
#: place so the check and the test read the same list, and so adding a
#: sixth cannot be done without noticing.
BLOCKERS = ("tests_failing", "evidence_stale", "intent_conflict_open",
            "artifact_stale", "approval_evidence_missing")


def blockers_for(*, tests_failing=(), stale_evidence=(), open_conflicts=(),
                 stale_artifacts=(), changeset=None) -> list:
    """Everything that forbids VERIFIED, with the reason for each.

    The fifth condition -- `approval_evidence_missing` -- closes a subtle
    hole: a ChangeSet can be applied through a legitimate path and then have
    its approval record lost or corrupted. Verification re-checks it rather
    than trusting the recorded state, because "this object says it was
    approved" is a declared fact and this module does not accept those.
    """
    found = []
    if tests_failing:
        found.append((
            "tests_failing",
            f"{len(tests_failing)} test(s) assert the old behaviour and are "
            f"now red: " + ", ".join(sorted(tests_failing)[:3])))
    if stale_evidence:
        found.append((
            "evidence_stale",
            f"{len(stale_evidence)} quoted line(s) no longer match the "
            f"artifact they were taken from"))
    if open_conflicts:
        found.append((
            "intent_conflict_open",
            f"{len(open_conflicts)} conflict(s) between what was asked for "
            f"and what the system states remain open"))
    if stale_artifacts:
        found.append((
            "artifact_stale",
            f"{len(stale_artifacts)} required artifact(s) still state the "
            f"old value: " + ", ".join(sorted(stale_artifacts)[:3])))

    approval = getattr(changeset, "approval", None) if changeset else None
    if changeset is not None:
        if approval is None or not getattr(approval, "is_approval", False):
            found.append((
                "approval_evidence_missing",
                "no valid approval record is attached to this ChangeSet at "
                "verification time"))
        elif approval.proposal_version != changeset.proposal_version:
            found.append((
                "approval_evidence_missing",
                f"the attached approval is for proposal version "
                f"{approval.proposal_version}, and this is "
                f"{changeset.proposal_version}"))
    return found


# --- the verdict -------------------------------------------------------------


def verify(*, changeset, snapshot_before, snapshot_after, parse, read_at,
           tests_failing=(), stale_evidence=(), open_conflicts=(),
           dimensions=None, expected_artifacts=None,
           approved_values=None, paths_by_artifact=None) -> VerificationResult:
    """Compare what was approved against what the artifacts now say.

    Deterministic: same snapshots, same ChangeSet, same result. Nothing here
    reads a clock, generates an id, or asks a model for a verdict.
    """
    if snapshot_before.snapshot_id == snapshot_after.snapshot_id:
        raise CannotVerify(
            "the before and after snapshots are the same; there is nothing "
            "to verify, and reporting a pass would be reporting on a change "
            "that never happened")

    edits = tuple(changeset.system_edits())
    outcomes = check_edits(snapshot_after, edits, parse=parse, read_at=read_at)

    covered = set(expected_artifacts if expected_artifacts is not None
                  else {e.artifact for e in edits})
    unexpected = check_unexpected(
        snapshot_before, snapshot_after, expected_artifacts=covered,
        approved_values=approved_values, parse=parse, read_at=read_at,
        paths_by_artifact=paths_by_artifact)

    stale_artifacts = sorted({o.artifact for o in outcomes
                              if o.outcome == MISSING})
    unreadable = sorted({o.artifact for o in outcomes
                         if o.outcome == UNREADABLE})
    blocked = blockers_for(
        tests_failing=tests_failing, stale_evidence=stale_evidence,
        open_conflicts=open_conflicts, stale_artifacts=stale_artifacts,
        changeset=changeset)

    unresolved: list = []
    for outcome in outcomes:
        if not outcome.landed:
            unresolved.append(outcome.rendered())
    for finding in unexpected:
        unresolved.append(finding.rendered())

    alignment, reason = _decide(outcomes, unexpected, blocked)

    result = VerificationResult(
        changeset_id=changeset.changeset_id,
        snapshot_before=snapshot_before.snapshot_id,
        snapshot_after=snapshot_after.snapshot_id,
        alignment=alignment,
        edits=outcomes,
        unexpected=unexpected,
        dimensions=dict(dimensions or _dimensions_from(outcomes, unexpected)),
        unresolved=unresolved,
        blocked_by=[name for name, _ in blocked],
        reason=reason)

    for _, detail in blocked:
        result.notes.append(detail)
    if not edits:
        result.notes.append(
            "this ChangeSet had no system edits; everything it asked for is "
            "carried out by a person and cannot be verified from artifacts")
    return result


def _decide(outcomes, unexpected, blocked) -> tuple:
    """The four P6 alignment values, and why this one.

    Reported in severity order so that a result carrying several problems
    names the one a person should act on first.
    """
    if any(u.kind == REVERSE_DRIFT for u in unexpected):
        return (AlignmentState.CONFLICT,
                "a value changed outside the approval path and now "
                "contradicts what was approved")
    if any(o.outcome == DIVERGED for o in outcomes):
        return (AlignmentState.CONFLICT,
                "an artifact holds neither the old value nor the approved "
                "one, so no ordering explains it")
    if any(o.outcome == MISSING for o in outcomes):
        return (AlignmentState.DRIFT,
                "part of the approved change did not land; the system is "
                "inconsistent with itself")
    if any(o.outcome == UNREADABLE for o in outcomes):
        # Not a pass, and not a conflict either: nothing was observed to
        # disagree. Reporting UNKNOWN keeps "nobody could check" distinct
        # from "the check found a problem" (P6 freeze, item 3).
        return (AlignmentState.UNKNOWN,
                "an artifact this change touches could not be read, so "
                "whether the change landed there is unestablished")
    if blocked:
        return (AlignmentState.DRIFT,
                "; ".join(detail for _, detail in blocked))
    if unexpected:
        return (AlignmentState.DRIFT,
                "every approved edit landed, but something else changed too")
    return (AlignmentState.ALIGNED,
            "every approved edit landed, nothing else moved, and no "
            "condition blocks verification")


def _dimensions_from(outcomes, unexpected) -> dict:
    """A default reading of the six, from the edits alone.

    Deliberately coarse and deliberately overridable. A caller that has run
    the real reconciliation passes it in; this is what a caller that has not
    gets, and it never reports a dimension as OK on no information.
    """
    from core.change_intelligence.impact import kind_of

    by_dimension = {
        "workflow_config": "workflow",
        "software": "software",
        "spreadsheet": "data",
        "document": "documents",
        "test": "tests",
    }
    dimensions = {name: "UNKNOWN" for name in DIMENSIONS}
    for outcome in outcomes:
        dimension = by_dimension.get(kind_of(outcome.artifact))
        if dimension is None:
            continue
        if outcome.outcome == UNREADABLE:
            dimensions[dimension] = "UNKNOWN"
        elif not outcome.landed:
            dimensions[dimension] = "DRIFT"
        elif dimensions[dimension] == "UNKNOWN":
            dimensions[dimension] = "OK"
    for finding in unexpected:
        dimension = by_dimension.get(kind_of(finding.artifact))
        if dimension:
            dimensions[dimension] = "DRIFT"
    return dimensions


# --- what a person does next -------------------------------------------------

#: The two things a person may do about drift. Neither is taken
#: automatically, and there is no third option that means "the system
#: decided". P8 prepares; a person chooses.
LEGITIMISE = "create an intent that makes this the approved value"
REVERT = "propose a change that restores the approved value"


@dataclass(frozen=True)
class NextAction:
    """A proposal for a person, never an action taken."""

    finding: str
    artifact: str
    options: tuple
    evidence: str

    def rendered(self) -> str:
        return (f"{self.artifact}: {self.finding}\n    "
                + "\n    ".join(f"- {option}" for option in self.options))


def next_actions(result: VerificationResult) -> tuple:
    """What could be done. Deliberately plural, and deliberately not done.

    Returning one option would be a recommendation, and a recommendation
    about whether an out-of-band edit is acceptable is a business decision
    wearing a technical costume.
    """
    actions = []
    for finding in result.reverse_drift:
        actions.append(NextAction(
            finding="changed outside the approval path",
            artifact=finding.artifact,
            options=(LEGITIMISE, REVERT),
            evidence=finding.reason))
    for outcome in result.missing:
        actions.append(NextAction(
            finding="an approved change did not land here",
            artifact=outcome.artifact,
            options=("re-apply the approved edit to this artifact",
                     "withdraw this part of the ChangeSet and re-review"),
            evidence=outcome.rendered()))
    for finding in result.unexpected:
        if finding.kind == UNEXPECTED:
            actions.append(NextAction(
                finding="changed although nothing approved it",
                artifact=finding.artifact,
                options=(LEGITIMISE, REVERT),
                evidence=finding.reason))
    return tuple(actions)


def render(result: VerificationResult) -> str:
    """Deterministic from the same result."""
    out = [f"VERIFICATION   {result.changeset_id}   "
           f"{result.alignment.value}",
           f"  {result.snapshot_before[:12]} -> {result.snapshot_after[:12]}",
           "", "  " + result.reason, ""]

    out.append("  DIMENSIONS")
    for name, state in result.dimension_summary:
        out.append(f"    {name:<12} {state}")

    if result.edits:
        out += ["", "  APPROVED EDITS"]
        for outcome in result.edits:
            out.append(f"    {outcome.rendered()}")
            if outcome.detail:
                out.append(f"        {outcome.detail}")

    if result.unexpected:
        out += ["", "  CHANGES NOBODY APPROVED"]
        for finding in result.unexpected:
            out.append(f"    {finding.rendered()}")
            out.append(f"        {finding.reason}")

    if result.blocked_by:
        out += ["", "  VERIFICATION BLOCKED BY"]
        for name, note in zip(result.blocked_by, result.notes):
            out.append(f"    {name}: {note}")

    actions = next_actions(result)
    if actions:
        out += ["", "  A PERSON DECIDES"]
        for action in actions:
            out.append("    " + action.rendered().replace("\n", "\n    "))

    return "\n".join(out)


__all__ = [
    "APPLIED",
    "BLOCKERS",
    "CannotVerify",
    "DIMENSIONS",
    "DIVERGED",
    "EditOutcome",
    "LEGITIMISE",
    "MISSING",
    "NextAction",
    "OUTCOMES",
    "REVERSE_DRIFT",
    "REVERT",
    "UNEXPECTED",
    "UNREADABLE",
    "UnexpectedChange",
    "VerificationResult",
    "VerificationWrites",
    "blockers_for",
    "check_edits",
    "check_unexpected",
    "next_actions",
    "render",
    "verify",
]
