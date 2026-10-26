"""SCN-11 contextual empty states on changed surfaces (real server, Chromium, routes on the throwaway PostgreSQL; stub providers only; reads only):
a filter that hides everything says so and offers to clear it (scope kept); a true empty list says what is known and offers an allowed next step (a link, no
mutation); failures keep their own unavailable states (covered by the pilot and partial-read tests)."""
from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _page(browser, server, env, acct, cred, path):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._posts = []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" and "/auth/" not in r.url else None)
    open_app(page, server.url)
    login(page, acct, cred)
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/{path}")
    return page, ctx


def test_no_match_offers_to_clear_and_a_true_empty_list_offers_an_allowed_step(env, ch, pw_browser):
    from infra.adapters.v2_identity import PostgresIdentityStore
    viewer = f"vw-{uuid4().hex[:8]}"
    ids = PostgresIdentityStore(env.dsn)
    ids.create_account(viewer, "cv")
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, viewer, ("viewer",))
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, env, env.admin, f"cred-a-{env.nonce}", "explorer")
        tid(page, "explorer-mode").click()
        expect(tid(page, "graph-svg")).to_be_visible(timeout=WAIT)
        tid(page, "explorer-search").fill("zz-nothing-matches-this")
        empty = tid(page, "state-empty")
        expect(empty).to_have_attribute("data-empty", "no-match")
        expect(empty).to_contain_text("No item matches the filter")
        tid(page, "explorer-clear-filter").click()
        expect(tid(page, "graph-svg")).to_be_visible()
        expect(tid(page, "explorer-search")).to_have_value("")
        ctx.close()
        vp, vctx = _page(pw_browser, server, env, viewer, "cv", "overview")
        we = tid(vp, "work-empty")
        expect(we).to_have_attribute("data-empty", "none", timeout=WAIT)
        expect(we).to_contain_text("loaded list")                                 # never "the workspace is empty"
        tid(vp, "work-empty-open").click()
        expect(tid(vp, "page-title")).to_have_text("Changes")
        assert not [u for u in vp._posts if "/v2/" in u], vp._posts
        vctx.close()
    finally:
        server.stop()
