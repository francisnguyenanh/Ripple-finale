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
    assert c.max_provider_calls_per_job == 4 and c.per_job_cap_usd == Decimal("0.16")
    assert not budget_invariants.problems(c), budget_invariants.problems(c)
