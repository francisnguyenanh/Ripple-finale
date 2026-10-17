# -*- coding: utf-8 -*-
"""Two judges at once, and a reset that stops at one session's edge.

The demo will be open to more than one person at a time, and the failure
mode is quiet: A's edit appears in B's screen, both see plausible numbers,
and neither has any reason to doubt them. Nothing raises. So isolation is
asserted from the outside, through the public API only -- the same path a
judge uses, not an internal helper that might isolate differently.

Reset has the symmetrical failure: it works, and it also clears something
it should not have reached. Every reset test below checks a second session
afterwards.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from app.api.router import Api, Request
from app.api.sessions import SessionRegistry, UnknownSession
from core.change_intelligence.session import InMemoryBaseline
from core.change_intelligence.shadow_change import baseline_fingerprint
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
WORKFLOW = "workflow/approval_routes.yaml"
LOCATOR = "approval_routes[3].condition.value"


@pytest.fixture(scope="module")
def artifacts():
    return build_baseline(SEED)["artifacts"]


@pytest.fixture
def api(artifacts):
    return Api(sessions=SessionRegistry(
        baseline=InMemoryBaseline.from_artifacts(artifacts)))


def _new(api):
    return api.call(Request(operation="session.create",
                            timestamp="t")).data["session_id"]


def _edit(api, session, value):
    return api.call(Request(
        operation="scenario.external_edit", session_id=session,
        payload={"artifact": WORKFLOW, "locator": LOCATOR, "value": value}))


def _threshold(api, session):
    raw = api.sessions.get(session).overlay.read(WORKFLOW)
    document = yaml.safe_load(raw)
    return document["approval_routes"][3]["condition"]["value"]


def _changed(api, session):
    return api.call(Request(operation="state.observe",
                            session_id=session)).data["changed_in_session"]


# --- isolation ---------------------------------------------------------------


def test_a_mutation_in_a_does_not_appear_in_b(api):
    a, b = _new(api), _new(api)
    original = _threshold(api, b)

    _edit(api, a, 300_000)

    assert _threshold(api, a) == 300_000
    assert _threshold(api, b) == original


def test_b_sees_no_changed_artifacts_after_a_edits(api):
    a, b = _new(api), _new(api)
    _edit(api, a, 300_000)
    assert _changed(api, a) == [WORKFLOW]
    assert _changed(api, b) == []


def test_three_concurrent_sessions_hold_three_different_values(api):
    sessions = [_new(api) for _ in range(3)]
    for index, session in enumerate(sessions, start=1):
        _edit(api, session, index * 100_000)
    assert [_threshold(api, s) for s in sessions] == [100_000, 200_000, 300_000]


def test_each_session_has_its_own_snapshot_chain(api):
    a, b = _new(api), _new(api)
    _edit(api, a, 300_000)
    first = api.call(Request(operation="state.observe",
                             session_id=a)).data["snapshot_id"]
    second = api.call(Request(operation="state.observe",
                              session_id=b)).data["snapshot_id"]
    assert first != second


def test_each_session_has_its_own_lineage(api):
    a, b = _new(api), _new(api)
    _edit(api, a, 300_000)
    for session in (a, b):
        assert api.call(Request(operation="lineage.show",
                                session_id=session)).data["version"] == 0
    assert api.sessions.get(a).lineage is not api.sessions.get(b).lineage


def test_ids_minted_in_one_session_are_scoped_to_it(api):
    a, b = _new(api), _new(api)
    from tests.intent_support import confirm_intent
    confirm_intent(api, a)
    intent = api.sessions.get(a).listing("intent")[0]
    assert intent.startswith(a + ":")
    assert not api.sessions.get(b).owns(intent)


def test_the_baseline_is_untouched_by_every_session(api, artifacts):
    before = baseline_fingerprint(artifacts=artifacts)
    for value in (100_000, 200_000, 300_000):
        _edit(api, _new(api), value)
    baseline = api.sessions.baseline
    after = baseline_fingerprint(
        artifacts={n: baseline.read(n) for n in baseline.names()})
    assert after == before


# --- reset -------------------------------------------------------------------


def test_mutate_inspect_reset_inspect(api):
    session = _new(api)
    original = _threshold(api, session)

    _edit(api, session, 300_000)
    assert _threshold(api, session) == 300_000

    api.call(Request(operation="session.reset", session_id=session))
    assert _threshold(api, session) == original
    assert _changed(api, session) == []


def test_reset_a_leaves_b_untouched(api):
    a, b = _new(api), _new(api)
    _edit(api, a, 300_000)
    _edit(api, b, 250_000)

    api.call(Request(operation="session.reset", session_id=a))

    assert _threshold(api, b) == 250_000
    assert _changed(api, b) == [WORKFLOW]


def test_reset_clears_transient_work(api):
    session = _new(api)
    from tests.intent_support import confirm_intent
    confirm_intent(api, session)
    assert api.sessions.get(session).listing("intent")

    api.call(Request(operation="session.reset", session_id=session))
    assert api.sessions.get(session).listing("intent") == ()


def test_reset_returns_the_lineage_to_the_baseline(api):
    session = _new(api)
    judge = api.sessions.get(session)
    judge.lineage.record_verified(
        snapshot_id="SNAP-1", parent_snapshot_id=None, changeset_id="CHG-1",
        actor="qa", timestamp="t")
    assert judge.lineage.version == 1

    api.call(Request(operation="session.reset", session_id=session))
    assert api.sessions.get(session).lineage.version == 0


def test_reset_does_not_remove_the_session(api):
    session = _new(api)
    api.call(Request(operation="session.reset", session_id=session))
    assert api.call(Request(operation="state.observe",
                            session_id=session)).ok


def test_reset_preserves_the_baseline(api, artifacts):
    before = baseline_fingerprint(artifacts=artifacts)
    session = _new(api)
    _edit(api, session, 300_000)
    api.call(Request(operation="session.reset", session_id=session))
    baseline = api.sessions.baseline
    assert baseline_fingerprint(
        artifacts={n: baseline.read(n) for n in baseline.names()}) == before


def test_reset_deletes_no_directory():
    """It operates inside the session boundary, not on the dataset."""
    import ast
    import inspect
    import textwrap

    from app.api import sessions as module

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(module.SessionRegistry.reset)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            for banned in ("rmtree", "unlink", "remove", "rmdir"):
                assert banned not in name, (
                    f"reset calls {name}; it must clear one overlay, not "
                    f"remove files")


def test_reset_of_an_unknown_session_is_refused(api):
    reply = api.call(Request(operation="session.reset",
                             session_id="SESSION-9999"))
    assert not reply.ok and reply.code == "unknown_session"


def test_resetting_twice_is_harmless(api):
    session = _new(api)
    _edit(api, session, 300_000)
    api.call(Request(operation="session.reset", session_id=session))
    reply = api.call(Request(operation="session.reset", session_id=session))
    assert reply.ok and reply.data["changed_artifacts"] == []


def test_a_session_can_be_used_again_after_reset(api):
    session = _new(api)
    _edit(api, session, 300_000)
    api.call(Request(operation="session.reset", session_id=session))
    reply = _edit(api, session, 250_000)
    assert reply.ok and _threshold(api, session) == 250_000


def test_mutate_a_mutate_b_reset_a_verify_b(api):
    """The brief's exact sequence."""
    a, b = _new(api), _new(api)
    _edit(api, a, 300_000)
    _edit(api, b, 250_000)
    api.call(Request(operation="session.reset", session_id=a))

    assert _threshold(api, b) == 250_000
    assert _changed(api, a) == []


# --- the registry ------------------------------------------------------------


def test_an_unknown_session_is_never_created_on_demand(api):
    with pytest.raises(UnknownSession):
        api.sessions.get("SESSION-9999")
    assert api.sessions.count() == 0


def test_session_ids_are_deterministic_across_two_runs(artifacts):
    def run():
        registry = SessionRegistry(
            baseline=InMemoryBaseline.from_artifacts(artifacts))
        return [registry.create().session_id for _ in range(3)]

    assert run() == run(), (
        "session ids differ between runs; replay cannot reproduce a scenario "
        "whose identifiers are random")


def test_the_registry_is_not_reachable_from_a_session(api):
    session = api.sessions.get(_new(api))
    for name in vars(session):
        assert not isinstance(getattr(session, name), SessionRegistry)
