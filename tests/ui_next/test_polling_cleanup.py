"""Request storm, polling and cleanup (stub providers only). Proves, from the browser's own network log and from the database:
1. an idle page makes a small, fixed number of requests and then none (no effect-dependency loop);
2. a page that is waiting for something polls at its documented interval, a bounded number of times;
3. leaving the page (navigation), switching workspace and signing out stop the polling of the page that was left;
4. the database connection count returns to its baseline after heavy page use (no leaked connection).
Waiting for ports to drain is only cleanup; none of these assertions depends on it."""
from __future__ import annotations

import time

import psycopg2
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000
PAGES = [("Overview", "overview-cta"), ("Sources", "artifact-row"), ("Explorer", "explorer-summary"), ("Questions", "questions-note"),
         ("Changes", "chat-panel"), ("Impact & Simulation", "flow-whatif"), ("History", "history-activity"), ("Support", "support-matrix")]


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _page(browser, server, who):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._log = []
    page._errors = []
    page.on("request", lambda r: page._log.append((time.monotonic(), r.method, r.url)) if "/v2/" in r.url else None)
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    open_app(page, server.url)
    login(page, *who)
    return page, ctx


def _since(page, mark):
    return [x for x in page._log if x[0] >= mark]


def _path(url):
    return url.split("?")[0].split("/v2/")[1]


def _connections(env):
    with psycopg2.connect(env.dsn) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
        return cur.fetchone()[0]


def test_an_idle_page_settles_to_zero_requests_on_every_page(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        for label, ready in PAGES:
            goto_view(page, label)
            expect(tid(page, ready).first).to_be_visible(timeout=WAIT)
            page.wait_for_timeout(2500)                                  # let the first load finish
            mark = time.monotonic()
            first = len([x for x in page._log if x[0] < mark])
            page.wait_for_timeout(7000)                                  # longer than every polling interval of the app (2 s, 6 s)
            later = _since(page, mark)
            reads = [x for x in later if x[1] == "GET"]
            assert not [x for x in later if x[1] != "GET"], (label, "an idle page wrote something", later[:3])
            # nothing is running or waiting here, so nothing may keep asking; allow one stray refresh at most
            assert len(reads) <= 2, (label, "idle page keeps requesting", [(m, _path(u)) for _, m, u in reads][:10])
            assert first < 60, (label, "too many requests to open one page", first)
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_a_waiting_page_polls_at_its_interval_a_bounded_number_of_times_and_stops_when_left(env, ch, pw_browser, monkeypatch):
    """The requester has a finished proposal and waits for ANOTHER person (the approver states the check): the page looks again every ~6 s,
    a bounded number of times; leaving the page stops it."""
    from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal
    c = chain_to_proposal(env, ch, monkeypatch)
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.req, f"cred-r-{env.nonce}"))
        select_workspace(page, env.ws)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/changes?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}")
        expect(tid(page, "next-action")).to_contain_text("approver", timeout=WAIT)             # waiting for a different person
        page.wait_for_timeout(1500)
        mark = time.monotonic()
        page.wait_for_timeout(13000)
        polls = [x for x in _since(page, mark) if x[1] == "GET" and "/head" in x[2]]
        assert 1 <= len(polls) <= 4, ("a page waiting for a person polls about every 6 s", len(polls))
        # leaving the page stops it: nothing about the flow is requested after navigation settles
        goto_view(page, "Overview")
        expect(tid(page, "overview-cta")).to_be_visible(timeout=WAIT)
        page.wait_for_timeout(1000)
        mark = time.monotonic()
        page.wait_for_timeout(9000)
        left = [x for x in _since(page, mark) if "/v2/chat/" in x[2] or "/v2/jobs/" in x[2]]
        assert not left, ("polling continued after leaving the page", [(m, _path(u)) for _, m, u in left][:6])
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_workspace_switch_and_sign_out_stop_the_polling_of_what_was_left(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        # a second workspace for the same account, made through the product's own route
        r = env.admin_client().post("/v2/workspaces", json={"project_id": f"p-{env.nonce}", "name": "Second workspace", "idempotency_key": "poll-1", "members": []})
        assert r.status_code == 200, r.text
        second = r.json()["workspace_id"]
        page.reload()
        select_workspace(page, env.ws)
        goto_view(page, "Sources")
        expect(tid(page, "artifact-row").first).to_be_visible(timeout=WAIT)
        page.wait_for_timeout(2500)
        page.get_by_label("Workspace").select_option(value=second)
        page.wait_for_timeout(2500)
        mark = time.monotonic()
        page.wait_for_timeout(8000)
        stale = [x for x in _since(page, mark) if env.ws in x[2]]
        assert not stale, ("the old workspace is still being read after the switch", [(m, _path(u)) for _, m, u in stale][:6])
        # sign out: nothing but the sign-in page is left asking
        goto_view(page, "Support")
        page.wait_for_timeout(2000)
        page.get_by_role("button", name="Sign out").click()
        expect(page.get_by_label("Account ID")).to_be_visible(timeout=WAIT)
        page.wait_for_timeout(1000)
        mark = time.monotonic()
        page.wait_for_timeout(8000)
        after = [x for x in _since(page, mark)]
        assert len(after) <= 1, ("requests continued after sign out", [(m, _path(u)) for _, m, u in after][:6])
        assert not page._errors, page._errors
        ctx.close()
    finally:
        server.stop()


def test_database_connections_return_to_their_baseline_after_heavy_page_use(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        base = _connections(env)
        page, ctx = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(page, env.ws)
        peak = 0
        for _ in range(2):
            for label, ready in PAGES:
                goto_view(page, label)
                expect(tid(page, ready).first).to_be_visible(timeout=WAIT)
                peak = max(peak, _connections(env))
        client = env.admin_client()
        for _ in range(60):                                              # a burst of reads, the way a stuck loop would
            assert client.get("/v2/sources/lifecycle", params={"workspace_id": env.ws}).status_code == 200
        peak = max(peak, _connections(env))
        page.wait_for_timeout(3000)
        settled = _connections(env)
        assert settled <= base + 3, ("connections did not return to the baseline", base, settled, "peak", peak)
        assert peak <= base + 40, ("connection count grew with use", base, peak)
        ctx.close()
    finally:
        server.stop()
    page_closed = _connections(env)
    assert page_closed <= base + 1, ("a connection stayed open after the server stopped", base, page_closed)
