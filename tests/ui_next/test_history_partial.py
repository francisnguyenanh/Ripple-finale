"""UX-13: a failed activity read on History is shown as unavailable, never as "Nothing has happened yet"; the version list (a separate
read) still works; Try again re-reads once. Read failures are injected in the browser. Stub providers only; nothing is mutated."""
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
    page._posts, page._audit = [], []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" else None)
    page.on("request", lambda r: page._audit.append(r.url) if "/v2/audit/" in r.url else None)
    if fail:
        page.route("**/v2/audit/*", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}'))
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/history")
    return page, ctx


def test_failed_activity_read_is_unavailable_not_empty(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, True)
        expect(tid(page, "history-unavailable")).to_be_visible(timeout=WAIT)
        assert "Nothing has happened yet" not in page.inner_text("main")
        expect(page.get_by_role("heading", name="Versions of the files")).to_be_visible()
        n = len(page._audit)
        tid(page, "history-retry").click()
        page.wait_for_timeout(800)
        assert len(page._audit) == n + 1, "Try again must re-read exactly once"
        assert not [u for u in page._posts if "/v2/jobs" in u or "/restore" in u], page._posts
        ctx.close()
    finally:
        server.stop()


def test_working_activity_read_has_no_unavailable_notice(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, False)
        expect(tid(page, "history-activity")).to_be_visible(timeout=WAIT)
        assert tid(page, "history-unavailable").count() == 0
        ctx.close()
    finally:
        server.stop()


def test_filter_appears_only_for_a_long_loaded_window_and_narrows_without_requests(env, ch, pw_browser):
    audit = env.admin_client().get(f"/v2/audit/{env.ws}").json().get("events", [])
    listed = [e for e in audit if e.get("event_type") not in ("JOB_QUEUED", "membership.grant")][-40:]
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, False)
        expect(tid(page, "history-activity")).to_be_visible(timeout=WAIT)
        has_filter = tid(page, "history-filter").count() == 1
        assert has_filter == (len(listed) > 8), (has_filter, len(listed))
        if has_filter:
            n = len(page._audit)
            actors = sorted({str(e.get("actor_id")) for e in listed if e.get("actor_id")})
            assert actors, "expected at least one named actor"
            tid(page, "history-filter-actor").select_option(actors[0])
            expect(tid(page, "history-filter-count")).to_be_visible()
            expected = len([e for e in listed if str(e.get("actor_id")) == actors[0]])
            assert page.locator("[data-event]").count() == expected
            tid(page, "history-filter-text").fill("zzz-no-such-event")
            expect(tid(page, "history-filter-none")).to_be_visible()
            tid(page, "history-filter-text").fill("")
            tid(page, "history-filter-actor").select_option("")
            assert page.locator("[data-event]").count() == len(listed)
            assert len(page._audit) == n, "filtering must not request anything"
        ctx.close()
    finally:
        server.stop()


def test_an_event_without_a_wording_is_named_neutrally_and_its_code_stays_technical(env, ch, pw_browser):
    """Review F5: an audit code the page has no words for is never turned into prose ("chat ai allowed by policy"); the Business view names it
    neutrally and the Technical view keeps the code. The real audit read is extended in the browser with one worded and one unknown code."""
    import json

    def extend(route):
        resp = route.fetch()
        body = resp.json()
        body["events"] = list(body.get("events") or []) + [
            {"event_type": "CHAT_AI_ALLOWED_BY_POLICY", "actor_id": "a-policy", "created_at": "2026-10-18T00:00:00+00:00", "payload": {}},
            {"event_type": "SOME_FUTURE_EVENT", "actor_id": "a-future", "created_at": "2026-10-18T00:00:01+00:00", "payload": {}}]
        route.fulfill(response=resp, body=json.dumps(body), content_type="application/json")

    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        page.route("**/v2/audit/*", extend)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/history")
        expect(page.locator("[data-event=SOME_FUTURE_EVENT]")).to_contain_text("recorded an event", timeout=WAIT)
        expect(page.locator("[data-event=CHAT_AI_ALLOWED_BY_POLICY]")).to_contain_text("under the workspace AI policy")
        business = tid(page, "history-activity").inner_text().lower()
        assert "some future event" not in business and "chat ai allowed by policy" not in business and "SOME_FUTURE_EVENT" not in tid(page, "history-activity").inner_text()
        page.get_by_test_id("mode-technical").click()
        expect(page.locator("[data-event=SOME_FUTURE_EVENT]")).to_contain_text("SOME_FUTURE_EVENT")
        ctx.close()
    finally:
        server.stop()


def test_a_failed_comparison_is_unavailable_not_choose_two_versions(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(WAIT)
        calls = []
        def diff(route):
            calls.append(route.request.url)
            route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}')
        page.route("**/v2/history/*/diff*", diff)

        def two_versions(route):                                                # the fixture has one version: a second listed one makes the chooser appear
            if "/diff" in route.request.url:
                return route.fallback()
            res = route.fetch()
            body = res.json()
            revs = body.get("revisions") or []
            if len(revs) == 1:
                body["revisions"] = revs + [dict(revs[0], revision_id=revs[0]["revision_id"] + "-listed", sequence=(revs[0].get("sequence") or 1) + 1)]
            route.fulfill(response=res, json=body)
        page.route(f"**/v2/history/{env.ws}", two_versions)
        open_app(page, server.url)
        login(page, env.admin, f"cred-a-{env.nonce}")
        select_workspace(page, env.ws)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/history")
        expect(tid(page, "history-diff-from")).to_be_visible(timeout=WAIT)
        opts = tid(page, "history-diff-from").locator("option").evaluate_all("els => els.map(e => e.value).filter(Boolean)")
        assert len(opts) >= 2, opts
        tid(page, "history-diff-from").select_option(opts[0])
        tid(page, "history-diff-to").select_option(opts[1])
        expect(tid(page, "history-diff-unavailable")).to_be_visible(timeout=WAIT)
        assert "Choose two different versions" not in tid(page, "history-diff").inner_text()
        n = len(calls)
        tid(page, "history-diff-retry").click()
        expect(tid(page, "history-diff-unavailable")).to_be_visible(timeout=WAIT)
        assert len(calls) == n + 1, "Try again must re-read the comparison exactly once"
        ctx.close()
    finally:
        server.stop()
