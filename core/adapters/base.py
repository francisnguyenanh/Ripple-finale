"""Adapter interface — plan Step 0.4, A2 §2.

This is a PUBLIC contract: it must not change once a third adapter exists.
`Iterator` return types are load-bearing, not stylistic — `-> list[...]`
would force holding the entire parse result in memory.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from core.kernel.serialize import canonical_dumps


@dataclass(frozen=True)
class FileRef:
    path: str  # project-relative
    size: int
    encoding: str = "utf-8"


@dataclass(frozen=True)
class Provenance:
    file: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    doc: Optional[str] = None
    doc_hash: Optional[str] = None
    sheet: Optional[str] = None
    row: Optional[int] = None
    method: str = "parser"
    quote: Optional[str] = None
    quote_hash: Optional[str] = None
    snapshot_id: Optional[str] = None  # required when derived from source

    def __post_init__(self) -> None:
        has_source_loc = self.file is not None and self.line_start is not None
        has_doc_loc = self.doc is not None and self.sheet is not None and self.row is not None
        if not has_source_loc and not has_doc_loc:
            raise ValueError(
                "Provenance requires either (file,line_start) or (doc,sheet,row)"
            )
        if has_source_loc and self.snapshot_id is None:
            raise ValueError("Provenance from source requires snapshot_id (A2 §2.4)")


@dataclass(frozen=True)
class IRRecord:
    kind: str  # "entity" | "relation"
    id: str
    payload: dict[str, Any]
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True)
class SourceSnapshot:
    """A2 §2.3 — the ground every evidence reference is timestamped against."""

    snapshot_id: str
    created_at: str
    files: dict[str, dict]  # path -> {sha256, size, line_count, encoding}

    @staticmethod
    def build(files: dict[str, dict], created_at: str) -> "SourceSnapshot":
        # snapshot_id = sha256 of the sorted file list, so it is independent
        # of discovery order across workers.
        sorted_files = {k: files[k] for k in sorted(files)}
        digest = hashlib.sha256(canonical_dumps(sorted_files).encode("utf-8")).hexdigest()
        snapshot_id = f"sha256:{digest}"
        return SourceSnapshot(snapshot_id=snapshot_id, created_at=created_at, files=sorted_files)


class SourceAdapter(ABC):
    name: str
    languages: list[str]

    @abstractmethod
    def discover(self, paths: list[str], options: dict) -> Iterator[FileRef]:
        ...

    @abstractmethod
    def parse(self, file_ref: FileRef, **kwargs: Any) -> Iterator[IRRecord]:
        ...


class DocAdapter(ABC):
    name: str
    formats: list[str]

    @abstractmethod
    def extract(self, file_ref: FileRef, targets: list[str]) -> Iterator[IRRecord]:
        ...
