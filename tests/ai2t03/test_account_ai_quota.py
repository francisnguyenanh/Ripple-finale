"""The daily AI-job allowance is per ACCOUNT (shared accounts share it), counted inside the creation transaction, and refused with its own explained code."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from infra import provider_gate
from infra.adapters.v2_postgres import PersonQuotaExceeded, StaleVersion
from tests.ai2t03.test_worker_terminal_ack_and_budget_round4 import project  # noqa: F401


def _job(store, t, w, kind, account, version=None):
    head = store.head(t, w)
    store.create_job(t, w, f"q-{uuid4().hex}", kind, {}, expected_version=head.version if version is None else version, idempotency_key=f"k-{uuid4().hex}", requested_by=account)


def test_the_third_ai_job_of_an_account_is_refused_others_are_not_and_deterministic_jobs_are_not_counted(project, monkeypatch):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    monkeypatch.setenv("RIPPLE_AI_JOBS_PER_ACCOUNT_DAY", "2")
    acct, other = f"acct-{uuid4().hex[:6]}", f"acct-{uuid4().hex[:6]}"
    _job(store, t, w, "extraction", acct)
    _job(store, t, w, "gap.answer", acct)
    for _ in range(3):
        _job(store, t, w, "change", acct)                                   # deterministic kinds do not use the AI allowance
    with pytest.raises(PersonQuotaExceeded) as exc:
        _job(store, t, w, "extraction", acct)
    assert "2 of 2" in str(exc.value)
    _job(store, t, w, "extraction", other)                                  # another account has its own allowance
    body = json.loads(provider_gate.refusal_for(exc.value).body)
    assert body["code"] == "person_quota" and "per account" in body["next_action"]


def test_a_creation_that_fails_does_not_use_the_allowance(project, monkeypatch):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    monkeypatch.setenv("RIPPLE_AI_JOBS_PER_ACCOUNT_DAY", "1")
    acct = f"acct-{uuid4().hex[:6]}"
    with pytest.raises(StaleVersion):
        _job(store, t, w, "extraction", acct, version=store.head(t, w).version + 5)       # refused before it exists: the counter rolls back with it
    _job(store, t, w, "extraction", acct)                                                   # the one allowed job is still available


def test_no_limit_configured_means_no_counting(project, monkeypatch):
    t, w, store = project["tenant"], project["workspace"], project["store"]
    monkeypatch.delenv("RIPPLE_AI_JOBS_PER_ACCOUNT_DAY", raising=False)
    acct = f"acct-{uuid4().hex[:6]}"
    for _ in range(4):
        _job(store, t, w, "extraction", acct)
