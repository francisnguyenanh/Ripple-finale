"""SUR-05 browser journey on the React build (/app-next/): the proposed correspondence of a source refresh inside "Update source files". Real server (uvicorn thread), live
worker, real Chromium, real routes, store and ledger on the throwaway PostgreSQL; SYNTHETIC model (the SUR-05 stub through the production pinned-call path). Proves: nothing
is requested on opening, the proposal shows before / after quotes with the server's proof facts and is never a rename proof, refused proposals and unknowns stay visible, and
only the person links two files, by reviewing the refresh again with a human-confirmed proof and a reason (no model call, nothing published). Not model quality, not human
acceptance."""
from __future__ import annotations

import base64
import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import use  # noqa: E402
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_sur05_correspondence import GUIDE, HANDBOOK, WORKFLOW, WORKFLOW2, YAML, corr_env, corr_model  # noqa: E402,F401
from tests.pitch_brief.conftest import LiveWorker, harbor_files  # noqa: E402


def _files():
    out = []
    for p, content in dict(harbor_files()).items():                                                  # the same mutation as the route test's prepare()
        if p == GUIDE:
            p, content = HANDBOOK, b"Moved handbook for the finance desk.\n\n" + content.replace(b"\n\n", b"\n\nIn short: ", 1)
        elif p == WORKFLOW:
            p = WORKFLOW2
        elif p == YAML:
            content = content.replace(b"standard_manager_threshold_jpy: 1000000", b"standard_manager_threshold_jpy: 1500000")
        out.append({"name": p, "mimeType": "text/plain", "buffer": content})
    return out


def _shot(page, name):
    target = os.environ.get("UI_NEXT_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, f"sur05_{name}.png"), full_page=False)


def test_a_proposed_correspondence_is_shown_as_a_proposal_and_only_the_person_links_files(corr_env, pw_browser):
    q = corr_env
    env = q.env
    model = use(q, corr_model)
    worker = LiveWorker(env).start()
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash="sources")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.get_by_test_id("field-refresh-files").set_input_files(_files())
        page.get_by_test_id("action-refresh-prepare").click()
        expect(page.locator('[data-testid="refresh-diff"]').first).to_be_visible(timeout=WAIT)
        corr = page.get_by_test_id("corr")
        expect(corr).to_have_attribute("data-status", "not_requested", timeout=WAIT)
        assert model.calls == 0                                                                        # opening and reviewing make no model call
        corr.scroll_into_view_if_needed()
        _shot(page, "1_not_requested")
        corr.get_by_test_id("corr-request").click()
        expect(corr).to_have_attribute("data-status", "done", timeout=90000)
        pairs = corr.locator('[data-testid="corr-pair"]')
        expect(pairs).to_have_count(3)
        assert model.calls == 1
        for i in range(3):
            assert pairs.nth(i).get_attribute("data-rename-proof") == "false"
            expect(pairs.nth(i)).to_contain_text("not a rename proof")
            expect(pairs.nth(i).get_by_test_id("corr-before")).to_be_visible()
        rewritten = corr.locator('[data-testid="corr-pair"][data-relation="moved_rewritten"]')
        expect(rewritten.get_by_test_id("corr-proof")).to_have_attribute("data-proof", "human_confirmed_with_reason")
        expect(corr.locator('[data-testid="corr-pair"][data-relation="moved_same_content"]').get_by_test_id("corr-proof")).to_have_attribute("data-proof", "content_hash")
        expect(corr.get_by_test_id("corr-rejected")).to_contain_text("5 proposal(s) were refused")
        expect(page.locator('[data-testid="refresh-diff"][data-kind="renamed"]')).to_have_count(0)    # the proposal linked nothing
        rewritten.scroll_into_view_if_needed()
        _shot(page, "2_proposals")
        link = rewritten.get_by_test_id("corr-link")
        expect(link).to_be_disabled()                                                                  # a reason is required: the person's decision, not the model's
        rewritten.get_by_test_id("corr-reason-input").fill("The handbook is the guide moved to the desk folder with one added line; I checked both files.")
        link.click()
        renamed = page.locator('[data-testid="refresh-diff"][data-kind="renamed"]')
        expect(renamed).to_have_count(1, timeout=WAIT)
        expect(renamed).to_contain_text(f"{GUIDE} -> {HANDBOOK}")
        expect(corr.locator('[data-testid="corr-pair"][data-relation="moved_rewritten"]').get_by_test_id("corr-linked")).to_be_visible()
        expect(page.get_by_test_id("refresh-status")).to_have_attribute("data-state", "PREPARED")     # nothing published; the person still decides to publish
        assert model.calls == 1
        _shot(page, "3_linked_by_person")
        assert not page._errors, page._errors
        page.context.close()
    finally:
        server.stop()
        worker.stop.set()
