"""BAS-03: the Final budget example prices every provider-backed role, spends nothing, and a model without a price fails closed.

Pure config tests (no DB, no provider). DB/worker evidence for disarmed zero-call is in tests/pitch_brief/test_final_disarmed_zero_calls.py.
"""
import pathlib
import textwrap
from decimal import Decimal

import pytest

from infra import provider_gate
from infra.worker import budget_invariants
from infra.worker.budget_config import BudgetConfigError, load_budget_config, model_for_role

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "ai_closed_loop" / "final_budgets.example.yaml"
PLANNED_ROLES = ("knowledge.qa", "retrieval.semantic", "document.wording")


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    return load_budget_config(str(EXAMPLE))


def test_every_provider_backed_kind_and_planned_role_has_an_explicit_model(config):
    for role in sorted(provider_gate.AI_KINDS) + list(PLANNED_ROLES):
        assert role in config.role_model_ids, f"{role} would silently run on default_model_id"
        assert config.role_model_ids[role] in config.model_prices
        assert model_for_role(config, role) == config.role_model_ids[role]


def test_example_is_no_spend_and_uses_the_existing_attempt_arithmetic(config):
    assert config.total_cap_usd == Decimal("0.01") and config.per_job_cap_usd == Decimal("0.01")  # the loader needs caps above 0; never reached at zero price
    assert budget_invariants.attempts(config) == config.max_retries + 1 == 2
    assert all(p.input_usd_per_1k == 0 and p.output_usd_per_1k == 0 for p in config.model_prices.values())
    assert config.fail_closed_on_missing_usage is True


def _write(tmp_path, body):
    path = tmp_path / "cfg.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def test_a_role_model_without_a_price_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    text = EXAMPLE.read_text(encoding="utf-8").replace("knowledge.qa: stand_in", "knowledge.qa: unpriced-model")
    with pytest.raises(BudgetConfigError, match="not a key in model_prices"):
        load_budget_config(_write(tmp_path, text))


def test_a_missing_cap_or_ceiling_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    for key in ("max_output_tokens", "max_input_tokens_per_call", "total_cap_usd"):
        lines = [l for l in EXAMPLE.read_text(encoding="utf-8").splitlines() if not l.startswith(key + ":")]
        with pytest.raises(BudgetConfigError):
            load_budget_config(_write(tmp_path, "\n".join(lines)))


def test_a_float_money_value_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    text = EXAMPLE.read_text(encoding="utf-8").replace('total_cap_usd: "0.01"', "total_cap_usd: 0.5")
    with pytest.raises(BudgetConfigError):
        load_budget_config(_write(tmp_path, text))
