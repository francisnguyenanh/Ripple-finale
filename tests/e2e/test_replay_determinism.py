# -*- coding: utf-8 -*-
"""Replay: the same inputs produce the same result, twice, on purpose.

Replay is what makes a benchmark figure checkable by somebody who does not
trust us. It consumes a baseline, a recorded sequence of public calls, and a
frozen configuration, and it must reproduce the deterministic parts exactly.

Two things it is **not**:

It is not the production path. The public judge runs live, because a judge
who discovers the answers were cached is right to discount the whole AI
claim. Replay is for CI, regression, benchmark, video reproducibility and
failure analysis -- a test below asserts no public handler consults it.

It is not a licence for the model to be deterministic. Explanation text may
differ between runs where the architecture permits it. What may not differ
is detection, impact, risk, ChangeSet structure and verification state --
and those are exactly what is compared here.
"""

from __future__ import annotations

import pathlib

import pytest

from app.api.router import Api, Request
from app.api.sessions import SessionRegistry
from core.change_intelligence.session import InMemoryBaseline
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
WORKFLOW = "workflow/approval_routes.yaml"
LOCATOR = "approval_routes[3].condition.value"

#: A recorded judge session. The same sequence, every time -- which is what
#: makes the comparison below a statement about the system rather than
#: about the order somebody happened to click in.
SCRIPT = (
    {"operation": "session.create", "timestamp": "2026-09-20T09:00:00Z"},
    {"operation": "state.observe"},
    {"operation": "whatif.simulate",
     "payload": {"target": "approval_threshold", "proposed_value": 500000}},
    {"operation": "intent.create", "actor": "judge",
     "timestamp": "2026-09-20T09:01:00Z",
     "payload": {"text": "the threshold should be 500,000"}},
    {"operation": "lineage.show"},
    {"operation": "scenario.external_edit",
     "payload": {"artifact": WORKFLOW, "locator": LOCATOR, "value": 300000}},
    {"operation": "state.observe"},
    {"operation": "lineage.show"},
    {"operation": "session.reset"},
    {"operation": "state.observe"},
)

#: Fields that legitimately differ between two runs of the same script.
#: Deliberately EMPTY for the deterministic parts: nothing in detection,
#: impact, risk, ChangeSet structure or verification state is allowed here.
NONDETERMINISTIC_FIELDS: frozenset = frozenset()


@pytest.fixture(scope="module")
def artifacts():
    return build_baseline(SEED)["artifacts"]


def _replay(artifacts) -> list:
    """Run the script from a fresh baseline and return every reply."""
    api = Api(sessions=SessionRegistry(
        baseline=InMemoryBaseline.from_artifacts(artifacts)))
    session_id = ""
    trace = []
    for step in SCRIPT:
        request = Request(
            operation=step["operation"],
            session_id=step.get("session_id", session_id),
            payload=dict(step.get("payload", {})),
            actor=step.get("actor", ""),
            timestamp=step.get("timestamp", ""))
        if request.operation == "intent.create":
            from tests.intent_support import confirm_intent
            reply = confirm_intent(api, session_id, request.actor, request.timestamp)
        else:
            reply = api.call(request)
        if reply.ok and "session_id" in reply.data and not session_id:
            session_id = reply.data["session_id"]
        trace.append({"operation": reply.operation, "ok": reply.ok,
                      "code": reply.code, "data": reply.data})
    return trace


# --- the central claim -------------------------------------------------------


def test_two_replays_of_the_same_script_agree_exactly(artifacts):
    assert _replay(artifacts) == _replay(artifacts)


def test_a_third_replay_still_agrees(artifacts):
    """Two runs can agree by accident once. Three is a habit."""
    first, second, third = (_replay(artifacts) for _ in range(3))
    assert first == second == third


def test_every_step_succeeded_so_the_comparison_means_something(artifacts):
    trace = _replay(artifacts)
    failed = [step for step in trace if not step["ok"]]
    assert not failed, (
        f"{failed} -- two replays of a script that fails identically would "
        f"also agree, and would prove nothing")


def test_no_field_is_excluded_from_the_comparison():
    assert NONDETERMINISTIC_FIELDS == frozenset(), (
        "a field was excused from the determinism comparison; detection, "
        "impact, risk, ChangeSet structure and verification state may not "
        "be")


# --- the parts that must not drift -------------------------------------------


def test_snapshot_ids_are_reproduced(artifacts):
    def snapshots(trace):
        return [step["data"].get("snapshot_id") for step in trace
                if "snapshot_id" in step["data"]]

    first, second = _replay(artifacts), _replay(artifacts)
    assert snapshots(first) == snapshots(second)
    assert any(snapshots(first)), "no snapshot id appeared in the trace"


def test_the_external_edit_produces_the_same_transition(artifacts):
    def edit(trace):
        return next(step for step in trace
                    if step["operation"] == "scenario.external_edit")

    first, second = edit(_replay(artifacts)), edit(_replay(artifacts))
    assert first["data"]["from"] == second["data"]["from"]
    assert first["data"]["to"] == second["data"]["to"]
    assert first["data"]["parent_snapshot_id"] == \
        second["data"]["parent_snapshot_id"]


def test_the_lineage_is_reproduced(artifacts):
    def lineage(trace):
        return [step["data"] for step in trace
                if step["operation"] == "lineage.show"]

    assert lineage(_replay(artifacts)) == lineage(_replay(artifacts))


def test_reset_returns_to_the_same_state_in_both_runs(artifacts):
    def final(trace):
        return trace[-1]["data"]["changed_in_session"]

    assert final(_replay(artifacts)) == final(_replay(artifacts)) == []


def test_a_different_script_gives_a_different_trace(artifacts):
    """The comparison must be capable of failing."""
    baseline = _replay(artifacts)

    api = Api(sessions=SessionRegistry(
        baseline=InMemoryBaseline.from_artifacts(artifacts)))
    session = api.call(Request(operation="session.create")).data["session_id"]
    api.call(Request(operation="scenario.external_edit", session_id=session,
                     payload={"artifact": WORKFLOW, "locator": LOCATOR,
                              "value": 250000}))
    observed = api.call(Request(operation="state.observe",
                                session_id=session)).data["snapshot_id"]

    snapshots = [step["data"].get("snapshot_id") for step in baseline]
    assert observed not in snapshots


# --- replay is not the production path ---------------------------------------


def test_no_public_handler_consults_a_replay_source():
    """A judge who finds the answers were cached is right to discount them.

    Scanned with the shared `code_lines`, which drops comments and
    docstrings -- the prose explaining why the public path runs live says
    the word "replay", and a text scan would flag it.
    """
    import pathlib as _pathlib

    import _srcscan

    app = _pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in _srcscan.python_files(app, include_tests=True):
        for number, line in _srcscan.code_lines(path):
            if "replay" in line.lower():
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        f"{offenders} reach for replay; the public path runs live, and a "
        f"judge who discovers the answers were cached is right to discount "
        f"the whole AI claim")


def test_the_script_uses_only_public_operations():
    """No shortcut past the API a judge would use.

    A scenario runner with a private path is the "fake demo path" the next
    gate forbids, and it is invisible until somebody deviates from the
    script.
    """
    from core.change_intelligence import public_surface

    for step in SCRIPT:
        assert public_surface.is_public(step["operation"]), step["operation"]


def test_the_script_covers_the_judge_journey():
    """observe -> change -> reconcile -> drift -> reset."""
    operations = {step["operation"] for step in SCRIPT}
    for required in ("session.create", "state.observe", "whatif.simulate",
                     "intent.create", "scenario.external_edit",
                     "session.reset"):
        assert required in operations, required
