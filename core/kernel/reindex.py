"""Incremental re-index — plan Step 6.6, A2 §6.

Every chunk carries `derived_from: [km_id@version]` from the moment it's
created. Without that, any KM change forces a full re-index — the most
common mistake in RAG systems, and the plan requires this field to exist
even before vectors are turned on at all.
"""

from __future__ import annotations

from dataclasses import dataclass


class OrphanChunkError(Exception):
    pass


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    derived_from: list[str]  # ["BR-1@3", ...]
    embedding_ref: str

    def __post_init__(self):
        if not self.derived_from:
            raise OrphanChunkError(f"chunk {self.chunk_id!r} has empty derived_from — not allowed")


def _base_id(ref: str) -> str:
    return ref.split("@", 1)[0]


def find_chunks_to_reindex(chunks: list[Chunk], changed_km_ids: set[str]) -> list[str]:
    return sorted(
        c.chunk_id for c in chunks
        if any(_base_id(ref) in changed_km_ids for ref in c.derived_from)
    )


def reindex(chunks: list[Chunk], changed_km_ids: set[str], embed_fn) -> tuple[list[Chunk], list[str]]:
    """Returns (new_chunk_list, ids_reembedded). Chunks untouched by the
    change keep their existing `embedding_ref` — never touched, never
    re-embedded.
    """
    to_reindex = set(find_chunks_to_reindex(chunks, changed_km_ids))
    new_chunks = []
    reembedded = []
    for c in chunks:
        if c.chunk_id in to_reindex:
            new_chunks.append(Chunk(c.chunk_id, c.derived_from, embed_fn(c.chunk_id)))
            reembedded.append(c.chunk_id)
        else:
            new_chunks.append(c)
    return new_chunks, sorted(reembedded)
