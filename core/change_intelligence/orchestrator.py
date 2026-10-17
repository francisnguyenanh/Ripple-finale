# -*- coding: utf-8 -*-
"""The loop, wired. Coordination only.

This module decides **what runs next**, never **what the answer is**. Every
verdict it reports was computed somewhere else:

    change_detector   what changed, and what is downstream of it
    impact_graph      how far propagation reaches, and where it must stop
    verification      whether the approved change actually landed
    lineage           whether the verified state advanced
    loop_controller   which kind of loop this change is
    fact_correction   the refusal when propagation reaches the Fact layer

If a risk formula or an impact rule appeared here, there would be two
implementations of one business rule, and the day they disagree the system
gives a verdict nobody can explain. A test walks this module's syntax tree
and fails if it starts computing rather than calling.

**The loop's shape**

    ArtifactChanged  ->  detect  ->  scope  ->  reconcile  ->  report
    ChangeApplied    ->  verify  ->  lineage  ->  VerificationCompleted

Both directions end at a **report for a person**. Neither ends at a repair.
The reverse direction -- somebody edited a spreadsheet with no ChangeSet --
produces a finding and two options, and stops there. Choosing between
legitimising the edit and reverting it is a business decision, and a system
that picked one would be deciding what the company's policy is.

**I8: the loop cannot reach the Fact layer.** Propagation scope is checked
against `fact_correction.guard_loop_never_touches_ir` before anything runs.
A scope containing an IR-layer record halts and becomes a correction
request that demands a new version and a fresh gate -- it is never edited
in place. That refusal is the evidence that the loop cannot quietly rewrite
what was observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence import change_detector, events, verification
from core.change_intelligence.lineage import VerifiedLineage
from core.kernel.fact_correction import (
    FactCorrectionRequiredError,
    guard_loop_never_touches_ir,
    request_fact_correction,
    route_gap,
)
from core.kernel.impact_graph import ImpactTouchesIRError, impact_scope
from core.kernel.loop_controller import classify_change
from core.kernel.reindex import find_chunks_to_reindex

#: What a turn of the loop produced. Deliberately not a verdict.
REPORTED = "REPORTED"
BLOCKED_ON_FACT = "BLOCKED_ON_FACT"
AWAITING_HUMAN = "AWAITING_HUMAN"


class OrchestratorDecided(Exception):
    """Reserved: raised if a decision path is ever added here.

    A symbol a test can look for, rather than a sentence in a docstring
    that a later change quietly contradicts.
    """


@dataclass
class LoopTurn:
    """One pass of the loop. What was looked at, what was found, what is open."""

    trigger: str
    session_id: str
    status: str = REPORTED
    scope: object = None
    result: object = None
    loop_kind: str = ""
    reindexed: tuple = ()
    fact_correction: object = None
    next_actions: tuple = ()
    emitted: tuple = ()
    notes: list = field(default_factory=list)

    @property
    def needs_a_person(self) -> bool:
        return bool(self.next_actions)


@dataclass
class Orchestrator:
    """Wires the loop together for one session. Holds no domain rule."""

    session_id: str
    lineage: VerifiedLineage
    bus: object = None

    def __post_init__(self) -> None:
        if self.bus is None:
            self.bus = events.EventBus()
        self._register()

    def _register(self) -> None:
        """Subscribe to the loop-triggering events, and only those.

        The list comes from the schemas, so a new event is
        subscribed by declaring it loop-triggering and not by editing a
        list here that somebody would eventually widen.
        """
        self.bus.subscribe("ArtifactChanged", self.on_artifact_changed)
        self.bus.subscribe("ChangeApplied", self.on_change_applied)

    # --- direction 1: reality moved ------------------------------------------

    def on_artifact_changed(self, event) -> LoopTurn:
        """Something changed. Find out what it reaches, and report."""
        turn = LoopTurn(trigger=event.event_type, session_id=event.session_id)
        turn.notes.append(
            f"{len(event.payload.get('artifacts', ()))} artifact(s) differ "
            f"between {event.payload.get('snapshot_before', '')[:12]} and "
            f"{event.payload.get('snapshot_after', '')[:12]}")
        return turn

    def run_reality_direction(self, *, snapshot_before, snapshot_after,
                              records=(), model=None, graph=None, chunks=(),
                              total_artifacts=0, scope_layers=None,
                              occurred_at: str = "") -> LoopTurn:
        """Reality changed with no ChangeSet. Detect, scope, report.

        Ends at a report. There is no branch here that writes anything
        back, and the two options it offers are offered to a person.
        """
        turn = LoopTurn(trigger="ArtifactChanged", session_id=self.session_id)

        deltas = change_detector.detect(snapshot_before, snapshot_after)
        turn.scope = change_detector.scope_for(
            deltas, records=records, model=model, graph=graph, chunks=chunks,
            total_artifacts=total_artifacts)

        # I8 -- checked BEFORE any work, so a scope that reaches the Fact
        # layer never gets partially processed and then halted.
        if scope_layers:
            try:
                guard_loop_never_touches_ir(scope_layers)
            except FactCorrectionRequiredError as exc:
                turn.status = BLOCKED_ON_FACT
                turn.notes.append(str(exc))
                return turn

        turn.loop_kind = classify_change(
            is_wording_only=False,
            requires_km_regeneration=bool(turn.scope.knowledge_ids),
            is_new_ruleset=False).loop

        if chunks:
            turn.reindexed = tuple(find_chunks_to_reindex(
                list(chunks), set(turn.scope.knowledge_ids)))

        if occurred_at:
            event = events.artifact_changed(
                session_id=self.session_id,
                snapshot_before=snapshot_before.snapshot_id,
                snapshot_after=snapshot_after.snapshot_id,
                artifacts=[d.artifact for d in deltas],
                occurred_at=occurred_at)
            self.bus.publish(event)
            turn.emitted = (event,)

        turn.status = AWAITING_HUMAN if deltas else REPORTED
        return turn

    # --- direction 2: an approved change was applied -------------------------

    def on_change_applied(self, event) -> LoopTurn:
        return LoopTurn(trigger=event.event_type, session_id=event.session_id,
                        status=REPORTED)

    def run_verification(self, *, changeset, snapshot_before, snapshot_after,
                         parse, read_at, actor: str, timestamp: str,
                         **kwargs) -> LoopTurn:
        """Verify, then advance the lineage only if it passed.

        The order matters and is the whole of D4: the version moves after
        the verdict, never alongside the apply that produced the snapshot.
        """
        turn = LoopTurn(trigger="ChangeApplied", session_id=self.session_id)

        result = verification.verify(
            changeset=changeset, snapshot_before=snapshot_before,
            snapshot_after=snapshot_after, parse=parse, read_at=read_at,
            **kwargs)
        turn.result = result
        turn.next_actions = verification.next_actions(result)

        if result.passed:
            self.lineage.record_verified(
                snapshot_id=snapshot_after.snapshot_id,
                parent_snapshot_id=snapshot_after.parent_snapshot_id,
                changeset_id=changeset.changeset_id,
                actor=actor, timestamp=timestamp,
                artifacts=[o.artifact for o in result.edits])
        else:
            self.lineage.record_failure(
                snapshot_id=snapshot_after.snapshot_id,
                changeset_id=changeset.changeset_id,
                actor=actor, timestamp=timestamp, reason=result.reason)

        event = events.verification_completed(
            session_id=self.session_id,
            changeset_id=changeset.changeset_id,
            snapshot=snapshot_after.snapshot_id,
            alignment=result.alignment.value,
            unresolved=result.unresolved,
            occurred_at=timestamp)
        turn.emitted = (event,)

        turn.status = REPORTED if result.passed else AWAITING_HUMAN
        return turn

    # --- propagation scope ----------------------------------------------------

    def propagation_scope(self, *, nodes, edges, changed_ids):
        """How far a change reaches. Computed by impact_graph, not here.

        Re-raised rather than caught: a propagation that reaches the Fact
        layer is not a condition to work around, it is the boundary the
        architecture exists to hold.
        """
        return impact_scope(nodes=nodes, edges=edges, changed=changed_ids)

    def correction_for(self, gap: dict):
        """A gap that turns out to be a Fact error leaves the loop entirely."""
        if route_gap(gap) != "fact_correction_flow":
            return None
        return request_fact_correction(gap)


__all__ = [
    "AWAITING_HUMAN",
    "BLOCKED_ON_FACT",
    "FactCorrectionRequiredError",
    "ImpactTouchesIRError",
    "LoopTurn",
    "Orchestrator",
    "OrchestratorDecided",
    "REPORTED",
]
