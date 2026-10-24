"""Role allowlist (`enabled_roles`, infra/ai_kinds.py): parsing, the fail-closed decision, the route gate and its explained refusal.
Pure tests: a temporary budget configuration file, no database, no provider. The DB / worker path is tests/pitch_brief/test_role_allowlist.py."""
from __future__ import annotations

import json
import pathlib

import pytest

from infra import ai_kinds, provider_gate
from infra.worker.budget_config import BudgetConfigError
from infra.worker.job_quota import load_worker_budget_config

REAL = pathlib.Path(__file__).resolve().parents[2] / "docs" / "runbooks" / "pitch_budget_config.yaml"


def cfg(tmp_path, line: str | None) -> str:
    text = REAL.read_text(encoding="utf-8").replace("enabled_roles: all\n", "" if line is None else line + "\n")
    path = tmp_path / "budget.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.delenv("RIPPLE_PROVIDER_MODE", raising=False)
    monkeypatch.delenv("RIPPLE_OPERATOR_FLAGS", raising=False)
    from infra import operator_flags
    operator_flags.reset_cache()
    yield monkeypatch
    operator_flags.reset_cache()


def test_parsing_absent_all_list_and_invalid_values(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    assert load_worker_budget_config(cfg(tmp_path, None)).enabled_roles is None
    assert load_worker_budget_config(cfg(tmp_path, "enabled_roles: all")).enabled_roles == "all"
    assert load_worker_budget_config(cfg(tmp_path, "enabled_roles: [knowledge.qa, retrieval.semantic]")).enabled_roles == {"knowledge.qa", "retrieval.semantic"}
    for bad, why in (("enabled_roles: [nope]", "not AI job kinds"), ("enabled_roles: []", "non-empty"), ("enabled_roles: everything", "non-empty"),
                     ("enabled_roles: [knowledge.qa, knowledge.qa]", "twice"), ("enabled_roles: [apply]", "not AI job kinds")):
        with pytest.raises(BudgetConfigError, match=why):
            load_worker_budget_config(cfg(tmp_path, bad))


def test_decision_is_fail_closed_and_never_touches_deterministic_kinds():
    assert ai_kinds.refusal(None, "knowledge.qa") == ai_kinds.NOT_CONFIGURED
    assert ai_kinds.refusal(frozenset({"extraction"}), "knowledge.qa") == ai_kinds.NOT_ENABLED
    assert ai_kinds.refusal(frozenset({"knowledge.qa"}), "knowledge.qa") is None
    assert ai_kinds.refusal("all", "roi.analyst") is None
    for deterministic in ("apply", "simulation", "source_ingest", None):
        assert ai_kinds.refusal(None, deterministic) is None
    assert ai_kinds.refusal(frozenset({"knowledge.qa"}), "retrieval.semantic") == ai_kinds.NOT_ENABLED
    assert provider_gate.AI_KINDS is ai_kinds.AI_KINDS and "retrieval.semantic" not in ai_kinds.AI_KINDS     # not a job kind: never skipped or claimed as one


@pytest.mark.parametrize("line,kind,code", [
    ("enabled_roles: [knowledge.qa]", "knowledge.qa", None),
    ("enabled_roles: [knowledge.qa]", "change.understand", "role_not_enabled"),
    (None, "knowledge.qa", "ai_roles_not_configured"),
    ("enabled_roles: all", "roi.analyst", None),
])
def test_route_gate_refuses_with_the_reason_and_an_explained_409(tmp_path, armed, line, kind, code):
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", cfg(tmp_path, line))
    if code is None:
        provider_gate.require_provider(kind)
        assert provider_gate.ai_available(kind)
        return
    with pytest.raises(provider_gate.RoleNotEnabled) as exc:
        provider_gate.require_provider(kind)
    assert exc.value.code == code and not provider_gate.ai_available(kind)
    resp = provider_gate.refusal_for(exc.value)
    body = json.loads(resp.body)
    assert resp.status_code == 409 and body["code"] == code and body["kind"] == kind
    assert "Nothing was started and nothing was charged" in body["next_action"] and "switched off" not in body["error"]


def test_unreadable_configuration_refuses_every_ai_kind_but_no_deterministic_one(tmp_path, armed):
    bad = tmp_path / "broken.yaml"
    bad.write_text("enabled_roles: [nope]\n", encoding="utf-8")
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(bad))
    with pytest.raises(provider_gate.RoleNotEnabled) as exc:
        provider_gate.require_provider("knowledge.qa")
    assert exc.value.code == "ai_config_unreadable"
    provider_gate.require_provider("apply")                          # not an AI kind: untouched


def test_global_switch_wins_over_the_allowlist(tmp_path, armed):
    """The allowlist only removes capability: with the provider disarmed an ENABLED kind is still refused, and the reason stays the switch."""
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", cfg(tmp_path, "enabled_roles: [knowledge.qa]"))
    armed.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    for kind in ("knowledge.qa", "change.understand"):
        with pytest.raises(provider_gate.ProviderDisarmed) as exc:
            provider_gate.require_provider(kind)
        assert not isinstance(exc.value, provider_gate.RoleNotEnabled)
        assert not provider_gate.ai_available(kind)


def test_semantic_layer_gate_follows_the_allowlist(tmp_path, armed):
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", cfg(tmp_path, "enabled_roles: [knowledge.qa]"))
    assert provider_gate.semantic_off() is True                       # knowledge.qa answers with the lexical layers, labelled as a fallback
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", cfg(tmp_path, "enabled_roles: [knowledge.qa, retrieval.semantic]"))
    assert provider_gate.semantic_off() is False
    armed.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    assert provider_gate.semantic_off() is True


def test_shipped_configurations_state_the_key_explicitly():
    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in ("docs/runbooks/pitch_budget_config.yaml", "config/ai_closed_loop/final_budgets.example.yaml", "config/ai_closed_loop/budgets.example.yaml",
                "config/ai_closed_loop/final_budgets.proposed_routing.example.yaml"):
        assert "\nenabled_roles: " in (root / rel).read_text(encoding="utf-8"), rel


def test_the_page_shell_names_the_enabled_kinds(tmp_path, armed):
    from fastapi.testclient import TestClient
    from infra import judge
    c = TestClient(judge.app)
    armed.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    assert 'name="ripple-ai-kinds"' not in c.get("/app-next/").text              # no AI configuration: the provider meta decides alone
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", cfg(tmp_path, "enabled_roles: [knowledge.qa, extraction]"))
    assert '<meta name="ripple-ai-kinds" content="extraction,knowledge.qa">' in c.get("/app-next/").text
    armed.setenv("RIPPLE_BUDGET_CONFIG_PATH", cfg(tmp_path, None))
    assert '<meta name="ripple-ai-kinds" content="none">' in c.get("/app-next/").text
