"""D0 v0.3 tests: guard reentrancy, scope contract, merge limit record. Synthetic fixtures only."""
import socket
import threading

import pytest

from core.change_intelligence.v2.source_ingest_adapters import parse_yaml
from infra.worker.rule_identity import scope_key
from pitch.d0 import extract_from_observations
from pitch.d0.guard import ProviderCallAttempted, active_depth, no_provider_calls


def run(content):
    res = parse_yaml("a.yaml", content)
    return extract_from_observations(res.observations, artifact_id="a1", source_revision_id="r1", blob_hash="h")


def test_scope_keeps_list_and_bool_values():
    out = run(b"p:\n  max_fee: 5\n  region: [JP, VN]\n  tier: true\n")
    c = out["candidates"][0]
    assert c["scope"]["region"] == ["JP", "VN"] and c["scope"]["tier"] is True
    assert scope_key(c["scope"]) is not None  # accepted by the product scope contract


def test_different_list_scopes_do_not_merge():
    out = run(b"a:\n  max_fee: 5\n  region: [JP]\n")
    out2 = run(b"a:\n  max_fee: 5\n  region: [VN]\n")
    assert out["candidates"][0]["scope"] != out2["candidates"][0]["scope"]


def test_guard_non_lifo_overlap_restores_originals():
    orig = (socket.create_connection, socket.socket.connect)
    a, b = no_provider_calls(), no_provider_calls()
    a.__enter__()
    b.__enter__()
    assert active_depth() == 2
    with pytest.raises(ProviderCallAttempted):
        socket.create_connection(("127.0.0.1", 9))
    a.__exit__(None, None, None)
    with pytest.raises(ProviderCallAttempted):  # still guarded while b is open
        socket.create_connection(("127.0.0.1", 9))
    b.__exit__(None, None, None)
    assert active_depth() == 0
    assert (socket.create_connection, socket.socket.connect) == orig


def test_guard_threads_overlap_restore():
    orig = socket.create_connection
    ev, done = threading.Event(), threading.Event()

    def worker():
        with no_provider_calls():
            ev.set()
            done.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    ev.wait(5)
    with no_provider_calls():
        run(b"a:\n  limit: 1\n")
    done.set()
    t.join()
    assert socket.create_connection is orig and active_depth() == 0


def test_guard_exit_without_enter_raises():
    from pitch.d0.guard import _exit
    with pytest.raises(RuntimeError):
        _exit()


def test_merge_same_as_production_first_value_kept():
    out = run(b"a:\n  maxAmount: 1\n  max_amount: 2\n")
    assert len(out["candidates"]) == 1  # documented limit shared with production extraction
