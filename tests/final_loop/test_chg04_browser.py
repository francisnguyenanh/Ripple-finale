"""CHG-04 browser journey on the React build (/app-next/): the requester states and confirms a change and sees what the rule means, what it touches and what stays pending;
a separate approver accepts the knowledge change and (separately) the automatic file edits; a clarification revises the draft and makes the approval out of date; a viewer cannot decide.

Real server (uvicorn thread), real worker thread, real Chromium, real routes and store on the throwaway PostgreSQL. The understanding comes from a synthetic stub model; the
accounts are SIMULATED roles. Nothing here is human acceptance or a statement about model quality.
"""
from __future__ import annotations

import pytest


def _shot(page, name):                                                                       # optional review capture (UI_NEXT_SHOTS); never affects the assertions
    import os
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"{name}.png"), full_page=False)

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.adapters.v2_identity import PostgresIdentityStore  # noqa: E402
from infra.worker import explain  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj  # noqa: E402
from tests.final_loop.test_kno01_business_store import bind, ws  # noqa: E402,F401
from tests.final_loop.test_kno03_graph_gaps import seed  # noqa: E402
from tests.final_loop.test_kno05_accept import accept, answer  # noqa: E402
from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE  # noqa: E402
from tests.final_loop.test_qry03_qa import Model  # noqa: E402
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, LiveWorker, drain  # noqa: E402


@pytest.fixture
def ui(env, ws, monkeypatch, tmp_path):                   # noqa: F811
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("  intent.explain: gemini-3.5-flash-lite", "  intent.explain: gemini-3.5-flash-lite\n  knowledge.qa: gemini-3.8-flash\n  change.understand: gemini-3.8-flash\n  change.draft_guidance: gemini-3.8-flash")
    path = tmp_path / "budget_ui.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    model = Model(lambda prompt: model_obj(ops=[OP_VALUE]))
    monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    seed(env, ws, rules=bind(KNOWLEDGE["observed_rules"] + DISTRACTORS, ws.source_revision_id))
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "conflicting_facts")
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "Finance reviews CAPEX from 2,000,000 JPY.")
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    ids = PostgresIdentityStore(env.dsn)
    approver, viewer = f"appr-{env.nonce}", f"view-{env.nonce}"
    ids.create_account(approver, "ca")
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, approver, ("change_approver", "reviewer"))
    ids.create_account(viewer, "cv")
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, viewer, ("viewer",))
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    yield env, admin, model, server, approver, viewer
    server.stop()
    worker.stop.set()


def test_requester_confirms_and_sees_the_meaning_an_approver_decides_separately_and_a_clarification_revises(ui, pw_browser):
    env, admin, model, server, approver, viewer = ui
    knowledge_before = admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["accepted"]
    # 1. the requester states a change, sees what was understood and confirms it
    req = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}")
    req.get_by_test_id("chg-input").fill(NUM_REQ)
    req.get_by_test_id("chg-submit").click()
    expect(req.get_by_test_id("chg-understanding")).to_have_attribute("data-status", "READY", timeout=WAIT)
    req.get_by_test_id("chg-confirm").click()
    expect(req.get_by_test_id("chg-confirmed")).to_be_visible(timeout=WAIT)
    review = req.get_by_test_id("chg-review")
    expect(review).to_be_visible(timeout=WAIT)
    expect(review).to_have_attribute("data-status", "PARTIAL")
    req.set_viewport_size({"width": 1440, "height": 900})                                          # EUI-01: decisions rail beside the evidence on a wide screen
    assert req.get_by_test_id("rev-rail").bounding_box()["x"] > req.get_by_test_id("rev-evidence").bounding_box()["x"] + 300
    assert req.get_by_test_id("rev-rail").get_by_test_id("rev-decisions").count() == 1
    _shot(req, "eui01_review_rail_1440")
    meaning = req.get_by_test_id("rev-meaning")
    expect(meaning).to_contain_text("Before:")
    expect(meaning).to_contain_text("After:")
    threshold = req.locator('[data-testid="rev-aspect"][data-aspect="threshold"]')                # EUI-03: the change as typed aspects (the sentence stays in Technical)
    expect(threshold).to_contain_text("Value (")
    expect(threshold).to_contain_text("→")
    assert req.get_by_test_id("rev-change").count() == 0
    expect(req.get_by_test_id("rev-affected").first).to_contain_text("changed by this request")
    expect(req.get_by_test_id("rev-counts")).to_contain_text("Automatic edits possible: 1")
    expect(req.get_by_test_id("rev-counts")).to_contain_text("Unresolved:")
    expect(req.get_by_test_id("rev-op")).to_have_attribute("data-coverage", "partial")          # the policy text named by the knowledge is not in this import: pending, not "no impact"
    expect(req.get_by_test_id("rev-withheld")).to_be_visible()                                  # the requester has no evidence rights: file details withheld
    assert "rule-" not in review.inner_text() and ".yaml" not in review.inner_text()
    expect(req.get_by_test_id("rev-no-decisions")).to_be_visible()
    assert req.get_by_test_id("rev-decide").count() == 0                                        # the requester cannot decide
    link = req.url
    # 2. a viewer sees nothing of someone else's request
    v = sign_in(pw_browser, server, env.ws, viewer, "cv")
    v.goto(link)
    expect(v.get_by_test_id("chg-error")).to_be_visible(timeout=WAIT)
    # 3. the approver reads the same request, accepts the knowledge change; the file edits stay a separate decision
    a = sign_in(pw_browser, server, env.ws, approver, "ca")
    a.goto(link)
    expect(a.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    expect(a.get_by_test_id("rev-file").first).to_be_visible()                                    # evidence rights: paths and instructions shown
    assert "approval.yaml" in a.get_by_test_id("rev-files").inner_text()
    a.get_by_test_id("rev-file-diff-show").click()                                               # EUI-03: the candidate byte diff, read on request, nothing applied
    fd = a.get_by_test_id("rev-file-diff")
    expect(fd).to_be_visible(timeout=WAIT)
    expect(fd.locator('[data-testid="rev-file-diff-file"][data-state="candidate"]')).to_have_count(1)
    expect(fd.locator('[data-testid="diff-line"][data-kind="remove"]')).to_have_count(1)
    expect(fd.locator('[data-testid="diff-line"][data-kind="add"]')).to_have_count(1)
    expect(fd).to_contain_text("candidate, not applied")
    expect(a.get_by_test_id("rev-file-diff-coverage")).to_contain_text("no byte diff for these")
    fd.scroll_into_view_if_needed()
    _shot(a, "eui03_candidate_file_diff_1440")
    expect(a.get_by_test_id("rev-approve")).to_be_disabled()                                     # a reason is required
    a.get_by_test_id("rev-reason").fill("Checked with Finance; the new threshold is right.")
    a.get_by_test_id("rev-approve").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Knowledge change: Accepted", timeout=WAIT)
    dec = a.locator("[data-testid=rev-decision][data-kind=knowledge]")
    expect(dec).to_have_count(1)
    expect(dec).to_have_attribute("data-current", "true")
    expect(a.get_by_test_id("rev-current-knowledge")).to_contain_text("Accepted")
    assert a.get_by_test_id("rev-current-files").count() == 0                                   # accepting the meaning approved no file edit
    expect(a.get_by_test_id("rev-approve")).to_be_disabled()                                     # already decided for this draft version
    a.get_by_test_id("mode-technical").click()
    expect(a.get_by_test_id("rev-technical")).to_contain_text("semantic diff", timeout=WAIT)
    a.get_by_test_id("mode-business").click()
    # 4. the requester clarifies an open item: the draft is revised and the approval is out of date
    req.reload()
    expect(req.get_by_test_id("chg-review")).to_be_visible(timeout=WAIT)
    expect(req.locator("[data-testid=rev-decision][data-kind=knowledge]")).to_have_attribute("data-current", "true", timeout=WAIT)
    opener = req.get_by_test_id("rev-clarify-open").first                                          # EUI-06: the form opens under the item it is about
    ref = opener.get_attribute("data-ref")
    opener.click()
    form = req.locator(f'[data-testid="rev-clarify"][data-ref="{ref}"]')
    expect(form).to_contain_text("Clarifying:")
    assert req.get_by_test_id("rev-clarify").count() == 1                                          # one open form at a time
    form.get_by_test_id("rev-clarify-disposition").select_option("answered")
    expect(form.get_by_test_id("rev-clarify-submit")).to_be_disabled()                             # an answer needs words
    form.get_by_test_id("rev-clarify-disposition").select_option("manual_later")
    form.get_by_test_id("rev-clarify-submit").click()
    expect(req.get_by_test_id("rev-note")).to_contain_text("revised", timeout=WAIT)
    assert req.get_by_test_id("rev-rail").get_by_test_id("rev-note").count() == 1                 # the outcome shows in the rail, beside the controls
    expect(req.locator("[data-testid=rev-decision][data-kind=knowledge]")).to_have_attribute("data-current", "false", timeout=WAIT)
    expect(req.locator("[data-testid=rev-decision][data-kind=clarification]")).to_have_count(1)
    # 5. the approver sees the stale decision, decides again for the revised draft and approves the file edits separately
    a.reload()
    expect(a.locator("[data-testid=rev-decision][data-kind=knowledge]")).to_contain_text("out of date", timeout=WAIT)
    a.get_by_test_id("rev-reason").fill("Approved again for the revised draft.")
    a.get_by_test_id("rev-approve").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Knowledge change: Accepted", timeout=WAIT)
    a.get_by_test_id("rev-reason").fill("The YAML edit is correct.")
    a.get_by_test_id("rev-approve-files").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Automatic file edits: Accepted", timeout=WAIT)
    expect(a.get_by_test_id("rev-current-files")).to_contain_text("Accepted")
    assert a.locator("[data-testid=rev-decision]").count() == 4                                   # approve, clarification, approve, file edits
    assert admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["accepted"] == knowledge_before                    # decisions committed no knowledge
    assert model.calls == 1                                                                      # one understanding call; the review and the decisions asked no model
    for page in (req, a, v):
        assert not page._errors, page._errors
