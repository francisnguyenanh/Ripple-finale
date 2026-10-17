import inspect
import typing

from core.adapters.base import DocAdapter, FileRef, IRRecord, Provenance, SourceAdapter, SourceSnapshot


def _return_annotation_is_iterator(fn) -> bool:
    hints = typing.get_type_hints(fn)
    ret = hints.get("return")
    origin = typing.get_origin(ret)
    return origin is collections_abc_Iterator or ret is typing.Iterator


import collections.abc

collections_abc_Iterator = collections.abc.Iterator


def test_source_adapter_signatures_return_iterator_not_list():
    assert _return_annotation_is_iterator(SourceAdapter.discover)
    assert _return_annotation_is_iterator(SourceAdapter.parse)


def test_doc_adapter_signature_returns_iterator_not_list():
    assert _return_annotation_is_iterator(DocAdapter.extract)


def test_no_list_return_type_anywhere_in_module():
    import core.adapters.base as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "-> list[IRRecord]" not in src
    assert "-> List[IRRecord]" not in src


def test_parse_iterator_does_not_materialize_all_records_eagerly():
    # A generator-based fake adapter must be able to yield one record at a
    # time without the caller forcing the whole sequence into memory first.
    produced = []

    def fake_parse(file_ref):
        for i in range(1000):
            rec = IRRecord(
                kind="entity",
                id=f"PGM:{i}",
                payload={},
                provenance=(
                    Provenance(file="f.cbl", line_start=i, line_end=i, snapshot_id="sha256:x"),
                ),
            )
            produced.append(i)
            yield rec

    gen = fake_parse(FileRef(path="f.cbl", size=100))
    first = next(gen)
    assert produced == [0]  # only one record produced so far
    assert first.id == "PGM:0"


def test_provenance_requires_snapshot_id_for_source_location():
    import pytest

    with pytest.raises(ValueError):
        Provenance(file="f.cbl", line_start=1, line_end=1)  # missing snapshot_id


def test_provenance_accepts_doc_location_without_snapshot():
    p = Provenance(doc="spec.xlsx", sheet="Sheet1", row=5)
    assert p.snapshot_id is None


def test_source_snapshot_id_independent_of_discovery_order():
    files_a = {"b.cbl": {"sha256": "h2", "size": 1, "line_count": 1, "encoding": "utf-8"},
               "a.cbl": {"sha256": "h1", "size": 1, "line_count": 1, "encoding": "utf-8"}}
    files_b = {"a.cbl": files_a["a.cbl"], "b.cbl": files_a["b.cbl"]}

    snap_a = SourceSnapshot.build(files_a, created_at="2026-01-01T00:00:00Z")
    snap_b = SourceSnapshot.build(files_b, created_at="2026-01-01T00:00:00Z")
    assert snap_a.snapshot_id == snap_b.snapshot_id
