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
    assert budget_invariants.job_reservation_usd(c) == Decimal("0.51264")
    # R23 (acceptance v8, 2026-10-30): the request assistant runs on gemini-3.8-flash, the one recovery call at the `us` multi-region, its own output caps
    assert all(c.role_model_ids[r] == "gemini-3.8-flash" for r in ("request.qa", "request.assistant", "request.assistant.recover"))
    assert c.role_locations == {"request.assistant.recover": "us"} and "gemini-3.8-flash@us" in c.model_prices
    assert c.role_max_output_tokens == {"request.assistant": 4096, "request.assistant.recover": 4096}
    assert c.role_generation_max_output_tokens == {"request.assistant": 2048, "request.assistant.recover": 2048}
    assert not budget_invariants.problems(c), budget_invariants.problems(c)


def test_production_example_gives_the_request_assistant_and_its_recovery_their_own_deadline_and_no_other_role_one(monkeypatch):
    """R11 (owner decision 2026-10-28): the request assistant has its own deadline; R23 (acceptance v8): 42 s for the job and the recovery call, 45 s kept for the
    disabled supplementary call; every other role keeps the shared 120 s request timeout."""
    from infra.worker.budget_config import role_request_timeout
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    c = load_worker_budget_config(str(EXAMPLE))
    assert c.role_request_timeouts == {"request.assistant": 42.0, "request.assistant.recover": 42.0, "request.assistant.complete": 45.0} and c.request_timeout_seconds == 120.0
    own = {"request.assistant", "request.assistant.recover", "request.assistant.complete"}
    assert role_request_timeout(c, "request.assistant") == 42.0 and role_request_timeout(c, "request.assistant.recover") == 42.0
    assert all(role_request_timeout(c, r) is None for r in c.enabled_roles - own)


def test_release_documents_name_the_assistant_model_of_the_production_example(monkeypatch):
    """DOC-02 (audit 2026-10-30): the quality plan pin row and the release checklist must name the model the production example pins for the request assistant,
    so a route change cannot leave the published documents describing another model. Pure file test."""
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    c = load_worker_budget_config(str(EXAMPLE))
    model = c.role_model_ids["request.assistant"]
    root = EXAMPLE.parents[2]
    plan_row = next(l for l in (root / "docs" / "evaluation" / "FINAL_AI_QUALITY_PLAN.md").read_text(encoding="utf-8").splitlines() if l.startswith("| request.assistant |"))
    assert model in plan_row.split("|")[4].split("history:")[0], "the current evidence of the pin row names another model"
    checklist = (root / "docs" / "release" / "FINAL_RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    rows = [l for l in checklist.splitlines() if l.startswith("| Request assistant")]
    assert rows and all(model in l for l in rows), "a release checklist row for the request assistant does not name the pinned model"
