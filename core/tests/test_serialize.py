import random

from core.kernel.serialize import canonical_dumps, sort_records


def test_canonical_dumps_key_order_independent():
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_dumps(a) == canonical_dumps(b)


def test_sort_records_independent_of_worker_completion_order():
    records = [
        {"type": "program", "id": "PGM:C"},
        {"type": "program", "id": "PGM:A"},
        {"type": "job", "id": "JOB:B"},
    ]
    shuffled = records[:]
    random.Random(42).shuffle(shuffled)

    out1 = sort_records(records, "ir.entities")
    out2 = sort_records(shuffled, "ir.entities")
    assert out1 == out2

    bytes1 = "\n".join(canonical_dumps(r) for r in out1)
    bytes2 = "\n".join(canonical_dumps(r) for r in out2)
    assert bytes1 == bytes2


def test_unregistered_artifact_kind_raises():
    import pytest

    with pytest.raises(KeyError):
        sort_records([{"id": "x"}], "not_a_real_kind")


def test_relations_sort_key_uses_provenance_tiebreak():
    records = [
        {"src": "A", "type": "call", "dst": "B", "provenance": [{"file": "z.cbl", "lines": "1"}]},
        {"src": "A", "type": "call", "dst": "B", "provenance": [{"file": "a.cbl", "lines": "1"}]},
    ]
    out = sort_records(records, "ir.relations")
    assert out[0]["provenance"][0]["file"] == "a.cbl"
