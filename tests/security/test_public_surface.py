# -*- coding: utf-8 -*-
"""What a stranger on the internet can reach, and everything they cannot.

Two halves. The first is an **inventory**: walk the syntax tree of every
handler the surface names and assert that none of them can touch the
filesystem, spawn a process, import dynamically, or deserialise an object.
The second is a **behaviour** suite: every failure mode from the brief,
each required to fail closed.

An audit that only tested the endpoints would miss the thing that actually
goes wrong, which is an internal helper quietly becoming reachable.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.api import handlers as handlers_module
from app.api.router import Api, PublicError, Request, inventory
from app.api.sessions import SessionRegistry
from core.change_intelligence import public_surface
from core.change_intelligence.public_surface import (
    CLASSIFICATIONS,
    CONTROLLED,
    PUBLIC_SURFACE,
    SurfaceViolation,
    WRITING,
)
from core.change_intelligence.session import InMemoryBaseline
from reference_system.reset.baseline import build_baseline

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED = ROOT / "reference_system"
APP = ROOT / "app"

WORKFLOW = "workflow/approval_routes.yaml"
LOCATOR = "approval_routes[3].condition.value"


@pytest.fixture(scope="module")
def artifacts():
    return build_baseline(SEED)["artifacts"]


@pytest.fixture
def api(artifacts):
    return Api(sessions=SessionRegistry(
        baseline=InMemoryBaseline.from_artifacts(artifacts)))


@pytest.fixture
def session(api):
    return api.call(Request(operation="session.create",
                            timestamp="t")).data["session_id"]


def _edit(api, session, **payload):
    body = {"artifact": WORKFLOW, "locator": LOCATOR, "value": 300_000}
    body.update(payload)
    return api.call(Request(operation="scenario.external_edit",
                            session_id=session, payload=body))


# --- the surface is the whole surface ----------------------------------------


def test_every_handler_is_declared_on_the_surface(api):
    assert set(api.handlers) == set(public_surface.names())


def test_starting_with_an_undeclared_handler_is_refused(artifacts, monkeypatch):
    """Construction fails rather than the route existing quietly."""
    original = handlers_module.build

    def extra(registry):
        table = original(registry)
        table["secret.debug"] = lambda request: {"ok": True}
        return table

    monkeypatch.setattr(handlers_module, "build", extra)
    with pytest.raises(SurfaceViolation, match="implemented-not-declared"):
        Api(sessions=SessionRegistry(
            baseline=InMemoryBaseline.from_artifacts(artifacts)))


def test_a_surface_entry_with_no_handler_is_refused(artifacts, monkeypatch):
    original = handlers_module.build

    def fewer(registry):
        table = original(registry)
        table.pop("lineage.show")
        return table

    monkeypatch.setattr(handlers_module, "build", fewer)
    with pytest.raises(SurfaceViolation, match="declared-not-implemented"):
        Api(sessions=SessionRegistry(
            baseline=InMemoryBaseline.from_artifacts(artifacts)))


def test_every_writing_operation_states_its_boundary():
    for operation in public_surface.writing_operations():
        assert operation.boundary, operation.name


def test_every_operation_has_a_classification_from_the_closed_set():
    for operation in PUBLIC_SURFACE:
        assert operation.classification in CLASSIFICATIONS


def test_there_is_no_admin_or_debug_classification():
    for banned in ("ADMIN", "INTERNAL", "DEBUG", "UNSAFE"):
        assert banned not in CLASSIFICATIONS


def test_exactly_one_operation_is_controlled():
    controlled = [o for o in PUBLIC_SURFACE if o.classification == CONTROLLED]
    assert len(controlled) == 1


def test_the_whitelist_is_derived_from_the_surface_not_maintained():
    assert (public_surface.external_edit_whitelist()
            is public_surface.controlled_operation().mutations)


def test_no_other_module_declares_a_whitelist():
    """One list, or it is two lists that will drift."""
    offenders = []
    for path in list((ROOT / "core").rglob("*.py")) + list(APP.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "public_surface.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("WHITELIST =", "ALLOWED_ARTIFACTS =",
                       "ALLOWED_CELLS ="):
            if marker in text:
                offenders.append(f"{path.name}:{marker}")
    assert not offenders, offenders


def test_the_inventory_is_readable_as_data():
    rows = inventory()
    assert len(rows) == len(PUBLIC_SURFACE)
    assert all(row["classification"] in CLASSIFICATIONS for row in rows)
    assert any(row["allows"] for row in rows)


def test_the_inventory_is_deterministic():
    assert inventory() == inventory()


# --- the endpoint audit, by syntax tree --------------------------------------

#: Things no public handler may reach. Matched on call and import nodes, not
#: on text, so a docstring mentioning one is not a finding and an aliased
#: call still is.
FORBIDDEN_CALLS = {
    "system", "popen", "spawn", "fork", "exec", "eval", "compile",
    "__import__", "import_module", "loads", "load", "unlink", "remove",
    "rmtree", "mkdir", "chmod", "rename", "urlopen", "request", "connect",
}
FORBIDDEN_IMPORTS = {"subprocess", "os", "shutil", "pickle", "marshal",
                     "socket", "urllib", "requests", "importlib", "ctypes"}


def _app_trees():
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_public_module_imports_a_dangerous_library():
    offenders = []
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0] in FORBIDDEN_IMPORTS:
                    offenders.append(f"{path.name}:{node.lineno} {name}")
    assert not offenders, offenders


def test_no_public_module_calls_a_dangerous_function():
    offenders = []
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name in FORBIDDEN_CALLS:
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert not offenders, offenders


def test_no_public_module_deserialises_an_object():
    """`yaml.safe_load` only. `yaml.load` would execute constructors."""
    offenders = []
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "load"
                    and "yaml" in ast.unparse(node.func.value)):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


def test_no_public_handler_opens_a_file():
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "open"):
                pytest.fail(f"{path.name}:{node.lineno} opens a file; the "
                            f"runtime reads through the overlay (I17)")


def test_the_handlers_hold_no_decision_of_their_own():
    source = inspect.getsource(handlers_module)
    for banned in ("AlignmentState.", "HIGH_AT", "MEDIUM_AT"):
        assert banned not in source, (
            f"handlers name {banned}; a verdict computed here is a second "
            f"implementation of a rule that already exists")


# --- failure modes: every one fails closed -----------------------------------


def test_an_unknown_operation_is_refused(api):
    reply = api.call(Request(operation="debug.dump_everything"))
    assert not reply.ok and reply.status == 404
    assert reply.code == "unknown_operation"


def test_a_missing_session_is_refused(api):
    reply = api.call(Request(operation="state.observe"))
    assert not reply.ok and reply.code == "missing_session"


def test_an_unknown_session_is_refused(api):
    reply = api.call(Request(operation="state.observe",
                             session_id="SESSION-9999"))
    assert not reply.ok and reply.status == 404
    assert reply.code == "unknown_session"


def test_an_unknown_session_is_not_created_helpfully(api):
    api.call(Request(operation="state.observe", session_id="SESSION-9999"))
    assert api.sessions.count() == 0


def test_a_malformed_request_is_refused(api, session):
    reply = api.call(Request(operation="scenario.external_edit",
                             session_id=session, payload={}))
    assert not reply.ok and reply.code == "malformed_request"


@pytest.mark.parametrize("artifact", [
    "../../etc/passwd",
    "/etc/passwd",
    "workflow/../../secret.yaml",
    "C:\\Windows\\system32\\config",
    "~/.ssh/id_rsa",
    "file://workflow/approval_routes.yaml",
])
def test_path_traversal_is_refused(api, session, artifact):
    reply = _edit(api, session, artifact=artifact)
    assert not reply.ok
    assert reply.code in ("invalid_artifact_path", "artifact_not_whitelisted")


def test_an_artifact_outside_the_whitelist_is_refused(api, session):
    reply = _edit(api, session, artifact="docs/procurement-policy.md")
    assert not reply.ok and reply.status == 403
    assert reply.code == "artifact_not_whitelisted"


def test_a_locator_outside_the_whitelist_is_refused(api, session):
    reply = _edit(api, session,
                  locator="approval_routes[0].condition.value")
    assert not reply.ok and reply.status == 403
    assert reply.code == "locator_not_whitelisted"


@pytest.mark.parametrize("value", ["500000", -1, 0, 10**12, 3.5, True, None,
                                   {"a": 1}, ["x"]])
def test_an_invalid_value_is_refused(api, session, value):
    reply = _edit(api, session, value=value)
    assert not reply.ok
    assert reply.code in ("invalid_value", "malformed_request")


def test_a_string_that_looks_like_a_number_is_not_coerced(api, session):
    """The value a person sees must be the value that lands."""
    reply = _edit(api, session, value="300000")
    assert not reply.ok and reply.code == "invalid_value"


def test_a_stale_expected_value_is_refused(api, session):
    reply = _edit(api, session, expected_current=999)
    assert not reply.ok and reply.status == 409
    assert reply.code == "stale_artifact"


def test_a_refused_edit_changes_nothing(api, session):
    before = api.call(Request(operation="state.observe",
                              session_id=session)).data["changed_in_session"]
    _edit(api, session, artifact="docs/procurement-policy.md")
    _edit(api, session, value="not a number")
    _edit(api, session, expected_current=999)
    after = api.call(Request(operation="state.observe",
                             session_id=session)).data["changed_in_session"]
    assert before == after == []


def test_an_edit_that_changes_nothing_is_reported_not_written(api, session):
    _edit(api, session, value=300_000)
    reply = _edit(api, session, value=300_000)
    assert reply.ok and reply.data["changed"] is False


def test_a_repeated_external_edit_is_idempotent_on_value(api, session):
    first = _edit(api, session, value=300_000)
    second = _edit(api, session, value=300_000)
    assert first.data["changed"] and not second.data["changed"]


def test_applying_without_approval_is_refused(api, session):
    from tests.intent_support import confirm_intent
    confirm_intent(api, session)
    intents = api.sessions.get(session).listing("intent")
    whatif = api.call(Request(
        operation="whatif.simulate", session_id=session,
        payload={"target": "approval_threshold", "proposed_value": 500000}))
    prepared = api.call(Request(
        operation="changeset.prepare", session_id=session,
        payload={"intent_id": intents[0],
                 "shadow_id": whatif.data["shadow_id"]}))
    assert prepared.ok, prepared.error
    reply = api.call(Request(
        operation="changeset.apply", session_id=session,
        actor="judge", timestamp="t",
        payload={"changeset_id": prepared.data["changeset_id"]}))
    assert not reply.ok and reply.code == "approval_required"


def test_a_decision_without_an_actor_is_refused(api, session):
    reply = api.call(Request(
        operation="changeset.decide", session_id=session,
        payload={"changeset_id": f"{session}:changeset:x",
                 "decision": "approve", "reason": "looks fine"}))
    assert not reply.ok and reply.code == "unattributed_decision"


def test_an_unknown_decision_verb_is_refused(api, session):
    reply = api.call(Request(
        operation="changeset.decide", session_id=session, actor="judge",
        timestamp="t",
        payload={"changeset_id": f"{session}:changeset:x",
                 "decision": "merge", "reason": "r"}))
    assert not reply.ok and reply.code == "unknown_decision"


def test_an_unexpected_exception_fails_closed(api, session, monkeypatch):
    def explode(request):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setitem(api.handlers, "state.observe", explode)
    reply = api.call(Request(operation="state.observe", session_id=session))
    assert not reply.ok and reply.status == 500
    assert "nothing was changed" in reply.error
    assert "RuntimeError" in reply.error


def test_a_refusal_carries_a_code_a_caller_can_act_on(api):
    reply = api.call(Request(operation="nope"))
    assert reply.code and reply.error and reply.code != reply.error


def test_every_reply_echoes_the_operation_and_classification(api, session):
    reply = api.call(Request(operation="state.observe", session_id=session,
                             request_id="REQ-1"))
    assert reply.operation == "state.observe"
    assert reply.classification == "READ"
    assert reply.request_id == "REQ-1"


# --- cross-session references ------------------------------------------------


def test_one_session_cannot_name_another_sessions_object(api):
    first = api.call(Request(operation="session.create")).data["session_id"]
    second = api.call(Request(operation="session.create")).data["session_id"]

    from tests.intent_support import confirm_intent
    confirm_intent(api, first)
    theirs = api.sessions.get(first).listing("intent")[0]

    # A real shadow_id owned by `second`, so the refusal below is proven to
    # come from the FOREIGN intent_id check, not from a missing payload
    # field -- changeset.prepare now requires both (SS8.3).
    whatif = api.call(Request(
        operation="whatif.simulate", session_id=second,
        payload={"target": "approval_threshold", "proposed_value": 500000}))

    reply = api.call(Request(operation="changeset.prepare",
                             session_id=second,
                             payload={"intent_id": theirs,
                                      "shadow_id": whatif.data["shadow_id"]}))
    assert not reply.ok and reply.status == 403
    assert reply.code == "cross_session"


def test_a_handler_cannot_reach_the_registry_from_a_session(api, session):
    """Isolation must not rest on every handler author remembering."""
    judge_session = api.sessions.get(session)
    for attribute in ("registry", "sessions", "store", "_sessions", "all"):
        assert not hasattr(judge_session, attribute), (
            f"JudgeSession exposes {attribute!r}, which is a route to every "
            f"other session")
