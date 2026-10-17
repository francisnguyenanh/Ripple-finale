"""Every AI job that the page says "a different person must allow" is held in EVERY workspace until approved (not only sandboxes).
Found live 2026-10-08: change.draft and the gap suggestion/query (gap.answer) ran while PENDING_APPROVAL in a non-sandbox workspace."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from infra.worker import sandbox_hold


class _Cur:
    def __init__(self, status): self.status = status
    def execute(self, *a, **k): pass
    def fetchone(self): return (self.status,)


class _Store:
    def __init__(self, status): self.status = status
    @contextmanager
    def _tx(self):
        yield _Cur(self.status)


@pytest.fixture(autouse=True)
def _non_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox_hold, "is_sandbox", lambda *a: False)


@pytest.mark.parametrize("kind", ["change.draft", "gap.answer", "sim.analysis", "explain"])
def test_requested_ai_job_pending_ai_job_is_held_in_a_non_sandbox_workspace(kind):
    assert sandbox_hold.approval_hold(_Store("PENDING_APPROVAL"), "t", "w", "j", kind) is True
    assert sandbox_hold.approval_hold(_Store("APPROVED"), "t", "w", "j", kind) is False


def test_jobs_with_their_own_gate_or_no_provider_are_not_held_here():
    # intent.interpret has its own per-conversation allowance; extraction/relation/impact make no requester approval claim in the page
    for kind in ("intent.interpret", "extraction", "relation", "impact", "simulation"):
        assert sandbox_hold.approval_hold(_Store("PENDING_APPROVAL"), "t", "w", "j", kind) is False, kind
