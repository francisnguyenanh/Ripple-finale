# -*- coding: utf-8 -*-
"""Make the shared helpers importable from every test package under tests/."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import os  # noqa: E402

import pytest  # noqa: E402

# The sandbox probe programs are a verification fixture and are disabled in a deployment; tests that exercise them opt in here (tests/ai11t01/test_unreachable_features.py proves the default).
os.environ.setdefault("RIPPLE_SANDBOX_PROBES", "1")
os.environ.setdefault("RIPPLE_SIM_RUNNER_ADAPTERS", "hd2,harbor_cli")      # the deployment default is harbor_cli only (tests/ai11t01/test_unreachable_features.py)


@pytest.fixture
def legacy_contract_injection(monkeypatch):
    """W1 LEGACY-MECHANICS SHORTCUT (opt-in via `pytestmark = pytest.mark.usefixtures(...)`): a BLOCKED
    review can no longer be approved or applied, so pre-W1 journeys/drivers that never state a
    verification contract would stop at the approval. This wraps ONLY the review stage's acceptance
    check: when the payload names no contract it stores one taken from the proposal's own recorded
    edit (circular -- exactly what production forbids; independence is proven in tests/w1_prod) and
    then runs the REAL check, so PASS/FAIL/UNAVAILABLE (multi-edit, python, markdown, no-op) stay
    genuine. Use it only where the test's subject is downstream of the approval."""
    from infra.worker import review as review_mod
    from tests.ai7t02.conftest import store_fixture_contract
    real = review_mod._check_acceptance_tests

    def injecting(ctx, payload, proposal_result, edit_kind, staged_bytes, is_no_op):
        if not payload.get("verification_contract_id"):
            cid = store_fixture_contract(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"],
                                         payload.get("proposal_job_id"))
            if cid:
                payload = {**payload, "verification_contract_id": cid}
        return real(ctx, payload, proposal_result, edit_kind, staged_bytes, is_no_op)

    monkeypatch.setattr(review_mod, "_check_acceptance_tests", injecting)


@pytest.fixture(autouse=True)
def _restore_apply_row_gate():
    yield
    from tests import _row_gate
    _row_gate.restore()


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """Process-wide login throttle (infra/v2_auth.py) must not leak failed-login counts between tests."""
    import sys
    mod = sys.modules.get("infra.v2_auth")
    if mod is not None and hasattr(mod, "_THROTTLE"):
        mod._THROTTLE.reset()
    judge = sys.modules.get("infra.judge")
    legacy = getattr(judge, "_LEGACY_LOGIN_THROTTLE", None)
    if legacy is not None:
        legacy.reset()
    yield
