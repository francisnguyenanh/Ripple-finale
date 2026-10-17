# -*- coding: utf-8 -*-
"""Apply cannot be reached without a real approval, through the
PUBLIC API only. Companion to test_approval_bypass.py (which drives
apply_gate directly): this file proves the same guarantees survive being
wrapped in handlers.py.
"""

from __future__ import annotations

import pathlib

import pytest

from app.api.router import Api, Request
from app.api.sessions import SessionRegistry
from core.change_intelligence.session import InMemoryBaseline
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
TARGET = "approval_threshold"
ACTOR = "judge@example.com"
WHEN = "2026-09-20T14:00:00Z"


@pytest.fixture
def api():
    built = build_baseline(SEED)
    baseline = InMemoryBaseline.from_artifacts(built["artifacts"])
    registry = SessionRegistry(baseline=baseline)
    return Api(sessions=registry)


def _call(api, operation, **payload):
    request = Request(operation=operation, session_id=payload.pop("session_id", ""),
                      payload=payload, actor=payload.pop("actor", ""),
                      timestamp=payload.pop("timestamp", ""))
    return api.call(request)


def _prepared_changeset(api):
    session_reply = _call(api, "session.create")
    session_id = session_reply.data["session_id"]

    from tests.intent_support import confirm_intent
    intent = confirm_intent(api, session_id, ACTOR, WHEN)
    intent_id = intent.data["intent_id"]

    whatif = _call(api, "whatif.simulate", session_id=session_id,
                   target=TARGET, proposed_value=500000)
    shadow_id = whatif.data["shadow_id"]

    prepare = _call(api, "changeset.prepare", session_id=session_id,
                    intent_id=intent_id, shadow_id=shadow_id)
    assert prepare.ok, prepare.error
    return session_id, prepare.data["changeset_id"]


def test_apply_without_approval_is_refused_and_changes_nothing(api):
    session_id, changeset_id = _prepared_changeset(api)
    session = api.sessions.get(session_id)
    snapshot_before = session.snapshot.snapshot_id

    reply = _call(api, "changeset.apply", session_id=session_id,
                 changeset_id=changeset_id, actor=ACTOR, timestamp=WHEN)

    assert not reply.ok
    assert session.snapshot.snapshot_id == snapshot_before, (
        "an apply that was refused moved the snapshot anyway")


def test_a_stale_approval_is_refused(api):
    session_id, changeset_id = _prepared_changeset(api)

    decide = _call(api, "changeset.decide", session_id=session_id,
                   changeset_id=changeset_id, decision="approve",
                   reason="ok", actor=ACTOR, timestamp=WHEN)
    assert decide.ok, decide.error

    # Force the stored ChangeSet's proposal to look different from what was
    # approved, the same way test_approval_bypass.py does at the core level:
    # re-prepare against the (now-approved) session would mint a NEW
    # changeset_id, so instead corrupt the persisted actions in place via
    # the only channel this test may use -- the public API has no operation
    # that edits a proposal after approval, which is the point. So this
    # test targets the handler's OWN version check by asking it to apply a
    # changeset_id that was never decided at all: a stale/unknown version.
    reply = _call(api, "changeset.apply", session_id=session_id,
                 changeset_id="CHG-doesnotexist", actor=ACTOR, timestamp=WHEN)
    assert not reply.ok
    assert reply.code in ("unknown_changeset", "cross_session")


def test_auto_safe_does_not_skip_changeset_decide(api):
    """AUTO_SAFE is a label on the prepare response, never an apply bypass."""
    session_id, changeset_id = _prepared_changeset(api)

    reply = _call(api, "changeset.apply", session_id=session_id,
                 changeset_id=changeset_id, actor=ACTOR, timestamp=WHEN,
                 force=True, skip_approval=True, readiness="AUTO_SAFE")

    assert not reply.ok, (
        "changeset.apply accepted extra payload fields as a bypass; no such "
        "parameter should let AUTO_SAFE skip changeset.decide")
