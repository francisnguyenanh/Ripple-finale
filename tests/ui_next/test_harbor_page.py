"""Harbor try-out page (browser; no provider): one requisition through the pinned Harbor code under two versions of the configuration, side by side."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.conftest import drain, import_harbor  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000


def test_a_requisition_is_routed_by_the_pinned_code_and_the_page_says_what_it_is(env, pw_browser):
    admin = env.admin_client()
    import_harbor(env, admin)
    drain(env)
    server = ServerThread().start()
    try:
        for technical in (False, True):
            ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.set_default_timeout(WAIT)
            open_app(page, server.url)
            login(page, env.admin, f"cred-a-{env.nonce}")
            select_workspace(page, env.ws)
            page.goto(f"{server.url}/app-next/#/ws/{env.ws}/harbor" + ("?mode=technical" if technical else ""))
            expect(page.get_by_test_id("harbor-claim")).to_contain_text("pinned public Harbor code")
            page.get_by_test_id("harbor-in-net_amount_jpy").fill("1200000")
            expect(page.get_by_test_id("harbor-run")).to_be_enabled(timeout=WAIT)
            page.get_by_test_id("harbor-run").click()
            expect(page.get_by_test_id("harbor-route-before")).to_have_text("needs a manager", timeout=WAIT)
            expect(page.get_by_test_id("harbor-route-after")).to_have_text("needs a manager")
            expect(page.get_by_test_id("harbor-result")).to_contain_text("Same result under both versions")
            if technical:
                assert "approval.yaml sha256" in page.get_by_test_id("harbor-tech-before").inner_text()
                assert "app/routing.py" in page.get_by_test_id("harbor-code-rev").inner_text()
            else:
                assert page.get_by_test_id("harbor-tech-before").count() == 0
                assert_business_clean(page, "Harbor try-out")
            ctx.close()
    finally:
        server.stop()
