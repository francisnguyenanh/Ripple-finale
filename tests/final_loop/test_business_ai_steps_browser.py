"""D2a equivalent flow (owner decision 2026-10-27): the AI steps of a confirmed business change are asked from its review on the React build — the model dependency
check, then the guidance on the server-built draft — through the real routes, worker, job / budget contract and store. The guidance stays closed until the check
has completed, and says why; a viewer cannot ask; every server-proven effect is shown whatever the AI text says.

Real server (uvicorn thread), real worker thread, real Chromium, throwaway PostgreSQL. The model is a synthetic stub that answers by prompt (understanding,
dependency check, guidance); the accounts are SIMULATED roles. Nothing here is a statement about model quality.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj  # noqa: E402
from tests.final_loop.test_chg03_draft import gitem, gobj  # noqa: E402
from tests.final_loop.test_kno01_business_store import bind, ws  # noqa: E402,F401
from tests.final_loop.test_kno03_graph_gaps import seed  # noqa: E402
from tests.final_loop.test_kno05_accept import accept, answer  # noqa: E402
from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE  # noqa: E402
from tests.final_loop.test_qry03_qa import Model  # noqa: E402
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, LiveWorker  # noqa: E402
from infra.adapters.v2_identity import PostgresIdentityStore  # noqa: E402
from tests.pitch_brief.conftest import drain  # noqa: E402

SUMMARY = "Raise the threshold; the server's facts say which cases move. Update the policy wording by hand."
# R5: a sentence claiming a cover the rules do not give (no rule reaches 1,000,000 to under 1,500,000 JPY after the change); the server removes it, and the reader
# sees it struck through and attributed, beside the server's own effect
FALSE_COVER = "Purchases from 1,000,000 to 1,500,000 JPY now fall under the finance review."


def answer_by_prompt(prompt: str):
    if prompt.startswith("You help a person check what else a business change might touch"):
        return {"status": "NONE_FOUND", "dependencies": [], "notes": []}
    if prompt.startswith("You help a person carry out ONE confirmed business change"):
        return gobj(items=[gitem("F001", instruction="Edit the approval threshold as confirmed.")], summary=SUMMARY + " " + FALSE_COVER)
    return model_obj(ops=[OP_VALUE])


@pytest.fixture
def ui(env, ws, monkeypatch, tmp_path):                   # noqa: F811
    path = tmp_path / "budget_ui.yaml"
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("  intent.explain: gemini-3.5-flash-lite", "  intent.explain: gemini-3.5-flash-lite\n  change.understand: gemini-3.8-flash\n"
                                                              "  change.impact_deps: gemini-3.8-flash\n  change.draft_guidance: gemini-3.8-flash")
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    model = Model(answer_by_prompt)
    monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    seed(env, ws, rules=bind(KNOWLEDGE["observed_rules"] + DISTRACTORS, ws.source_revision_id))
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "conflicting_facts")
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "Finance reviews CAPEX from 2,000,000 JPY.")
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    ids = PostgresIdentityStore(env.dsn)
    viewer = f"view-{env.nonce}"
    ids.create_account(viewer, "cv")
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, viewer, ("viewer",))
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    yield env, admin, model, server, viewer
    server.stop()
    worker.stop.set()


def test_the_requester_asks_the_dependency_check_then_the_guidance_from_the_review_and_reads_it(ui, pw_browser):
    env, admin, model, server, viewer = ui
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.get_by_test_id("chg-input").fill(NUM_REQ)
    req.get_by_test_id("chg-submit").click()
    expect(req.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "READY", timeout=WAIT)
    req.get_by_test_id("chg-confirm").click()
    expect(req.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    steps = req.get_by_test_id("ai-steps")
    expect(steps).to_be_visible()
    expect(req.get_by_test_id("ai-deps")).to_have_attribute("data-status", "not_requested")
    # guidance is closed until the dependency check completed, and the reason is beside the control (no call is possible)
    expect(req.get_by_test_id("ai-guidance-start")).to_be_disabled()
    expect(req.get_by_test_id("ai-guidance-why")).to_contain_text("dependency check")
    calls_before = model.calls
    req.get_by_test_id("ai-deps-start").click()
    expect(req.get_by_test_id("ai-deps")).to_have_attribute("data-status", "completed", timeout=WAIT)
    assert model.calls == calls_before + 1                                                       # exactly one model call for the check
    expect(req.get_by_test_id("ai-guidance-start")).to_be_enabled(timeout=WAIT)
    req.get_by_test_id("ai-guidance-start").click()
    expect(req.get_by_test_id("ai-guidance")).to_have_attribute("data-status", "completed", timeout=WAIT)
    assert model.calls == calls_before + 2
    expect(req.get_by_test_id("ai-guidance-summary")).to_contain_text("Raise the threshold")
    expect(req.get_by_test_id("rev-effect").first).to_contain_text("Cases ")                   # the server's facts stand beside the AI text
    expect(req.get_by_test_id("ai-guidance-removed-item").first).to_have_text(FALSE_COVER)          # removed by the server, shown struck through
    expect(req.get_by_test_id("ai-guidance-removed")).to_contain_text("Removed by the server")
    assert "now fall under" not in req.get_by_test_id("ai-guidance-summary").inner_text()
    assert req.get_by_test_id("ai-guidance-start").count() == 0 and req.get_by_test_id("ai-deps-start").count() == 0   # done: nothing to re-ask
    review = req.get_by_test_id("chg-review").inner_text()
    assert "rule-" not in review and ".yaml" not in review
    link = req.url
    # a viewer cannot reach someone else's request, so no AI step can be asked
    v = sign_in(pw_browser, server, env.ws, viewer, "cv")
    v.goto(link)
    assert v.get_by_test_id("ai-deps-start").count() == 0 and v.get_by_test_id("ai-guidance-start").count() == 0
    assert model.calls == calls_before + 2


@pytest.fixture
def prod_roles(env, ws, monkeypatch, tmp_path):            # noqa: F811
    """The server with the production role allowlist (config/ai_closed_loop/final_budgets.production.example.yaml): the Pitch-era kinds are not enabled."""
    import yaml
    from tests.pitch_brief.conftest import REPO
    roles = yaml.safe_load((REPO / "config/ai_closed_loop/final_budgets.production.example.yaml").read_text(encoding="utf-8"))["enabled_roles"]
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("enabled_roles: all", "enabled_roles: [" + ", ".join(roles) + "]")
    assert "enabled_roles: all" not in cfg
    path = tmp_path / "budget_prod_roles.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    server = ServerThread().start()
    yield env, server
    server.stop()


def test_with_the_production_allowlist_the_changes_page_names_the_equivalent_business_change_step(prod_roles, pw_browser):
    env, server = prod_roles
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash="changes")
    note = req.get_by_test_id("flow-ai-not-enabled")
    expect(note).to_be_visible(timeout=WAIT)
    link = note.get_by_test_id("ai-equivalent").get_by_role("link")
    expect(link).to_have_attribute("href", f"#/ws/{env.ws}/questions?ui_section=change")
    link.click()
    expect(req.get_by_test_id("qsection-change")).to_be_visible(timeout=WAIT)
    expect(req.get_by_test_id("chg-input")).to_be_visible()                                       # the enabled path: the request box of the business change flow
