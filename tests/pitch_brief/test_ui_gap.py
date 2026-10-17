"""Browser: the GAP loop and the preview card as a person sees them. Requester comments (not authoritative), the admin
confirms (a decision), the old draft turns stale, the requester regenerates under their own AI permission (nobody approves the
model call), the new draft states which confirmed answers it used. Stub provider; no sandbox config (ordinary rules)."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft  # noqa: E402
from tests.ai10ui.contract import select_workspace, tech_url, tid, UI_BASE# noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import drain, checks_ready  # noqa: E402
from tests.pitch_brief.test_change_draft import Model, _res, run  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 30000
QUESTION = "Should the emergency channel follow the same Manager limit?"


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(page, url):
    page.goto("about:blank")
    page.goto(tech_url(url))


def test_gap_loop_and_preview_in_the_browser(env, ch, monkeypatch, pw_browser):
    def mutate(obj, prompt):
        obj["questions_for_human"] = [QUESTION]
    model = Model(mutate=mutate)
    jid, body = run(env, ch, monkeypatch, model)
    intent_id = _res(body)["intent"]["intent_id"]
    server = ServerThread().start()
    try:
        requester = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        for p in (requester, admin):
            select_workspace(p, env.ws)
        url = f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={jid}"
        _open(requester, url)
        expect(tid(requester, "draft-status")).to_be_visible(timeout=WAIT)
        # preview: separates MODEL from SERVER and says plainly that no simulation exists
        for a, b in ((("AI", "server") if UI_BASE.startswith("/app-next") else ("MODEL", "SERVER")),):     # the new page says it in words, the legacy page with tags
            expect(tid(requester, "flow-preview")).to_contain_text(a)
            expect(tid(requester, "flow-preview")).to_contain_text(b)
        expect(tid(requester, "preview-simulation")).to_have_attribute("data-state", "UNAVAILABLE")
        # related documents: four groups that are never mixed; the YAML edit is "impacted, with evidence" and unchanged until apply
        docs = tid(requester, "preview-docs")
        expect(docs).to_contain_text("not proven dependencies" if UI_BASE.startswith("/app-next") else "not causal dependencies")
        expect(tid(requester, "preview-docs-evidence")).to_contain_text("approval.yaml")
        expect(tid(requester, "preview-docs-evidence")).to_contain_text("will change only after you apply" if UI_BASE.startswith("/app-next") else "unchanged until you apply")
        assert int(tid(requester, "preview-docs-confirm").get_attribute("data-count")) >= 1
        assert int(tid(requester, "preview-docs-uncovered").get_attribute("data-count")) >= 1
        expect(tid(requester, "preview-docs-unchanged")).to_contain_text("a person edits it")
        gap = requester.locator("[data-testid=gap-item][data-kind=question]")
        expect(gap).to_contain_text(QUESTION)
        expect(gap.get_by_test_id("gap-state")).to_have_attribute("data-state", "unresolved")
        # requester: may comment, may NOT confirm (no button), nothing becomes authoritative
        assert gap.get_by_test_id("gap-confirm").count() == 0
        gap.get_by_test_id("gap-text").fill("I think emergency keeps the old limit")
        gap.get_by_test_id("gap-comment-save").click()
        expect(gap.get_by_test_id("gap-state")).to_have_attribute("data-state", "commented", timeout=WAIT)
        expect(gap.get_by_test_id("gap-comment")).to_contain_text("not authoritative")
        # admin: confirms an answer as a decision
        _open(admin, url)
        agap = admin.locator("[data-testid=gap-item][data-kind=question]")
        agap.get_by_test_id("gap-text").fill("Emergency follows the new Manager limit.")
        agap.get_by_test_id("gap-why").fill("agreed with the approver")
        agap.get_by_test_id("gap-confirm").click()
        expect(agap.get_by_test_id("gap-state")).to_have_attribute("data-state", "confirmed", timeout=WAIT)
        expect(agap.get_by_test_id("gap-confirmation")).to_contain_text(env.admin)
        expect(tid(admin, "draft-stale")).to_be_visible()
        # requester regenerates under their own AI permission (admitted by policy, no approve button); the new draft states what it used
        monkeypatch.setattr(change_draft, "_client_for_model", lambda m: Model())
        _open(requester, url)
        checks_ready(env)
        tid(requester, "action-run-draft").click()
        expect(tid(requester, "state-draft")).to_have_attribute("data-state", "APPROVED", timeout=WAIT)
        url2 = requester.url
        assert tid(requester, "action-approve-draft").count() == 0
        drain(env)
        _open(requester, url2)
        expect(tid(requester, "clarifications-used")).to_contain_text(env.admin, timeout=WAIT)
        assert tid(requester, "draft-stale").count() == 0
        for p in (requester, admin):
            assert not p._errors, p._errors
    finally:
        server.stop()
