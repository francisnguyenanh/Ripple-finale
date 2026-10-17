# -*- coding: utf-8 -*-
"""In-memory implementations of the baseline / overlay / session contracts.

Promoted from `spike/` after ADR-002 and ADR-003. The spike proved the
SEMANTICS; this is the same design written to the P1 interfaces so the rest
of the product can build on it, with a cloud adapter swapped in behind the
same contracts.

Nothing here opens a file or names a database. That is checked, not
promised: `test_q5_change_intelligence_never_names_infrastructure` greps
this package for `open(`, `Path(` and `sqlite3`. The bytes arrive from tier
1 (ADR-001) and are handed in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from core.change_intelligence.storage import (
    BaselineImmutableError,
    SessionScopeError,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class InMemoryBaseline:
    """READ-ONLY. The immutable half.

    Frozen, and its `write` always refuses. A baseline with no write method
    would be a promise; a baseline whose write says no is a fact a test can
    check, and a mutation test calls it.
    """

    baseline_id: str
    _artifacts: dict[str, bytes]

    @classmethod
    def from_artifacts(cls, artifacts: dict[str, bytes]) -> "InMemoryBaseline":
        manifest = {name: _sha256(body) for name, body in sorted(artifacts.items())}
        return cls(
            baseline_id=_sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")),
            _artifacts=dict(artifacts),
        )

    def read(self, path: str) -> bytes:
        if path not in self._artifacts:
            raise KeyError(f"{path!r} is not in baseline {self.baseline_id[:12]}")
        return self._artifacts[path]

    def names(self) -> list[str]:
        return sorted(self._artifacts)

    def manifest(self) -> dict[str, str]:
        return {name: _sha256(body) for name, body in sorted(self._artifacts.items())}

    def write(self, path: str, content: bytes) -> None:
        raise BaselineImmutableError(
            f"the baseline is immutable; {path!r} belongs in a session overlay "
            f"(baseline_id={self.baseline_id[:12]})"
        )


@dataclass(frozen=True)
class SessionSnapshot:
    """An addressable, comparable view of one session at one moment."""

    snapshot_id: str
    session_id: str
    baseline_id: str
    parent_snapshot_id: str | None
    overlay_manifest: dict[str, str]
    _overlay: "InMemoryOverlay" = field(repr=False, compare=False)

    def read(self, path: str) -> bytes:
        return self._overlay.read_at(self, path)

    def list_changed(self) -> list[str]:
        """Artifacts whose CONTENT differs from the baseline.

        Not the overlay's key list: writing an artifact back to its original
        bytes leaves a row behind but changes nothing, and reporting it as
        changed would hand P8 a phantom every time a no-op is applied.
        """
        return sorted(
            name for name in self.overlay_manifest
            if self._overlay.baseline.manifest().get(name) != self.overlay_manifest[name]
        )

    def diff_from(self, other: "SessionSnapshot") -> list[str]:
        if other.baseline_id != self.baseline_id:
            raise SessionScopeError(
                "cannot diff snapshots built on different baselines: "
                f"{self.baseline_id[:12]} vs {other.baseline_id[:12]}"
            )
        names = set(self._overlay.baseline.names()) | set(self.overlay_manifest) \
            | set(other.overlay_manifest)
        return sorted(n for n in names if self._read_or_none(n) != other._read_or_none(n))

    def _read_or_none(self, path: str) -> bytes | None:
        try:
            return self.read(path)
        except KeyError:
            return None


class InMemoryOverlay:
    """read-write. Session-local changes over an immutable baseline.

    Append-only: every write mints a new snapshot and no row is ever
    updated, which is what keeps an older snapshot readable and makes
    `diff_from` mean something.
    """

    def __init__(self, baseline: InMemoryBaseline, session_id: str) -> None:
        if not session_id:
            raise SessionScopeError("an overlay without a session_id cannot be isolated (I18)")
        self.baseline = baseline
        self.session_id = session_id
        self._blobs: dict[str, bytes] = {}
        self._snapshots: dict[str, dict[str, str]] = {}
        self._head = self._mint({}, parent=None)

    def _mint(self, manifest: dict[str, str], *, parent: str | None) -> SessionSnapshot:
        snapshot_id = _sha256(json.dumps(
            {"session": self.session_id,
             "baseline": self.baseline.baseline_id,
             "overlay": manifest},
            sort_keys=True).encode("utf-8"))
        self._snapshots[snapshot_id] = dict(manifest)
        return SessionSnapshot(
            snapshot_id=snapshot_id, session_id=self.session_id,
            baseline_id=self.baseline.baseline_id, parent_snapshot_id=parent,
            overlay_manifest=dict(manifest), _overlay=self,
        )

    @property
    def head(self) -> SessionSnapshot:
        return self._head

    def write(self, path: str, content: bytes) -> SessionSnapshot:
        """The single write path, for one artifact. `write_all({path: content})`."""
        return self.write_all({path: content})

    def _stage_blob(self, path: str, content: bytes) -> str:
        """Compute the blob for one artifact. Touches no shared state.

        Split out so a write-layer failure on one artifact (the failure
        mode review 14's atomicity probe injects) can be reproduced and
        caught before `write_all` commits anything.
        """
        return _sha256(content)

    def write_all(self, contents: dict[str, bytes]) -> SessionSnapshot:
        """Write every artifact in `contents` into exactly ONE new snapshot.

        Every blob is staged first, into LOCAL variables only. `self._blobs`
        and `self._head` -- the only state a reader can observe -- are
        mutated in the last two lines, after the loop above has completed
        without raising. A `_stage_blob` failure on any artifact, including
        the last one, leaves both untouched: there is nothing to roll back
        because nothing was written yet.
        """
        staged_blobs: dict[str, bytes] = {}
        manifest = dict(self._head.overlay_manifest)
        for path, content in contents.items():
            blob = self._stage_blob(path, content)
            staged_blobs[blob] = content
            manifest[path] = blob

        # The single commit point. Nothing above this line touched
        # self._blobs or self._head.
        self._blobs.update(staged_blobs)
        self._head = self._mint(manifest, parent=self._head.snapshot_id)
        return self._head

    def clear(self) -> SessionSnapshot:
        """Reset. Nothing can be lost that the baseline did not already have."""
        self._head = self._mint({}, parent=self._head.snapshot_id)
        return self._head

    def read_at(self, snapshot: SessionSnapshot, path: str) -> bytes:
        if snapshot.session_id != self.session_id:
            raise SessionScopeError(
                f"snapshot belongs to {snapshot.session_id!r}, not {self.session_id!r} (I18)"
            )
        blob = snapshot.overlay_manifest.get(path)
        if blob is not None:
            return self._blobs[blob]
        return self.baseline.read(path)

    def read(self, path: str) -> bytes:
        return self.read_at(self._head, path)


class InMemorySessionStore:
    """Judge session isolation. One baseline, many sessions (I18)."""

    def __init__(self, baseline: InMemoryBaseline) -> None:
        self.baseline = baseline
        self._sessions: dict[str, InMemoryOverlay] = {}
        self._counter = 0

    def create(self, baseline_id: str | None = None) -> str:
        if baseline_id is not None and baseline_id != self.baseline.baseline_id:
            raise SessionScopeError(
                f"this deployment serves baseline {self.baseline.baseline_id[:12]}, "
                f"not {baseline_id[:12]}"
            )
        self._counter += 1
        session_id = f"SESSION-{self._counter:04d}"
        self._sessions[session_id] = InMemoryOverlay(self.baseline, session_id)
        return session_id

    def overlay(self, session_id: str) -> InMemoryOverlay:
        if session_id not in self._sessions:
            raise SessionScopeError(f"no such session {session_id!r}")
        return self._sessions[session_id]

    def get(self, session_id: str) -> dict | None:
        overlay = self._sessions.get(session_id)
        if overlay is None:
            return None
        return {"session_id": session_id, "baseline_id": self.baseline.baseline_id,
                "snapshot_id": overlay.head.snapshot_id,
                "changed": overlay.head.list_changed()}

    def reset(self, session_id: str) -> str:
        """Drop the overlay and hand back a FRESH session id.

        A new id rather than a cleared one, deliberately: anything still
        holding the old id is holding a session that no longer exists, and
        should find that out rather than quietly write into a reset world.
        """
        self.overlay(session_id)
        del self._sessions[session_id]
        return self.create()
