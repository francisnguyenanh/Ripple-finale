# -*- coding: utf-8 -*-
"""Build the immutable baseline, and reset by making a new session.

Reset is NOT a rollback. A rollback has to undo things, and undoing is where
state machines go to die: half-applied changes, orphaned overlay rows, a
session that was reset while a simulation was mid-flight.

    BASELINE-001 (immutable)
        +-- SESSION-A  -> shadow changes, decisions
        +-- SESSION-B  -> ...
        +-- SESSION-C  -> ...

Reset = drop this session's overlay and start another one on the same
baseline. Nothing is undone because nothing was ever done TO the baseline.
That is also what lets several judges use one deployment at once without
standing on each other (I18), and it is why this had to be right from the start
rather than retrofitted -- retrofitting it would mean rewriting the
state architecture.

This module is TIER 1 (build time, ADR-001). It reads the filesystem, which
is exactly what tier 1 is allowed to do and tier 2 is not.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

#: The six artifact families, and what belongs to each in this seed. Listed
#: rather than globbed: a baseline that silently absorbs whatever happens to
#: be in the directory is a baseline nobody can reason about.
FAMILY_GLOBS: dict[str, list[str]] = {
    "software": ["application/*.py"],
    "workflow": ["workflow/*.yaml"],
    "docs": ["docs/*.md"],
    "spreadsheet": ["data/*.xlsx"],
    "tests": ["tests/*.py"],
    "history": ["history/transactions.jsonl", "history/changelog.jsonl"],
}

EXCLUDE = {"__init__.py", "generate_corpus.py", "replay.py"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_artifacts(seed_root: str | pathlib.Path) -> dict[str, bytes]:
    """Read every artifact of the six families, keyed by its logical path."""
    root = pathlib.Path(seed_root)
    artifacts: dict[str, bytes] = {}
    for _family, patterns in FAMILY_GLOBS.items():
        for pattern in patterns:
            for path in sorted(root.glob(pattern)):
                if path.name in EXCLUDE or not path.is_file():
                    continue
                artifacts[path.relative_to(root).as_posix()] = path.read_bytes()
    return artifacts


def families_of(artifacts: dict[str, bytes]) -> dict[str, list[str]]:
    """Which family each artifact belongs to -- needed by the adapters and by scope checks."""
    root_to_family = {
        "application": "software", "workflow": "workflow", "docs": "docs",
        "data": "spreadsheet", "tests": "tests", "history": "history",
    }
    out: dict[str, list[str]] = {f: [] for f in FAMILY_GLOBS}
    for name in sorted(artifacts):
        family = root_to_family.get(name.split("/", 1)[0])
        if family:
            out[family].append(name)
    return out


def build_manifest(artifacts: dict[str, bytes]) -> dict[str, str]:
    return {name: _sha256(body) for name, body in sorted(artifacts.items())}


def baseline_id(manifest: dict[str, str]) -> str:
    """Content-addressed, and therefore reproducible.

    The same seed gives the same id on any machine, on any day. That is what
    lets a judge who opens the demo next week see the same baseline as one
    who opened it today -- the id is PINNED in the deployment, never
    discovered by looking for the newest.
    """
    return _sha256(json.dumps(manifest, sort_keys=True).encode("utf-8"))


def build_baseline(seed_root: str | pathlib.Path) -> dict:
    """The tier-1 output: an immutable snapshot, ready to be uploaded."""
    artifacts = collect_artifacts(seed_root)
    manifest = build_manifest(artifacts)
    return {
        "baseline_id": baseline_id(manifest),
        "manifest": manifest,
        "artifacts": artifacts,
        "families": families_of(artifacts),
        "artifact_count": len(artifacts),
    }


def write_baseline(seed_root: str | pathlib.Path, out: str | pathlib.Path) -> dict:
    """Materialise the baseline next to the seed, for the adapters and the deployment.

    Writes the manifest only. The bytes are already on disk at build time
    and go to Cloud Storage; duplicating them here would create
    a second copy that can drift from the first.
    """
    baseline = build_baseline(seed_root)
    path = pathlib.Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"baseline_id": baseline["baseline_id"],
             "artifact_count": baseline["artifact_count"],
             "families": baseline["families"],
             "manifest": baseline["manifest"]},
            indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    return baseline
