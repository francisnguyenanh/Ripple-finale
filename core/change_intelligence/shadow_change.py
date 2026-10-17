# -*- coding: utf-8 -*-
"""A hypothetical change, and the isolation it promises.

    "If we lowered the approval threshold from 1,000,000 to 500,000, what
     would change?"

The entire value of this feature rests on one promise: asking the question
changes nothing. So the safety boundary is not a convention -- `ShadowChange`
cannot be constructed without the snapshot it branches from, and the store
refuses to hand a session another session's work.

**`base_snapshot` is mandatory** (I17). A what-if with no recorded starting
point cannot be replayed, and a comparison whose "current" column came from
whatever the world looked like when somebody clicked is not a comparison.

**`session_id` is mandatory** (I18). Two judges running what-ifs at the same
time must not see each other's. That is not a privacy nicety at a
competition -- it is the difference between a demo where the numbers mean
something and one where they drift for reasons nobody can reconstruct.

**`target` is a semantic_id, not free text.** A shadow aimed at "the
approval thing" cannot be tied back to the representations it affects, and
impact analysis would have nothing to walk from.

Nothing here writes to a canonical artifact. `apply_to_config` returns a NEW
config object; the seed's `ProcurementConfig.with_threshold` is a copy
constructor for exactly this reason.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace


class CanonicalWriteRefused(Exception):
    """Something tried to write through a shadow into the real world.

    I17, enforced rather than documented. A shadow store that merely
    "should not" be written through is a shadow store that will be.
    """


class SessionIsolationError(Exception):
    """One session reached for another session's shadow. I18."""


class IncompleteShadow(Exception):
    """A shadow missing the fields that make it replayable."""


@dataclass(frozen=True)
class ShadowChange:
    """One hypothetical change, belonging to one session.

    Frozen. A shadow that could be edited after impact was computed would
    make the impact set describe a question nobody asked.
    """

    shadow_id: str
    base_snapshot: str
    target: str
    current_value: object
    proposed_value: object
    session_id: str
    scenario_id: str = ""
    #: Intent id AND version. The version matters: an intent
    #: revised to v2 is a different request, and a shadow that recorded
    #: only the id would silently claim to answer the new one.
    intent_id: str = ""
    intent_version: int = 0
    impacted_artifacts: tuple = ()
    created_by: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        missing = [name for name in ("shadow_id", "base_snapshot", "target",
                                     "session_id")
                   if not getattr(self, name)]
        if missing:
            raise IncompleteShadow(
                f"a shadow without {missing} cannot be replayed or attributed "
                f"(I17, I18)")
        if self.current_value == self.proposed_value:
            # Not an error -- Scenario H asks exactly this -- but it must be
            # visible, because an impact set of zero is a finding and not a
            # failure to compute one.
            object.__setattr__(self, "scenario_id",
                               self.scenario_id or "no-op")

    @property
    def is_noop(self) -> bool:
        return self.current_value == self.proposed_value

    def fingerprint(self) -> str:
        """Content address of the QUESTION, so two runs can be compared.

        Deliberately excludes `impacted_artifacts`: the fingerprint
        identifies what was asked, not what was computed from it, and a
        determinism test needs to state that the same question came back
        with the same answer.
        """
        payload = {
            "base_snapshot": self.base_snapshot,
            "target": self.target,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def apply_to_config(self, config, *, rule_id: str):
        """A NEW config with the proposed value. The original is untouched.

        The seed's config is frozen and `with_threshold` returns a copy, so
        this cannot mutate by accident -- but the call is here, in the
        shadow, so that "what does the shadow world look like" has one
        answer and not one per caller.
        """
        return config.with_threshold(rule_id, self.proposed_value)


@dataclass
class ShadowStore:
    """Shadows, kept apart by session.

    An in-memory store on purpose. A shadow is a question somebody is
    holding in their head for a minute, and persisting it would raise the
    question of when it stops being true -- the base snapshot moves, and a
    stored shadow silently becomes an answer about a world that no longer
    exists.
    """

    _by_session: dict = field(default_factory=dict)

    def put(self, shadow: ShadowChange) -> ShadowChange:
        self._by_session.setdefault(shadow.session_id, {})[shadow.shadow_id] = shadow
        return shadow

    def get(self, session_id: str, shadow_id: str) -> ShadowChange:
        """Fetch, refusing across sessions.

        Raises rather than returning None when the shadow exists but
        belongs to somebody else. "Not found" and "not yours" are different
        facts, and collapsing them hides a real isolation failure behind a
        plausible empty result.
        """
        mine = self._by_session.get(session_id, {})
        if shadow_id in mine:
            return mine[shadow_id]

        for other_session, shadows in self._by_session.items():
            if shadow_id in shadows and other_session != session_id:
                raise SessionIsolationError(
                    f"{shadow_id} belongs to another session; a what-if is "
                    f"private to the person asking it (I18)")
        raise KeyError(shadow_id)

    def list_for(self, session_id: str) -> tuple:
        return tuple(sorted(self._by_session.get(session_id, {}).values(),
                            key=lambda s: s.shadow_id))

    def sessions(self) -> tuple:
        return tuple(sorted(self._by_session))

    def discard(self, session_id: str, shadow_id: str) -> None:
        """A what-if the person walked away from. Removed, not archived."""
        self.get(session_id, shadow_id)
        del self._by_session[session_id][shadow_id]


# --- the safety measurement ----------------------------------------------------


def baseline_fingerprint(*, artifacts: dict, graph=None, model=None) -> str:
    """One hash over everything a shadow must not touch.

    Artifacts, graph shape and Knowledge Model together, because a shadow
    that left the files alone and quietly edited the KM would pass a test
    that hashed only the files -- and the KM is where the conclusions live.
    """
    payload = {
        "artifacts": {
            path: hashlib.sha256(raw).hexdigest()
            for path, raw in sorted(artifacts.items())
        },
    }

    if graph is not None:
        payload["graph"] = {
            "nodes": sorted(n.id for n in graph.nodes()),
            "edges": sorted(f"{e.src}|{e.type}|{e.dst}" for e in graph.edges()),
        }

    if model is not None:
        payload["knowledge"] = sorted(
            f"{r.id}|{r.semantic_id}|{r.alignment}|"
            f"{','.join(str(rep.value) for rep in r.representations)}"
            for r in model.records)

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class ReadOnlyView:
    """A snapshot wrapper that raises on every write.

    FAILURE INJECTION lives against this: the isolation test does not only
    assert the hash is unchanged afterwards, it asserts that an attempt to
    write RAISES. A guarantee that holds because nobody happened to try is
    not a guarantee.
    """

    def __init__(self, snapshot):
        self._snapshot = snapshot

    @property
    def snapshot_id(self) -> str:
        return self._snapshot.snapshot_id

    def read(self, path: str) -> bytes:
        return self._snapshot.read(path)

    def names(self) -> list:
        return list(getattr(self._snapshot, "names", lambda: [])())

    def write(self, *args, **kwargs):
        raise CanonicalWriteRefused(
            "a shadow may not write to the baseline; the whole feature is "
            "the promise that asking changes nothing (I17)")

    def write_all(self, *args, **kwargs):
        """Same refusal as `write` (N2, SessionArtifactOverlay now declares
        both). Without this, calling `write_all` on a shadow view raised a
        bare `AttributeError` instead of the meaningful
        `CanonicalWriteRefused` -- correct by accident, not by contract."""
        raise CanonicalWriteRefused(
            "a shadow may not write to the baseline; the whole feature is "
            "the promise that asking changes nothing (I17)")

    put = write
    delete = write
    __setitem__ = write


__all__ = [
    "CanonicalWriteRefused",
    "IncompleteShadow",
    "ReadOnlyView",
    "SessionIsolationError",
    "ShadowChange",
    "ShadowStore",
    "baseline_fingerprint",
]
