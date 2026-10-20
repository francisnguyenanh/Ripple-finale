"""U1/U5 in the browser (stub providers only: SIMULATED): the Sources page shows each stage of each file from the real store and offers the
right button for its state; "Update source files" warns about deletions and needs an acknowledgement; the workspace is explained;
an administrator creates another workspace and a requester cannot. Screenshots go to UI_NEXT_SHOTS."""
from __future__ import annotations

import os
import pathlib

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.test_intent_chat import kinds  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000
SHOTS = os.environ.get("UI_NEXT_SHOTS")


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, who, w=1280, h=900):
    ctx = browser.new_context(base_url=server.url, viewport={"width": w, "height": h})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    open_app(page, server.url)
    login(page, *who)
    return page, ctx


def _shot(page, name):
    if SHOTS:
        pathlib.Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(pathlib.Path(SHOTS) / name), full_page=True)


def _row(page, name):
    return page.locator(f'[data-testid="artifact-row"][data-logical-path$="{name}"]')


def test_each_file_shows_its_own_stages_and_the_right_button(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Sources")
        expect(tid(page, "lifecycle-summary")).to_be_visible()
        total = len(ch.arts)
        assert f"{total}" in tid(page, "source-count").inner_text()
        expect(tid(page, "count-analysed")).to_contain_text(f"{len(ch.jobs)} of {total}")
        expect(tid(page, "count-not-analysed")).to_contain_text(str(total - len(ch.jobs)))
        expect(tid(page, "subset-note")).to_contain_text("selected part of the public Harbor demo files")
        expect(tid(page, "provider-line")).to_contain_text("Not declared")
        expect(tid(page, "knowledge-state")).to_be_visible()
        approval = _row(page, "approval.yaml")
        expect(approval.get_by_test_id("stage-imported")).to_be_visible()
        expect(approval.get_by_test_id("stage-parsed")).to_have_attribute("data-state", "parsed")
        expect(approval.get_by_test_id("stage-analysis")).to_have_attribute("data-state", "analysed")
        text = approval.get_by_test_id("stage-rules-text").inner_text()
        assert "0 confirmed" in text and "not confirmed or rejected as a whole" in text, text
        expect(approval.get_by_role("button", name="Re-analyze")).to_be_visible()
        expect(approval.get_by_role("button", name="Analyze", exact=True)).to_have_count(0)
        never = _row(page, "calculations.py")
        expect(never.get_by_test_id("stage-analysis")).to_have_attribute("data-state", "not_analysed")
        expect(never.get_by_role("button", name="Analyze", exact=True)).to_be_visible()
        expect(never.get_by_role("button", name="Re-analyze")).to_have_count(0)
        assert_business_clean(page, "Sources lifecycle")
        _shot(page, "u1_sources_1280.png")
        # Re-analyze asks first and starts nothing until confirmed
        before = kinds(env).get("extraction", 0)
        approval.get_by_role("button", name="Re-analyze").click()
        expect(tid(page, "reanalyze-confirm")).to_be_visible()
        page.wait_for_timeout(600)
        assert kinds(env).get("extraction", 0) == before, "asking must not start an analysis"
        tid(page, "reanalyze-confirm").get_by_role("button", name="Cancel").click()
        expect(tid(page, "reanalyze-confirm")).to_have_count(0)
        # Analyze on a never-analysed file: one click, one job (a double click must not make two)
        never.get_by_role("button", name="Analyze", exact=True).dblclick()
        expect(tid(page, "job-panel")).to_be_visible(timeout=WAIT)
        page.wait_for_timeout(800)
        assert kinds(env).get("extraction", 0) == before + 1, kinds(env)
        goto_view(page, "Sources")
        never = _row(page, "calculations.py")
        expect(never.get_by_test_id("stage-analysis")).to_have_attribute("data-state", "pending_approval", timeout=WAIT)
        expect(never.get_by_test_id("view-analysis-status")).to_be_visible()
        expect(never.get_by_role("button", name="Analyze", exact=True)).to_have_count(0)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_a_missing_file_is_a_deletion_and_needs_an_acknowledgement(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Sources")
        expect(tid(page, "refresh-consequence")).to_be_visible()                                       # the deletion consequence stays visible (EUI-01)
        expect(tid(page, "refresh-consequence")).to_contain_text("counts as deleted")
        expect(tid(page, "refresh-steps")).not_to_be_visible()                                           # the routine walkthrough is collapsed
        tid(page, "refresh-how").locator("summary").click()
        expect(tid(page, "refresh-steps")).to_contain_text("Publish the new version on purpose")
        expect(tid(page, "refresh-panel")).to_contain_text("Update source files")
        assert "Manual source refresh" not in page.inner_text("main")
        tid(page, "field-refresh-files").set_input_files({"name": "approval.yaml", "mimeType": "application/yaml", "buffer": b"manager_threshold_jpy: 900000\n"})
        tid(page, "action-refresh-prepare").click()
        expect(tid(page, "refresh-deleted-warning")).to_be_visible()
        expect(tid(page, "refresh-deleted-warning")).to_contain_text("deleted from the new version")
        assert page.locator('[data-testid="refresh-diff"][data-kind="deleted"]').count() == len(ch.arts)
        commit = tid(page, "action-refresh-commit")
        expect(commit).to_be_disabled()
        tid(page, "refresh-ack").check()
        expect(commit).to_be_enabled()
        _shot(page, "u1_refresh_1280.png")
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_the_workspace_is_explained_and_an_administrator_can_start_another(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Support")
        expect(tid(page, "workspace-about")).to_be_visible()
        expect(tid(page, "workspace-source-count")).to_contain_text(str(len(ch.arts)))
        expect(tid(page, "workspace-terms")).to_contain_text("It is not one file, one question or one writing session")
        expect(tid(page, "workspace-terms")).to_contain_text("does not create a budget")
        steps = {s.get_attribute("data-step"): s.get_attribute("data-allowed") for s in tid(page, "workspace-step").all()}
        assert steps == {"read": "true", "propose": "true", "confirm": "true", "approve": "true"}
        expect(tid(page, "workspace-members")).to_contain_text(env.req)
        assert_business_clean(page, "Support: workspace")
        _shot(page, "u1_support_1280.png")
        tid(page, "workspace-create-open").click()
        tid(page, "workspace-name").fill("Harbor Treasury")
        tid(page, "workspace-description").fill("Cash policy, limits and the treasury desk workbook.")
        expect(tid(page, "workspace-create-submit")).to_be_enabled()
        tid(page, "workspace-member").locator(f'xpath=self::*[@data-account="{env.req}"]').check()
        tid(page, "workspace-create-submit").dblclick()
        expect(tid(page, "workspace-created")).to_be_visible(timeout=WAIT)
        options = [o.inner_text() for o in page.get_by_test_id("ws-select").locator("option").all()]
        assert "Harbor Treasury" in options, options
        tid(page, "workspace-created-open").click()
        expect(tid(page, "state-empty")).to_be_visible(timeout=WAIT)
        expect(tid(page, "state-empty")).to_contain_text("No source has been imported")
        with env.store._tx() as cur:
            cur.execute("SELECT count(*) FROM v2_workspaces WHERE tenant_id=%s AND display_name='Harbor Treasury'", (env.tenant,))
            assert cur.fetchone()[0] == 1, "a double click made two workspaces"
        assert not page._errors, page._errors
        ctx.close()
        # a requester sees the explanation, what they may do, and no way to create or manage
        page, ctx = _open(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        goto_view(page, "Support")
        expect(tid(page, "workspace-about")).to_be_visible()
        expect(tid(page, "workspace-create")).to_have_count(0)
        expect(tid(page, "workspace-members")).to_have_count(0)
        steps = {s.get_attribute("data-step"): s.get_attribute("data-allowed") for s in tid(page, "workspace-step").all()}
        assert steps["confirm"] == "false" and steps["propose"] == "true", steps
        ctx.close()
    finally:
        server.stop()
