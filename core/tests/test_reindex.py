import pytest

from core.kernel.reindex import Chunk, OrphanChunkError, find_chunks_to_reindex, reindex


def test_orphan_chunk_rejected():
    with pytest.raises(OrphanChunkError):
        Chunk(chunk_id="C1", derived_from=[], embedding_ref="e1")


def test_only_chunks_derived_from_changed_km_are_reembedded():
    chunks = [
        Chunk("C1", ["BR-1@3"], "e1"),
        Chunk("C2", ["BR-2@1"], "e2"),
        Chunk("C3", ["BR-1@3", "BR-5@2"], "e3"),
    ]
    to_reindex = find_chunks_to_reindex(chunks, changed_km_ids={"BR-1"})
    assert to_reindex == ["C1", "C3"]


def test_unrelated_chunks_keep_their_embedding_ref():
    chunks = [Chunk("C1", ["BR-1@3"], "e1"), Chunk("C2", ["BR-2@1"], "e2")]
    new_chunks, reembedded = reindex(chunks, {"BR-1"}, embed_fn=lambda cid: f"new-{cid}")
    by_id = {c.chunk_id: c for c in new_chunks}
    assert by_id["C1"].embedding_ref == "new-C1"
    assert by_id["C2"].embedding_ref == "e2"  # untouched
    assert reembedded == ["C1"]
