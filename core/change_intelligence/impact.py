# -*- coding: utf-8 -*-
"""What moves if this rule changes, across eleven kinds of thing.

    business rules | processes | software | workflow config | documents
    spreadsheets | APIs | tests | roles | notifications | audit controls

The last three turn a technical report into an operational one:

    roles           who has to approve something they did not before?
    notifications   who starts receiving mail?
    audit controls  which checklist has the old number written on it?

**Precision matters as much as recall.** A system that marks everything as
impacted is useless -- it has moved the reader's problem back to them and
added a list. So every node is tiered, and the tiers are reported
separately:

    DIRECT     the artifact states the rule itself
    INDIRECT   it depends on something that does, through >= 1 edge
    UNKNOWN    not enough information to say either way

**Every node carries a reason and evidence.** A bare list persuades nobody
and cannot be checked. "docs/manager-guide.md" is a filename;
"DOCUMENTS edge to BR-001, and section 3.2 writes the number out" is a
claim a reader can verify in ten seconds, which is the difference between
a report that gets acted on and one that gets closed.

I8: impact walks the Enterprise Graph and the Knowledge Model. It never
touches the Fact layer, and it never writes anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DIRECT = "DIRECT"
INDIRECT = "INDIRECT"
UNKNOWN = "UNKNOWN"

TIERS = (DIRECT, INDIRECT, UNKNOWN)

#: The eleven kinds required. Kept as a tuple so a test can
#: assert the count rather than trusting that nobody quietly dropped the
#: three that are hardest to compute.
OBJECT_KINDS = (
    "business_rule",
    "process",
    "software",
    "workflow_config",
    "document",
    "spreadsheet",
    "api",
    "test",
    "role",
    "notification",
    "audit_control",
)

#: path prefix -> object kind. Explicit, for the same reason ingestion's
#: table is explicit: a classifier that guesses will one day guess wrong
#: quietly.
_KIND_BY_PREFIX = (
    ("workflow/", "workflow_config"),
    ("data/", "spreadsheet"),
    ("tests/", "test"),
    ("docs/", "document"),
    ("application/", "software"),
)

#: Kinds whose artifact carries the value the system ACTS on. Changing the
#: rule means changing these; everything else describes or verifies them and
#: follows afterwards.
#:
#: This is the DIRECT/INDIRECT line, and it is not "does the artifact
#: mention the rule". Every document in the seed mentions the threshold, and
#: marking all of them DIRECT gave a direct-tier precision of 0.375 against
#: the seed's own ground truth -- a reader told nine things are direct stops
#: believing the word. A document that states the number still has to be
#: updated; it is not where the change is made.
_OPERATIVE_KINDS = frozenset({"workflow_config", "spreadsheet", "software"})


@dataclass(frozen=True)
class ImpactNode:
    """One thing that would move, why, and where to check it."""

    identifier: str
    kind: str
    tier: str
    reason: str
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.reason:
            raise IncompleteImpact(
                f"{self.identifier} was marked {self.tier} with no reason; a "
                f"bare list persuades nobody and cannot be checked")
        if self.tier not in TIERS:
            raise IncompleteImpact(f"{self.tier!r} is not one of {TIERS}")
        if self.kind not in OBJECT_KINDS:
            raise IncompleteImpact(f"{self.kind!r} is not one of the eleven kinds")


class IncompleteImpact(Exception):
    """An impact node missing what makes it usable.

    FAILURE INJECTION target: a node without a reason be
    caught by validation rather than rendered as an empty cell.
    """


@dataclass
class ImpactResult:
    target: str
    nodes: tuple[ImpactNode, ...] = ()
    notes: list = field(default_factory=list)

    def tier(self, tier: str) -> tuple:
        return tuple(n for n in self.nodes if n.tier == tier)

    def kind(self, kind: str) -> tuple:
        return tuple(n for n in self.nodes if n.kind == kind)

    @property
    def kinds_touched(self) -> tuple:
        return tuple(sorted({n.kind for n in self.nodes}))

    @property
    def artifacts(self) -> tuple:
        """Identifiers that are real files, for comparison with ground truth."""
        return tuple(sorted({n.identifier for n in self.nodes if "/" in n.identifier}))

    def has_unknown(self) -> bool:
        """Read by risk assessment: an impact set with UNKNOWN in it can
        never be reported as low risk."""
        return bool(self.tier(UNKNOWN))


def kind_of(identifier: str) -> str:
    for prefix, kind in _KIND_BY_PREFIX:
        if identifier.startswith(prefix):
            return kind
    return "software"


def analyse(target: str, *, model=None, graph=None, config=None,
            records=None, roles_by_outcome=None, notification_targets=None,
            audit_controls=None) -> ImpactResult:
    """Everything that would move, tiered and explained.

    Each source of nodes is optional and its ABSENCE is recorded as a note
    rather than silently producing a smaller answer. An impact set that is
    small because nothing was supplied looks exactly like one that is small
    because little is affected, and those are opposite facts.
    """
    result = ImpactResult(target=target)
    nodes: list[ImpactNode] = []
    direct_artifacts: set = set()

    # --- DIRECT: artifacts that state the rule ---------------------------
    if model is not None:
        for record in model.records:
            if record.semantic_id != target:
                continue
            nodes.append(ImpactNode(
                identifier=f"rule:{target}", kind="business_rule", tier=DIRECT,
                reason=f"{target} is the rule being changed",
                evidence=record.id))
            # One node per ARTIFACT, not per representation. A document
            # that states the rule in three sections is one thing to
            # update, and listing it three times makes the impact set look
            # bigger than the work is.
            seen_here: dict = {}
            for rep in record.representations:
                seen_here.setdefault(rep.artifact, rep)

            for artifact, rep in sorted(seen_here.items()):
                direct_artifacts.add(artifact)
                quote = ""
                for ref in record.evidence:
                    if ref.artifact == artifact:
                        quote = f"{ref.artifact}:{ref.locator}"
                        break
                kind = kind_of(artifact)
                operative = kind in _OPERATIVE_KINDS
                nodes.append(ImpactNode(
                    identifier=artifact,
                    kind=kind,
                    tier=DIRECT if operative else INDIRECT,
                    reason=(
                        (f"carries the value {target} is read from"
                         if operative else
                         f"describes {target} and would state the old value")
                        + (f"; states {rep.value}" if rep.value is not None
                           else "; states the rule without a value")),
                    evidence=quote or artifact))
    else:
        result.notes.append(
            "no Knowledge Model supplied: DIRECT representations were not "
            "computed, so this result is incomplete rather than small")

    # --- INDIRECT: reachable through the graph ---------------------------
    if graph is not None:
        scope = graph.impact_scope(target)
        for identifier in sorted(scope.get("indirect", [])):
            artifact = identifier.split(":", 1)[-1] if ":" in identifier else identifier
            if artifact in direct_artifacts:
                continue
            nodes.append(ImpactNode(
                identifier=artifact,
                kind=kind_of(artifact),
                tier=INDIRECT,
                reason=f"reachable from {target} through the enterprise graph",
                evidence=identifier))
    else:
        result.notes.append(
            "no enterprise graph supplied: INDIRECT dependents were not "
            "computed")

    # --- tests -------------------------------------------------------------
    #
    # From the assertion records, matched on the VALUES the rule holds --
    # the same route the evidence view uses. A test that asserts the
    # threshold has to move with it, and a test file is not reachable
    # through the rule edges because a test documents nothing (it
    # corroborates), so nothing else would have found it.
    if records is not None and model is not None:
        wanted = {rep.value for record in model.records
                  if record.semantic_id == target
                  for rep in record.representations if rep.value is not None}
        by_file: dict = {}
        for record in records:
            if getattr(record, "kind", "") != "test_assertion":
                continue
            hits = [v for v in record.payload.get("values", []) if v in wanted]
            if not hits:
                continue
            provenance = record.provenance[0] if record.provenance else None
            path = getattr(provenance, "file", "") or ""
            by_file.setdefault(path, []).append(record.payload["test_name"])
        for path, names in sorted(by_file.items()):
            nodes.append(ImpactNode(
                identifier=path, kind="test", tier=INDIRECT,
                reason=(f"{len(names)} assertion(s) state a value this change "
                        f"moves, so they go red: {', '.join(sorted(set(names))[:3])}"),
                evidence=path))
    else:
        result.notes.append(
            "no ingestion records supplied: affected tests were not computed")

    # --- roles ------------------------------------------------------------
    if roles_by_outcome:
        for outcome, role in sorted(roles_by_outcome.items()):
            if not role:
                continue
            nodes.append(ImpactNode(
                identifier=role, kind="role", tier=INDIRECT,
                reason=(f"requests newly routed to {outcome} land with this "
                        f"role, so its workload changes"),
                evidence=f"outcome {outcome}"))
    else:
        result.notes.append("no role mapping supplied: affected roles unknown")
        nodes.append(ImpactNode(
            identifier="roles", kind="role", tier=UNKNOWN,
            reason="no mapping from decision outcome to role was supplied",
            evidence=""))

    # --- notifications ----------------------------------------------------
    if notification_targets:
        for outcome, address in sorted(notification_targets.items()):
            nodes.append(ImpactNode(
                identifier=address, kind="notification", tier=INDIRECT,
                reason=f"receives mail for every {outcome} decision",
                evidence=f"outcome {outcome}"))
    else:
        result.notes.append(
            "no notification map supplied: who starts receiving mail is unknown")

    # --- audit controls ----------------------------------------------------
    if audit_controls:
        for control, detail in sorted(audit_controls.items()):
            nodes.append(ImpactNode(
                identifier=control, kind="audit_control", tier=INDIRECT,
                reason=detail, evidence=control))
    else:
        result.notes.append(
            "no audit controls supplied: affected controls unknown")

    result.nodes = tuple(sorted(nodes, key=lambda n: (TIERS.index(n.tier),
                                                      n.kind, n.identifier)))
    return result


def score(result: ImpactResult, *, expected_direct=(), expected_indirect=(),
          not_impacted=()) -> dict:
    """Precision and recall against hand-written ground truth.

    `unnecessary` is an added metric and the one that keeps
    this honest: an artifact the ground truth says is NOT impacted, which
    the analysis flagged anyway. A system that marks everything scores
    perfect recall and is useless.
    """
    found = set(result.artifacts)
    expected = set(expected_direct) | set(expected_indirect)

    true_positive = found & expected
    unnecessary = found & set(not_impacted)
    missed = expected - found

    found_direct = {n.identifier for n in result.tier(DIRECT) if "/" in n.identifier}
    direct_correct = found_direct & set(expected_direct)

    return {
        "expected": len(expected),
        "found": len(found),
        "recall": len(true_positive) / len(expected) if expected else 1.0,
        "precision": len(true_positive) / len(found) if found else 1.0,
        "unnecessary": sorted(unnecessary),
        "unnecessary_rate": len(unnecessary) / len(found) if found else 0.0,
        "missed": sorted(missed),
        "direct_precision": (len(direct_correct) / len(found_direct)
                             if found_direct else 1.0),
    }


__all__ = [
    "DIRECT",
    "INDIRECT",
    "IncompleteImpact",
    "ImpactNode",
    "ImpactResult",
    "OBJECT_KINDS",
    "TIERS",
    "UNKNOWN",
    "analyse",
    "kind_of",
    "score",
]
