"""Canonical serialize + sort — A2 §11.

Every module MUST go through this helper instead of calling json.dumps
directly. This is what makes "re-run -> byte identical" testable at all
once work is split across parallel workers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable

# Canonical sort key per artifact type — A2 §11. Applied at aggregation
# time only; workers never sort their own partial output.
SORT_KEYS: dict[str, Callable[[dict], tuple]] = {
    "ir.entities": lambda r: (r["type"], r["id"]),
    "ir.relations": lambda r: (
        r["src"],
        r["type"],
        r["dst"],
        (r.get("provenance") or [{}])[0].get("file", ""),
        (r.get("provenance") or [{}])[0].get("lines", ""),
    ),
    "km": lambda r: (r.get("partition_id", ""), r["id"]),
    "findings": lambda r: (r["round"], r["role"], str(r["target"]), r["id"]),
    "partitions": lambda r: (r["partition_id"],),
    "chunks": lambda r: (r["chunk_id"],),
}


import datetime as _dt


class _CanonicalEncoder(json.JSONEncoder):
    """Extend the default encoder with types that arise from real-world data.

    * `datetime` / `date`: Excel date-typed cells (openpyxl returns Python
      datetime objects).  ISO-8601 strings are stable, human-readable, and
      round-trip without loss of information.
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, (_dt.datetime, _dt.date)):
            return obj.isoformat()
        return super().default(obj)


def canonical_dumps(obj: Any) -> str:
    """The ONLY sanctioned way to serialize an artifact to JSON text."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      cls=_CanonicalEncoder)


def sort_records(records: Iterable[dict], artifact_kind: str) -> list[dict]:
    """Apply the canonical sort key for `artifact_kind` (A2 §11 table).

    Raises KeyError if `artifact_kind` is not registered — an unregistered
    artifact kind must not silently fall back to insertion order.
    """
    key_fn = SORT_KEYS[artifact_kind]
    return sorted(records, key=key_fn)


def write_jsonl(path: str, records: Iterable[dict], artifact_kind: str | None = None) -> None:
    """Aggregator-only writer: sorts (if artifact_kind given) then writes
    one canonical JSON object per line. Workers must NOT call this with
    artifact_kind set — they write their own partition-local file unsorted
    and let the aggregator merge + sort.
    """
    rows = list(records)
    if artifact_kind is not None:
        rows = sort_records(rows, artifact_kind)
    # A manifest with zero rows (e.g. no diagram was drawable this
    # run) still needs somewhere to land; without this an empty result
    # was indistinguishable from "the pipeline never got this far",
    # exactly the ambiguity this avoids.
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(canonical_dumps(row))
            f.write("\n")


def read_jsonl(path: str) -> list[dict]:
    """Reads a `.jsonl` artifact back, tolerant of a missing file (an
    empty/not-yet-produced artifact reads as `[]`, not an error) -- the
    read-side counterpart to `write_jsonl`. Several modules (`pipeline_stages`,
    `extraction_yield`) used to each keep their own private copy of this;
    consolidated here so read and write of the same artifact shape live
    in one file.
    """
    import os as _os

    if not _os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
