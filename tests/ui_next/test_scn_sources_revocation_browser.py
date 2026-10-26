"""Access revoked while Sources is open (plan 17 SCN-21 follow-up, owner request 2026-10-26). Real server, Chromium, routes and identity store on the throwaway
PostgreSQL; access is changed with the store's own `grant_membership` / `revoke_membership` (no mocked authorization). The product has no realtime revocation
signal: every read is authorised when it is made, so the check is at the next read (an evidence pick, another file, a reload, a sign-in).
A. Evidence rights removed, membership kept (reviewer -> viewer): the next evidence pick is refused, the page re-reads the person's rights and the pane drops the
   evidence block, so the value shown before is gone; another file shows "needs evidence rights"; switching Cards / Table brings nothing back; the file list
   itself stays (the viewer may read it).
B. Membership removed: an evidence read answered BEFORE the removal but delivered after a newer read never shows; the next read ends the session (sign-in
   screen, nothing of the workspace on screen); signing in again, the link is refused in either view and nothing is listed.
Reads only throughout: no job, mutation or model call."""
from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.adapters.v2_identity import PostgresIdentityStore  # noqa: E402
from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.ui_next.test_scn_inspector_browser import WAIT, ch  # noqa: E402,F401


def _open_value(page, rows):
    """Open the first file with two observations in the D6 pane and show its first value; returns (artifact id, shown text)."""
    for i in range(rows.count()):
        rows.nth(i).get_by_test_id("artifact-details").click()
        pane = tid(page, "sources-selected")
        expect(pane.locator('[data-testid="sel-observation-item"], [data-testid="sel-evidence-box"] p, [data-testid="sel-evidence-failed"]').first).to_be_visible(timeout=WAIT)
        if pane.get_by_test_id("sel-observation-item").count() >= 2:
            pane.get_by_test_id("sel-observation-item").nth(0).click()
            expect(pane.get_by_test_id("sel-evidence-value")).to_be_visible(timeout=WAIT)
            return rows.nth(i).get_attribute("data-artifact-id"), pane.get_by_test_id("sel-evidence-value").inner_text()
    raise AssertionError("no file with two observations in this fixture")


def _signed(pw_browser, server, env, who):
    ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    page._posts = []
    page.on("request", lambda r: page._posts.append(r.url) if r.method == "POST" and "/v2/" in r.url and "/auth/" not in r.url else None)
    open_app(page, server.url)
    login(page, who, "cv")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources?files_view=table")
    expect(page.locator('tr[data-testid="artifact-row"]').first).to_be_visible(timeout=WAIT)
    return page, ctx


def test_revoked_access_is_enforced_at_the_next_read_and_nothing_revoked_comes_back(env, ch, pw_browser):
    ids = PostgresIdentityStore(env.dsn)
    project = env.tenant.replace("t-", "p-")
    server = ServerThread().start()
    try:
        # A. evidence rights removed, membership kept
        who = f"scn-rv-{uuid4().hex[:8]}"
        ids.create_account(who, "cv")
        ids.grant_membership(env.tenant, project, env.ws, who, ("reviewer",))
        page, ctx = _signed(pw_browser, server, env, who)
        rows = page.locator('tr[data-testid="artifact-row"]')
        expect(tid(page, "action-analyze")).to_have_count(0)
        target, shown = _open_value(page, rows)
        ids.grant_membership(env.tenant, project, env.ws, who, ("viewer",))                     # no evidence.read any more
        pane = tid(page, "sources-selected")
        pane.get_by_test_id("sel-observation-item").nth(1).click()                              # the next read: refused
        page.wait_for_timeout(1500)
        expect(page.locator('[data-testid="sel-evidence-panel"]')).to_have_count(0, timeout=WAIT)  # the earlier value is cleared
        assert shown not in page.locator("body").inner_text()
        if rows.count():                                                                          # the viewer may still read the list
            tid(page, "sources-view-cards").click()
            expect(page.locator('[data-testid="sel-evidence-panel"]')).to_have_count(0)
            tid(page, "sources-view-table").click()
            expect(page.locator('[data-testid="sel-evidence-panel"]')).to_have_count(0)
            other = [rows.nth(i).get_attribute("data-artifact-id") for i in range(rows.count()) if rows.nth(i).get_attribute("data-artifact-id") != target][0]
            page.locator(f'tr[data-artifact-id="{other}"]').get_by_test_id("artifact-details").click()
            expect(page.locator('[data-testid="sel-evidence-failed"], [data-testid="sources-selected-no-evidence"]').first).to_be_visible(timeout=WAIT)
            expect(tid(page, "sel-observation-item")).to_have_count(0)
            assert shown not in page.locator("body").inner_text()
        assert not page._posts, page._posts
        ctx.close()

        # B. membership removed, with an older answer delivered late
        who2 = f"scn-rv-{uuid4().hex[:8]}"
        ids.create_account(who2, "cv")
        ids.grant_membership(env.tenant, project, env.ws, who2, ("reviewer",))
        page, ctx = _signed(pw_browser, server, env, who2)
        rows = page.locator('tr[data-testid="artifact-row"]')
        target, shown = _open_value(page, rows)
        pane = tid(page, "sources-selected")
        held = []
        page.route("**/evidence/**", lambda route: held.append((route, route.fetch())) if not held else route.continue_())
        pane.get_by_test_id("sel-observation-item").nth(1).click()
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(100)
        assert held and held[0][1].status == 200                                                  # authorised when it was made
        ids.revoke_membership(env.tenant, project, env.ws, who2)
        pane.get_by_test_id("sel-observation-item").nth(0).click()                                # the next read, after the removal
        page.wait_for_timeout(1500)
        held[0][0].fulfill(response=held[0][1])                                                   # the older answer arrives late
        page.wait_for_timeout(800)
        page.unroute("**/evidence/**")
        expect(page.locator('[data-testid="sel-evidence-panel"]')).to_have_count(0)
        assert shown not in page.locator("body").inner_text(), "a value read before the removal is on screen after it"
        if page.get_by_label("Account ID").count() == 0:                                          # the session may also end only at the next page read
            page.reload()
        expect(page.get_by_label("Account ID")).to_be_visible(timeout=WAIT)                       # the session ended
        expect(tid(page, "artifact-row")).to_have_count(0)
        page.get_by_label("Account ID").fill(who2)
        page.get_by_label("Credential").fill("cv")
        page.get_by_role("button", name="Sign in").click()                                        # back to the link: refused, nothing listed
        for view in ("table", "cards"):
            page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources?files_view={view}&file={target}")
            expect(page.locator('[data-testid="link-unavailable"], [data-testid="state-error"]').first).to_be_visible(timeout=WAIT)
            expect(tid(page, "artifact-row")).to_have_count(0)
            expect(tid(page, "sources-selected")).to_have_count(0)
            assert shown not in page.locator("body").inner_text()
        assert not page._posts, page._posts
        ctx.close()
    finally:
        server.stop()
