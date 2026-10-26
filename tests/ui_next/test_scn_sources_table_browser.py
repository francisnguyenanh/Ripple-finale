"""SCN-21 Sources Cards | Table on the real page (Harbor stub workspace with sources; real server, Chromium, routes on the throwaway PostgreSQL; reads only).
- Cards stay the default; Table is one more view of the SAME rows: same ids / paths / selection attributes, same stage words, same actions; one view at a time
  (no duplicate rows or test ids), no button inside a clickable row, a flush table.
- Switching views requests nothing (no read, no job, no model), keeps the selected file, the open D6 pane and the URL; reload / deep link restore the view.
- Rules found are the server's counts (an unreadable lifecycle shows Unknown, never 0, and says so); a reader sees no Analyze; an account without access is refused.
- Rapid selection in the table ends on the last pick; a long path wraps; 390 px uses labelled rows without sideways scroll."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.ui_next.test_scn_inspector_browser import WAIT, _open, ch  # noqa: E402,F401


def _ids(page):
    return [tid(page, "artifact-row").nth(i).get_attribute("data-artifact-id") for i in range(tid(page, "artifact-row").count())]


def test_table_view_is_the_same_rows_and_actions_and_switching_requests_nothing(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "sources")
        expect(tid(page, "artifact-row").first).to_be_visible(timeout=WAIT)
        expect(tid(page, "sources-view-cards")).to_have_attribute("aria-pressed", "true")     # Cards stay the default (D6)
        assert page.locator('article[data-testid="artifact-row"]').count() == tid(page, "artifact-row").count()
        cards = _ids(page)
        first = tid(page, "artifact-row").first
        aid = first.get_attribute("data-artifact-id")
        first.get_by_test_id("artifact-details").click()                                    # open the D6 pane from a card
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", aid)
        page.wait_for_load_state("networkidle")
        reads = []
        page.on("request", lambda r: reads.append(r.url) if "/v2/" in r.url else None)
        tid(page, "sources-view-table").click()
        table = tid(page, "sources-files-table")
        expect(table).to_be_visible()
        assert "files_view=table" in page.url and f"file={aid}" in page.url
        assert page.locator('article[data-testid="artifact-row"]').count() == 0                 # one view at a time
        assert page.locator('tr[data-testid="artifact-row"]').count() == len(cards) and _ids(page) == cards   # same rows, same order, same ids
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", aid)       # the pane stays open on the same file
        row = page.locator(f'tr[data-artifact-id="{aid}"]')
        expect(row).to_have_attribute("data-selected", "true")
        expect(row.get_by_test_id("artifact-details")).to_have_attribute("aria-pressed", "true")
        assert page.locator('tr[data-testid="artifact-row"] button button, button [data-testid="artifact-details"]').count() == 0
        assert row.get_attribute("onclick") is None and row.get_attribute("role") is None      # the row itself is not a control
        page.wait_for_load_state("networkidle")
        assert not reads, reads                                                                 # switching requested nothing
        from tests.ui_next.test_a11y import scoped_violations
        assert not scoped_violations(page, '[data-testid="sources-table-card"]'), scoped_violations(page, '[data-testid="sources-table-card"]')
        assert not [u for u in page._posts if "/v2/" in u], page._posts
        expect(row.get_by_test_id("file-analysis")).not_to_have_text("")
        known = row.get_by_test_id("file-rules")
        expect(known).to_have_attribute("data-known", "true")
        assert not known.inner_text().startswith("0 found")                                              # a zero total reads "None found / None yet", never "0 found"
        m = page.evaluate("""() => { const t = document.querySelector('[data-testid="sources-files-table"] table'); const c = t.closest('section[data-flush="true"]');
            const a = t.getBoundingClientRect(), b = c.getBoundingClientRect(); return [a.left - b.left, b.right - a.right]; }""")
        assert all(0 <= x <= 1.5 for x in m), m                                                   # flush to the panel edges
        ids = _ids(page)
        for other in ids[1:4]:                                                                   # rapid selection: the last pick wins
            page.locator(f'tr[data-artifact-id="{other}"]').get_by_test_id("artifact-details").click()
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", ids[min(3, len(ids) - 1)])
        page.reload()                                                                           # deep link: view and selection come back from the URL
        expect(tid(page, "sources-files-table")).to_be_visible(timeout=WAIT)
        expect(tid(page, "sources-selected")).to_have_attribute("data-artifact-id", ids[min(3, len(ids) - 1)])
        tid(page, "sources-view-cards").click()
        expect(page.locator('article[data-testid="artifact-row"]').first).to_be_visible()
        assert "files_view" not in page.url
        tid(page, "sources-view-table").click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.keyboard.press("Escape")
        expect(tid(page, "sources-files-table")).to_be_visible()
        first_cell = page.locator('[data-testid="sources-files-table"] tbody tr td').first
        assert first_cell.evaluate("(e) => getComputedStyle(e, '::before').content").strip('"') == "File"
        assert page.evaluate("() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1")
        long_path = page.evaluate("() => { const r = document.querySelector('tr[data-testid=\"artifact-row\"] td'); return r.scrollWidth <= r.clientWidth + 1; }")
        assert long_path
        ctx.close()
    finally:
        server.stop()


def test_table_view_states_unknown_stages_and_roles(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "sources?files_view=table")
        page.route("**/v2/sources/lifecycle*", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"injected"}'))
        page.reload()
        expect(tid(page, "lifecycle-unavailable")).to_be_visible(timeout=WAIT)                 # the failure is said
        rules = page.locator('tr[data-testid="artifact-row"]').first.get_by_test_id("file-rules")
        expect(rules).to_have_text("Unknown")                                                    # never 0
        expect(rules).to_have_attribute("data-known", "false")
        expect(page.locator('tr[data-testid="artifact-row"]').first.get_by_test_id("file-analysis")).to_have_text("Unknown")
        ctx.close()
        from uuid import uuid4
        from infra.adapters.v2_identity import PostgresIdentityStore
        ids = PostgresIdentityStore(env.dsn)
        reader = f"scn-r-{uuid4().hex[:8]}"
        ids.create_account(reader, "cv")
        ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, reader, ("viewer",))
        from tests.ai10ui.contract import login, open_app, select_workspace
        ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
        rp = ctx.new_page()
        rp.set_default_timeout(WAIT)
        open_app(rp, server.url)
        login(rp, reader, "cv")
        select_workspace(rp, env.ws)
        rp.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources?files_view=table")
        expect(tid(rp, "sources-files-table")).to_be_visible(timeout=WAIT)
        expect(tid(rp, "action-analyze")).to_have_count(0)                                       # a reader starts nothing, in either view
        expect(rp.get_by_role("button", name="Analyze")).to_have_count(0)
        tid(rp, "sources-view-cards").click()
        expect(tid(rp, "action-analyze")).to_have_count(0)
        stranger = f"scn-x-{uuid4().hex[:8]}"
        ids.create_account(stranger, "cv")
        rp.context.clear_cookies()
        open_app(rp, server.url)
        login(rp, stranger, "cv")
        rp.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources?files_view=table")
        expect(rp.locator('[data-testid="link-unavailable"], [data-testid="state-error"]').first).to_be_visible(timeout=WAIT)   # refused, nothing listed
        expect(tid(rp, "artifact-row")).to_have_count(0)
        ctx.close()
    finally:
        server.stop()
