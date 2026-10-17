"""Every limit that stops an AI action is refused with its own code, plain sentence and next action (nothing started, nothing charged)."""
from __future__ import annotations

import json
from decimal import Decimal

from infra import provider_gate
from infra.adapters.v2_postgres import ConcurrencyLimitExceeded, JobCeilingExceeded
from infra.worker.budget_config import BudgetExceeded


def _body(exc):
    r = provider_gate.refusal_for(exc)
    assert r is not None and r.status_code == 409 and r.headers["cache-control"] == "no-store"
    return json.loads(r.body)


def test_each_limit_has_its_own_code_and_a_next_action():
    cases = [(provider_gate.ProviderDisarmed("off"), "provider_disarmed"), (JobCeilingExceeded("used 2 of 2"), "allocation_exhausted"),
             (BudgetExceeded(scope="t/p:total", cap=Decimal("8.30"), spent=Decimal("8.00"), estimate=Decimal("0.44")), "budget_exceeded"),
             (ConcurrencyLimitExceeded("2 running"), "concurrency_limit")]
    for exc, code in cases:
        b = _body(exc)
        assert b["code"] == code and b["error"] and "Nothing" in b["next_action"]


def test_an_unknown_error_is_not_dressed_up_as_a_limit():
    assert provider_gate.refusal_for(RuntimeError("boom")) is None
