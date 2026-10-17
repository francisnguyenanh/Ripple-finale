# -*- coding: utf-8 -*-
"""What changed, and what has to be looked at because of it.

If every change re-runs the whole pipeline, this is "run it again", not
continuous intelligence. The claim is only worth making if the work done is
proportional to the change made, and the only honest way to show that is to
count: change one artifact of thirty, and the number reprocessed must be
substantially fewer than thirty. If it is thirty, the scoping does nothing
and the word "scoped" is decoration.

The walk is: artifact -> the records read from it -> the knowledge those
records support -> the chunks derived from that knowledge. Each hop is an
edge that already exists in the Enterprise Graph. Nothing here re-derives
a relationship -- if the graph does not know two things are connected, this
module does not invent the connection, it reports the node as unreachable
so that the gap in the graph is visible rather than papered over.

**Deterministic and unordered-input-safe.** Two runs over the same pair of
snapshots produce the same scope in the same order. Sets are sorted on the
way out, because a scope that comes back in a different order every time
cannot be compared between runs and a replay test would be meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ArtifactDelta:
    """One artifact that differs between two snapshots, and how."""

    artifact: str
    change: str  # "added" | "removed" | "modified"
    before_digest: str = ""
    after_digest: str = ""

    def rendered(self) -> str:
        return f"{self.artifact}  {self.change}"


@dataclass
class Scope:
    """What has to be reprocessed, and what deliberately does not."""

    artifacts: tuple = ()
    records: tuple = ()
    knowledge_ids: tuple = ()
    chunk_ids: tuple = ()
    unreachable: tuple = ()
    total_artifacts: int = 0
    notes: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.artifacts

    @property
    def fraction(self) -> float:
        """How much of the corpus this change reaches. The scoping measure."""
        if not self.total_artifacts:
            return 0.0
        return len(self.artifacts) / self.total_artifacts

    def rendered(self) -> str:
        return (f"{len(self.artifacts)} of {self.total_artifacts} artifact(s), "
                f"{len(self.records)} record(s), "
                f"{len(self.knowledge_ids)} knowledge record(s), "
                f"{len(self.chunk_ids)} chunk(s)")


def detect(snapshot_before, snapshot_after) -> tuple:
    """Artifacts that differ, with which direction they moved.

    Reported as deltas rather than a bare name list: "this file appeared"
    and "this file changed" lead to different work, and a caller handed only
    names has to go and ask again.
    """
    changed = snapshot_after.diff_from(snapshot_before)
    deltas = []
    for artifact in changed:
        before = _digest(snapshot_before, artifact)
        after = _digest(snapshot_after, artifact)
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        else:
            change = "modified"
        deltas.append(ArtifactDelta(
            artifact=artifact, change=change,
            before_digest=before or "", after_digest=after or ""))
    return tuple(deltas)


def _digest(snapshot, artifact) -> str | None:
    manifest = snapshot.overlay_manifest
    if artifact in manifest:
        return manifest[artifact]
    try:
        return snapshot._overlay.baseline.manifest().get(artifact)
    except AttributeError:
        return None


def scope_for(deltas, *, records=(), model=None, graph=None, chunks=(),
              total_artifacts=0) -> Scope:
    """Everything downstream of these artifacts, and nothing else.

    Every input is optional and its absence is a NOTE, not a smaller
    answer. A scope that is small because the graph was not supplied looks
    exactly like a scope that is small because little is affected, and
    those are opposite facts -- the same distinction P6 drew about impact.
    """
    touched = tuple(sorted({d.artifact for d in deltas}))
    scope = Scope(artifacts=touched, total_artifacts=total_artifacts)

    if not touched:
        scope.notes.append("nothing changed between these snapshots")
        return scope

    # --- records read from those artifacts -------------------------------
    if records:
        scope.records = tuple(sorted(
            {getattr(r, "id", "") or f"{r.artifact}:{r.locator}"
             for r in records
             if getattr(r, "artifact", None) in set(touched)}))
    else:
        scope.notes.append(
            "no records supplied: the record layer was not scoped, so this "
            "result is incomplete rather than small")

    # --- knowledge supported by those records -----------------------------
    reached: set = set()
    unreachable: list = []
    if model is not None:
        for record in model.records:
            evidence_artifacts = {ref.artifact for ref in record.evidence}
            if evidence_artifacts & set(touched):
                reached.add(record.id)
        scope.knowledge_ids = tuple(sorted(reached))
    else:
        scope.notes.append("no Knowledge Model supplied: knowledge not scoped")

    # --- anything the graph connects but evidence did not ------------------
    if graph is not None:
        for artifact in touched:
            if not _graph_knows(graph, artifact):
                unreachable.append(artifact)
        scope.unreachable = tuple(sorted(unreachable))
        if unreachable:
            scope.notes.append(
                f"{len(unreachable)} changed artifact(s) have no edge in the "
                f"graph, so nothing downstream of them could be scoped; that "
                f"is a gap in the graph, not an absence of consequence")
    else:
        scope.notes.append("no graph supplied: propagation not followed")

    # --- chunks derived from the reached knowledge -------------------------
    if chunks:
        scope.chunk_ids = tuple(sorted(
            getattr(c, "id", "") for c in chunks
            if _chunk_base(c) in reached))

    return scope


def _graph_knows(graph, artifact) -> bool:
    try:
        nodes = {n.id for n in graph.nodes()}
    except Exception:
        return True
    if artifact in nodes:
        return True
    try:
        return any(artifact in (e.src, e.dst) for e in graph.edges())
    except Exception:
        return True


def _chunk_base(chunk) -> str:
    ref = getattr(chunk, "km_ref", "") or getattr(chunk, "source_ref", "")
    return ref.split("@")[0].split(":")[-1] if ref else ""


def is_scoped(scope: Scope, *, threshold: float = 0.5) -> bool:
    """Did scoping actually do anything?

    A caller asserting this is asserting the product's claim. The default
    threshold is deliberately loose -- the test that matters compares the
    scope to the corpus and fails when they are equal, because equal means
    the walk reached everything and the word "scoped" is decoration.
    """
    return bool(scope.total_artifacts) and scope.fraction < threshold


def render(scope: Scope) -> str:
    out = [f"SCOPE   {scope.rendered()}"]
    for artifact in scope.artifacts:
        out.append(f"  {artifact}")
    for note in scope.notes:
        out.append(f"  note: {note}")
    return "\n".join(out)


__all__ = [
    "ArtifactDelta",
    "Scope",
    "detect",
    "is_scoped",
    "render",
    "scope_for",
]
