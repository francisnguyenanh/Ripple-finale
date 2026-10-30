"""The review's `ai_steps` (D2a equivalent flow, owner decision 2026-10-27): who may ask for the dependency check and the guidance of a confirmed change, the next
attempt, the guidance gate (the same one the guidance route enforces) and a kind this server does not enable. Pure: no database, no model."""
from __future__ import annotations

import pytest

from infra import provider_gate
from infra import v2_business_routes as routes

PROBE_REQUIRED = {"report": {"write_scope": [], "affected_rules": [], "counterevidence_rule_ids": []}, "rules": {"rule-x": {"status": "VALIDATED"}}}
PROBE_NOT_NEEDED = {"report": {"write_scope": ["rule-x"], "affected_rules": [], "counterevidence_rule_ids": []}, "rules": {"rule-x": {"status": "VALIDATED"}}}
REQUESTER = ("intent.create", "ai.use", "change.prepare")


def view(dep=None, guide=None, perms=REQUESTER, probe=PROBE_REQUIRED):
    return {"probe": probe, "dep_state": dep or {"status": "not_requested"}, "draft": {"guidance": guide or {"status": "not_requested"}}, "perms": list(perms)}


@pytest.fixture(autouse=True)
def all_enabled(monkeypatch):
    monkeypatch.setattr(provider_gate, "role_refusal", lambda kind: None)


def test_a_requester_may_ask_the_dependency_check_first_and_guidance_waits_for_it_with_the_reason():
    s = routes._ai_steps(view())
    assert s["dependencies"] == {"may_ask": True, "next_attempt": 1, "kind_enabled": True}
    g = s["guidance"]
    assert g["may_ask"] is True and g["next_attempt"] == 1 and g["gate"]["code"] == "dependencies_incomplete"
    assert "dependency check first" in g["gate"]["next_action"] and "POST" not in g["gate"]["next_action"]


def test_guidance_opens_once_the_check_completed_with_full_coverage_or_when_no_check_is_needed():
    assert routes._ai_steps(view(dep={"status": "completed", "complete": True, "attempt": 1}))["guidance"]["gate"] is None
    assert routes._ai_steps(view(dep={"status": "completed", "complete": True, "attempt": 1}))["dependencies"]["next_attempt"] is None   # done: nothing to re-ask
    assert routes._ai_steps(view(probe=PROBE_NOT_NEEDED))["guidance"]["gate"] is None
    partial = routes._ai_steps(view(dep={"status": "completed", "complete": False, "attempt": 1}))["guidance"]["gate"]
    assert partial and "partial" in partial["next_action"]


def test_a_failed_or_stale_step_offers_the_next_attempt_and_never_a_fourth():
    assert routes._ai_steps(view(dep={"status": "failed", "attempt": 1}))["dependencies"]["next_attempt"] == 2
    assert routes._ai_steps(view(dep={"status": "stale", "attempt": 2}))["dependencies"]["next_attempt"] == 3
    assert routes._ai_steps(view(dep={"status": "failed", "attempt": 3}))["dependencies"]["next_attempt"] is None
    assert routes._ai_steps(view(dep={"status": "pending", "attempt": 1}))["dependencies"]["next_attempt"] is None
    assert routes._ai_steps(view(guide={"status": "tampered", "attempt": 1}))["guidance"]["next_attempt"] == 2
    assert routes._ai_steps(view(guide={"status": "completed", "attempt": 1}))["guidance"]["next_attempt"] is None


def test_a_reader_without_the_permissions_may_not_ask_and_a_kind_this_server_does_not_enable_is_said(monkeypatch):
    viewer = routes._ai_steps(view(perms=("knowledge.read",)))
    assert viewer["dependencies"]["may_ask"] is False and viewer["guidance"]["may_ask"] is False
    no_prepare = routes._ai_steps(view(perms=("intent.create", "ai.use")))
    assert no_prepare["dependencies"]["may_ask"] is True and no_prepare["guidance"]["may_ask"] is False
    monkeypatch.setattr(provider_gate, "role_refusal", lambda kind: "role_not_enabled" if kind == "change.draft_guidance" else None)
    off = routes._ai_steps(view())
    assert off["guidance"] == {**off["guidance"], "may_ask": False, "kind_enabled": False} and off["dependencies"]["kind_enabled"] is True
