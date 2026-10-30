"""The production AI configuration example: an explicit role list of the Final scope (never `all`), no Pitch-era kind, every enabled role pinned
to a priced model, the owner-approved 10 USD month cap, retries off, and a call ceiling that lets a question use its semantic layers."""
import pathlib
from decimal import Decimal

from infra import ai_kinds
from infra.worker import budget_invariants
from infra.worker.job_quota import load_worker_budget_config

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "config" / "ai_closed_loop" / "final_budgets.production.example.yaml"
LEGACY = {"analysis", "change.draft", "gap.answer", "sim.analysis", "explain", "intent.interpret", "impact.judge"}


def test_production_example_lists_the_final_roles_explicitly(monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    c = load_worker_budget_config(str(EXAMPLE))
    assert isinstance(c.enabled_roles, frozenset) and c.enabled_roles != ai_kinds.ALL
    assert not (c.enabled_roles & LEGACY)
    assert c.enabled_roles | LEGACY == ai_kinds.MODEL_ROLES                      # every non-legacy model role of the product is enabled
    for role in c.enabled_roles:
        assert c.role_model_ids.get(role) in c.model_prices, role
    assert c.total_cap_usd == Decimal("10.00") and c.max_retries == 0 and c.fail_closed_on_missing_usage
    # R5 (2026-10-28): gap proposals on 3.8 Flash; the reservation of the priciest model (standard rate) x 4 calls fits the per-job cap, so no job is refused
    assert c.max_provider_calls_per_job == 4 and c.per_job_cap_usd == Decimal("0.52") and c.role_model_ids["knowledge.gap_proposals"] == "gemini-3.8-flash"
    assert budget_invariants.job_reservation_usd(c) == Decimal("0.51264") and c.role_model_ids["request.assistant"] == "gemini-3.5-flash-lite"
    assert not budget_invariants.problems(c), budget_invariants.problems(c)


def test_production_example_gives_the_request_assistant_its_own_45_second_deadline_and_no_other_role_one(monkeypatch):
    """R11 (owner decision 2026-10-28): request.assistant has a 45 s deadline; every other role keeps the shared 120 s request timeout."""
    from infra.worker.budget_config import role_request_timeout
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    c = load_worker_budget_config(str(EXAMPLE))
    # R17 (c88d0c5f) gave the supplementary call of the same job the same 45 s bound in the example; this assertion was not updated then (found R21)
    assert c.role_request_timeouts == {"request.assistant": 45.0, "request.assistant.complete": 45.0} and c.request_timeout_seconds == 120.0
    own = {"request.assistant", "request.assistant.complete"}
    assert all(role_request_timeout(c, r) == 45.0 for r in own) and all(role_request_timeout(c, r) is None for r in c.enabled_roles - own)
