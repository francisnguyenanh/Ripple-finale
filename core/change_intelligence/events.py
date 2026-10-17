# -*- coding: utf-8 -*-
"""The six events that drive the loop, dispatched once each.

P1 defined nine domain events. Six of them carry `x-loop-triggering: true`
and are the loop's inputs; three are produced *inside* a turn already in
progress. Subscribing to those three would mean:

    IntentAccepted -> ShadowChangeCreated -> reconcile
                   -> ShadowChangeCreated -> reconcile -> ...

an infinite loop that no unit test would reveal, because it needs a whole
turn to appear. It would surface during a demo.

So the classification is read from **one place** -- the `x-loop-triggering`
flag in each event's JSON Schema -- and never restated here. A list copied
into this module would be a list that drifts, and the drift would be in the
direction of subscribing to more things, because that is the direction
somebody debugging a missed update pushes it.

**Idempotency is by `dedup_key`, not by `event_id`.** An id makes a
redelivery unique; a dedup key makes it the same fact. The two are
different questions and only one of them is about the world: "has this
already been handled" cannot be answered by something designed to be
different every time.

**Dispatch is in process.** Pub/Sub is a declared cut. What has to be true
for the product's claim is that the loop closes, not which broker carries
it, and an in-process bus makes the replay test exact rather than flaky.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Fetching the classification is infrastructure and lives outside this
#: tier (P1 §6 Q5). The classification itself is still declared in exactly
#: one place -- the `x-loop-triggering` flag in each event's schema.
from core.schema.event_registry import internal, known, loop_triggering


class UnknownEvent(Exception):
    """An event type with no schema. The vocabulary is closed on purpose."""


class NotLoopTriggering(Exception):
    """Something tried to subscribe the loop to an internal event."""


@dataclass(frozen=True)
class Event:
    """One thing that happened, with the key that says whether it is new."""

    event_type: str
    dedup_key: str
    session_id: str
    occurred_at: str
    payload: dict = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self) -> None:
        for name in ("event_type", "dedup_key", "session_id", "occurred_at"):
            if not getattr(self, name):
                raise UnknownEvent(
                    f"an event without {name} cannot be deduplicated or "
                    f"attributed")
        if self.event_type not in known():
            raise UnknownEvent(
                f"{self.event_type!r} has no schema; the event vocabulary is "
                f"closed so that a typo becomes an error rather than an event "
                f"nobody subscribes to")

    @property
    def is_loop_triggering(self) -> bool:
        return self.event_type in loop_triggering()


def artifact_changed(*, session_id: str, snapshot_before: str,
                     snapshot_after: str, artifacts, occurred_at: str) -> Event:
    """The reverse-drift trigger.

    The dedup key is built from the two snapshot ids, which are content
    addresses: re-emitting the same transition produces the same key, and
    two genuinely different transitions cannot collide.
    """
    return Event(
        event_type="ArtifactChanged",
        dedup_key=f"ArtifactChanged:{snapshot_before}:{snapshot_after}",
        session_id=session_id, occurred_at=occurred_at,
        payload={"snapshot_before": snapshot_before,
                 "snapshot_after": snapshot_after,
                 "artifacts": sorted(artifacts)})


def change_applied(*, session_id: str, changeset_id: str, new_snapshot: str,
                   occurred_at: str) -> Event:
    return Event(
        event_type="ChangeApplied",
        dedup_key=f"ChangeApplied:{changeset_id}:{new_snapshot}",
        session_id=session_id, occurred_at=occurred_at,
        payload={"changeset_id": changeset_id, "new_snapshot": new_snapshot})


def verification_completed(*, session_id: str, changeset_id: str,
                           snapshot: str, alignment: str, unresolved,
                           occurred_at: str) -> Event:
    return Event(
        event_type="VerificationCompleted",
        dedup_key=f"VerificationCompleted:{changeset_id}:{snapshot}",
        session_id=session_id, occurred_at=occurred_at,
        payload={"changeset_id": changeset_id, "alignment": alignment,
                 "unresolved": list(unresolved)})


@dataclass
class EventBus:
    """In-process dispatch with a dedup ledger.

    The ledger is kept rather than a set of keys alone, because "this was
    already handled, here is what it produced" is the answer a replay needs,
    and recomputing it would make replay a second execution.
    """

    handlers: dict = field(default_factory=dict)
    seen: dict = field(default_factory=dict)
    ledger: list = field(default_factory=list)

    def subscribe(self, event_type: str, handler) -> None:
        """Register a handler. Refuses internal events.

        The refusal is the whole point of this module: it is the one place where
        somebody chasing a missed update would otherwise widen the loop's
        inputs and create a cycle.
        """
        if event_type not in known():
            raise UnknownEvent(event_type)
        if event_type in internal():
            raise NotLoopTriggering(
                f"{event_type} is produced inside a turn already in progress; "
                f"subscribing the loop to it creates a cycle that no unit "
                f"test shows and a demo does")
        self.handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: Event) -> tuple:
        """Dispatch once. A key already seen returns its first result.

        Returns `(results, was_duplicate)`. The caller is told rather than
        left to infer it: "nothing happened because it was a duplicate" and
        "nothing happened because no handler cared" are different facts.
        """
        if event.dedup_key in self.seen:
            return self.seen[event.dedup_key], True

        results = []
        for handler in self.handlers.get(event.event_type, []):
            results.append(handler(event))

        outcome = tuple(results)
        self.seen[event.dedup_key] = outcome
        self.ledger.append(event)
        return outcome, False

    def handled(self) -> tuple:
        """Every event actually dispatched, in order. Duplicates absent."""
        return tuple(self.ledger)

    def count_of(self, event_type: str) -> int:
        return sum(1 for e in self.ledger if e.event_type == event_type)


__all__ = [
    "Event",
    "EventBus",
    "NotLoopTriggering",
    "UnknownEvent",
    "artifact_changed",
    "change_applied",
    "internal",
    "known",
    "loop_triggering",
    "verification_completed",
]
