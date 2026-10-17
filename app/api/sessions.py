# -*- coding: utf-8 -*-
"""Judge sessions: isolated, resettable, and holding no cross-references.

Three properties, each of which is a test rather than an intention:

**Isolation.** Two people trying the demo at the same time must not see each
other's work. Enforced by the overlay (I18) and, above it, by never handing
out an object that can reach another session. A handler receives a
`JudgeSession` and has no route from it to the registry, so there is no
`session.store.sessions[other_id]` to reach along -- a class of bug that
looks like a typo and behaves like a data leak.

**Reset within the boundary.** Reset returns ONE session to the baseline. It
does not delete a directory, does not touch the baseline, and does not
reach another session. Implemented as "clear this overlay and drop this
session's transient state", because the baseline is immutable and therefore
already the known-good state -- there is nothing to restore FROM anywhere
else.

**Nothing durable is assumed.** The registry is in-process, and that is
stated rather than hidden: on Cloud Run this is the per-instance cache in
front of the state store, not the truth. `docs/contracts/P9_FROZEN.md`
records which parts are migration-sensitive.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from core.change_intelligence.lineage import VerifiedLineage
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay


class UnknownSession(Exception):
    """No such session. Never a new one created helpfully."""


class CrossSessionAccess(Exception):
    """One session reached for another's object."""


@dataclass
class JudgeSession:
    """One person's isolated view. Holds no reference to any other session."""

    session_id: str
    overlay: InMemoryOverlay
    lineage: VerifiedLineage
    baseline_id: str
    created_at: str = ""
    #: Transient work: intents, shadows, prepared changesets, decisions.
    #: Cleared by reset, because a proposal built against a state that no
    #: longer exists is a proposal about nothing.
    work: dict = field(default_factory=dict)
    #: The version this in-process copy was last
    #: known to match in the durable backend. A multi-instance backend's
    #: `persist_session` compares against it under a row lock and refuses
    #: (`StaleSessionVersion`) rather than overwriting a write that landed
    #: from another instance in between. 0 for a session with no backend,
    #: or one freshly created.
    durable_version: int = 0
    orchestrator: object = None

    @property
    def snapshot(self):
        return self.overlay.head

    def put(self, kind: str, key: str, value) -> None:
        self.work.setdefault(kind, {})[key] = value

    def fetch(self, kind: str, key: str):
        found = self.work.get(kind, {}).get(key)
        if found is None:
            raise KeyError(f"{kind}:{key}")
        return found

    def listing(self, kind: str) -> tuple:
        return tuple(sorted(self.work.get(kind, {})))

    def owns(self, identifier: str) -> bool:
        """Does this session's id prefix the object's?

        Every id minted here carries the session, so a reference from
        another session is refused by inspection rather than by lookup --
        a lookup that misses looks the same as an object that was deleted.
        """
        return identifier.startswith(self.session_id + ":")

    def mint(self, kind: str, seed: str) -> str:
        """A deterministic id, scoped to this session.

        Deterministic so replay reproduces it; scoped so it cannot be
        mistaken for another session's.
        """
        digest = hashlib.sha256(
            f"{self.session_id}|{kind}|{seed}".encode("utf-8")).hexdigest()
        return f"{self.session_id}:{kind}:{digest[:10]}"


@dataclass
class SessionRegistry:
    """Creates, finds and resets sessions. The only thing that holds them all.

    Deliberately not passed to handlers. A handler that could reach the
    registry could reach every session, and the isolation would then rest on
    every handler author remembering not to.
    """

    baseline: InMemoryBaseline
    _sessions: dict = field(default_factory=dict)
    _counter: int = 0
    backend: object = None

    def create(self, *, created_at: str = "", request_id: str = "") -> JudgeSession:
        """A new session from the immutable baseline.

        The id is sequential rather than random: replay has to reproduce it,
        and a random id would make two runs of the same scenario differ in
        a field that means nothing.
        """
        if self.backend is not None and request_id:
            session_id = "SESSION-" + hashlib.sha256(request_id.encode()).hexdigest()[:32]
            existing = self.backend.fetch_session(session_id, self.baseline)
            if existing is not None:
                self._sessions[session_id] = existing
                return existing
        elif self.backend is not None and hasattr(self.backend, "mint_session_id"):
            session_id = self.backend.mint_session_id()
        else:
            self._counter += 1
            session_id = f"SESSION-{self._counter:04d}"
        session = JudgeSession(
            session_id=session_id,
            overlay=InMemoryOverlay(self.baseline, session_id),
            lineage=VerifiedLineage(session_id=session_id,
                                    baseline_id=self.baseline.baseline_id),
            baseline_id=self.baseline.baseline_id,
            created_at=created_at)
        self._sessions[session_id] = session
        if self.backend is not None and hasattr(self.backend, "persist_session"):
            try:
                session.durable_version = self.backend.persist_session(session, expected_version=0)
            except Exception:
                self._sessions.pop(session_id, None)
                # Handles a concurrent create or a lost acknowledgement after
                # commit. The stable id is scoped to the caller's request.
                if request_id:
                    existing = self.backend.fetch_session(session_id, self.baseline)
                    if existing is not None:
                        self._sessions[session_id] = existing
                        return existing
                raise
        return session

    def get(self, session_id: str) -> JudgeSession:
        """Fetch, or refuse.

        Never creates one on demand. An unknown session id that quietly
        produced a working session would turn a typo into a silent new
        world, and the person would be looking at a different demo from the
        one they thought they were in.
        """
        if session_id not in self._sessions:
            if self.backend is not None and hasattr(self.backend, "fetch_session"):
                loaded = self.backend.fetch_session(session_id, self.baseline)
                if loaded is not None:
                    self._sessions[session_id] = loaded
                    return loaded
            raise UnknownSession(
                f"{session_id!r} does not exist. Sessions are created "
                f"explicitly and are never opened on demand")
        return self._sessions[session_id]

    def reset(self, session_id: str) -> JudgeSession:
        """Return ONE session to the baseline.

        The overlay is cleared and the transient work is dropped. The
        baseline is not touched, no directory is removed, and no other
        session is reachable from here -- reset takes an id and operates on
        exactly the session that id names.

        The verified lineage is rebuilt rather than kept: it describes how
        far a verified state advanced, and after a reset that state is the
        baseline again. Keeping the old entries would claim the session had
        confirmed a state it no longer holds.
        """
        session = self.get(session_id)
        session.overlay.clear()
        epoch = session.work.get("reset_epoch", 0) + 1
        preserved = {key: session.work[key] for key in ("judge_usage", "handled_request") if key in session.work}
        session.work.clear()
        session.work.update(preserved)
        session.work["reset_epoch"] = epoch
        session.orchestrator = None
        session.lineage = VerifiedLineage(
            session_id=session_id, baseline_id=self.baseline.baseline_id)
        return session

    def count(self) -> int:
        if self.backend is not None and hasattr(self.backend, "count"):
            return self.backend.count()
        return len(self._sessions)

    def ids(self) -> tuple:
        if self.backend is not None and hasattr(self.backend, "ids"):
            return self.backend.ids()
        return tuple(sorted(self._sessions))


__all__ = [
    "CrossSessionAccess",
    "JudgeSession",
    "SessionRegistry",
    "UnknownSession",
]
