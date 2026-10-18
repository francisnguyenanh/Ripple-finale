"""UX-10: when the file lifecycle read fails, Sources says so (instead of silently showing files without stages) and every file is still
listed; Try again re-reads. Read failure injected in the browser; stub providers only; nothing is started."""
from __future__ import annotations

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


def _open(browser, server, env, fail):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._posts, page._life = [], []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" else None)
    page.on("request", lambda r: page._life.append(r.url) if "/v2/sources/lifecycle" in r.url else None)
    if fail:
        page.route("**/v2/sources/lifecycle*", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}'))
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources")
    return page, ctx


def test_failed_lifecycle_read_is_announced_and_files_stay_listed(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, True)
        expect(tid(page, "lifecycle-unavailable")).to_be_visible(timeout=WAIT)
        assert tid(page, "artifact-row").count() >= 1, "every file must still be listed"
        n = len(page._life)
        tid(page, "lifecycle-retry").click()
        page.wait_for_timeout(800)
        assert len(page._life) > n, "Try again must re-read"
        assert not [u for u in page._posts if "/v2/jobs" in u], page._posts
        ctx.close()
    finally:
        server.stop()


def test_working_lifecycle_has_no_notice_and_no_filter_for_a_short_list(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, False)
        expect(tid(page, "artifact-row").first).to_be_visible(timeout=WAIT)
        assert tid(page, "lifecycle-unavailable").count() == 0
        if tid(page, "artifact-row").count() <= 8:
            assert tid(page, "files-filter").count() == 0
        ctx.close()
    finally:
        server.stop()
