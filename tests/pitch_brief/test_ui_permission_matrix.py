"""Role x page: what each signed-in role SEES and may CLICK on every page of the application (real server, real Chromium, stub provider).
Printed as a matrix (run with -s) and asserted on the invariants: a role never gets an enabled action it cannot perform; a disabled action says why."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import UI_BASE, tid  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal, _to_approval  # noqa: E402
from tests.pitch_brief.test_ui_handoff import WAIT, _account, _signed_in  # noqa: E402

PAGES = ["overview", "sources", "explorer", "questions", "changes", "impact", "harbor", "history", "support"]
# chrome that is not a business action
CHROME = {"Sign out", "Business", "Technical", "Menu", "Close menu", "Refresh", "Try again", "Refresh status", "Reload state"}


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _buttons(page):
    return page.evaluate("""() => [...document.querySelectorAll('main button, main a[role=button]')].filter(b => b.offsetParent !== null)
        .map(b => ({t: (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 60), id: b.getAttribute('data-testid') || '',
                    off: b.disabled || b.getAttribute('aria-disabled') === 'true', why: b.getAttribute('title') || ''}))
        .filter(b => b.t || b.id)""")


def test_matrix(env, ch, monkeypatch, pw_browser):
    c = chain_to_proposal(env, ch, monkeypatch)
    _to_approval(env, ch, c)
    server = ServerThread().start()
    out = {}
    try:
        who = {"requester": (env.req, f"cred-r-{env.nonce}"), "admin": (env.admin, f"cred-a-{env.nonce}"), "approver": _account(env, "change_approver"),
               "reviewer": _account(env, "reviewer"), "viewer": _account(env, "viewer")}
        for role, cred in who.items():
            ctx, page = _signed_in(pw_browser, server, env, cred)
            for pg in PAGES:
                page.goto(f"{server.url}{UI_BASE}#/ws/{env.ws}/{pg}" + (f"?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}" if pg == "changes" else ""))
                page.wait_for_timeout(1800)
                btns = [b for b in _buttons(page) if b["t"] not in CHROME]
                out[(role, pg)] = btns
            ctx.close()
    finally:
        server.stop()
    rows = {}
    for (role, pg), btns in out.items():
        on = sorted({b["id"] or b["t"] for b in btns if not b["off"]})
        off = sorted({(b["id"] or b["t"]) + (" [" + b["why"][:40] + "]" if b["why"] else " [NO REASON]") for b in btns if b["off"]})
        rows[f"{role}/{pg}"] = {"enabled": on, "disabled": off}
    print("\nMATRIX\n" + json.dumps(rows, indent=1))
    def enabled(role):
        return {i for k, v in rows.items() if k.startswith(role + "/") for i in v["enabled"]}

    def acts(names):
        return {n for n in names if n.startswith(("action-", "review-", "member-", "workspace-", "ask-", "wi-start", "sandbox-")) and not n.endswith("-toggle")}
    # nobody gets an enabled action their role cannot perform
    assert acts(enabled("viewer")) == set(), acts(enabled("viewer"))
    assert acts(enabled("reviewer")) == {"review-ready", "ask-start"}, acts(enabled("reviewer"))
    # The fixture stops at an approval bundle that waits for a decision (contract confirmed by another person, review done, approval prepared by the
    # requester). The page resolves that chain from the proposal, so the approver's job is on screen: a person holding change.approve who did not
    # request the bundle (the approver, and the admin, whose bundle includes change.approve) may Approve or Reject; nobody else may.
    assert acts(enabled("approver")) == {"ask-start", "action-approve", "action-reject"}, acts(enabled("approver"))   # the check is already confirmed; the manual form is folded under Advanced
    assert {"action-approve", "action-reject"} <= enabled("admin") and {"action-approve", "action-reject"} <= enabled("approver")
    assert not {"action-approve", "action-reject"} & (enabled("requester") | enabled("reviewer") | enabled("viewer")), "no decision button outside change.approve"
    assert "action-apply" not in enabled("approver") and not any(a.startswith("member-") for a in enabled("requester") | enabled("approver") | enabled("reviewer") | enabled("viewer"))
    assert "member-ai-off" in enabled("admin")
    # in permission but not yet possible: visible, disabled, with the reason (apply waits for the decision; the requester holds change.apply)
    for role in ("requester", "admin"):
        assert any(d.startswith("action-apply [Waiting for the approver") for d in rows[f"{role}/changes"]["disabled"]), (role, rows[f"{role}/changes"]["disabled"])
    # HIDDEN, not merely disabled: an action outside the role does not exist in the page at all
    def present(role):
        return {(b["id"] or b["t"]) for (r, pg), btns in out.items() if r == role for b in btns}
    for role in ("viewer", "reviewer", "approver", "requester"):
        assert not {n for n in present(role) if n.startswith("member-") or n.startswith("workspace-create") or n.startswith("sandbox-")}, role
    assert not {n for n in present("viewer") if n.startswith(("action-", "review-", "ask-", "wi-start"))}, present("viewer")
    assert not {n for n in present("reviewer") if n.startswith("action-")}, present("reviewer")
    assert not {n for n in present("approver") if n.startswith("action-") and n not in ("action-set-contract", "action-confirm-checks", "action-approve", "action-reject")}, present("approver")   # no apply, no prepare, no analyse for an approver
    assert not {n for n in present("requester") if n in ("action-approve", "action-reject", "action-set-contract", "action-confirm-checks", "review-ready", "review-comment", "review-request-changes")}, present("requester")
    for role in ("viewer", "requester", "approver"):                                      # no evidence.read: no Evidence drawer, no Export
        assert "Evidence" not in present(role) and "export-link-source" not in present(role), role
    assert "Evidence" in present("reviewer") and "Evidence" in present("admin")
    # every disabled action says why
    assert not [(k, d) for k, v in rows.items() for d in v["disabled"] if "NO REASON" in d], [(k, d) for k, v in rows.items() for d in v["disabled"] if "NO REASON" in d]
    pathlib_out = os.environ.get("MATRIX_OUT")
    if pathlib_out:
        open(pathlib_out, "w", encoding="utf-8").write(json.dumps(rows, indent=1))


def _changes_url(server, env, c):
    return f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={c.intent}&draft={c.draft}&impact={c.impact}&simulation={c.simulation}&proposal={c.proposal}"


def _count(page, testid):
    return page.locator(f"[data-testid={testid}]").count()


def test_decision_buttons_follow_the_state_and_nobody_decides_their_own_change(env, ch, monkeypatch, pw_browser):
    """Approve/Reject exist only (1) for a holder of change.approve, (2) once an approval bundle waits for a decision, (3) when the viewer did not
    request that bundle. The server enforces the same rules whatever the page shows (negative checks over the API at the end)."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    c = chain_to_proposal(env, ch, monkeypatch)
    approver = _account(env, "change_approver")
    server = ServerThread().start()
    try:
        # (2) state: with a proposal but NO approval bundle yet, not even the approver has a decision button
        ctx, page = _signed_in(pw_browser, server, env, approver)
        page.goto(_changes_url(server, env, c)); page.wait_for_timeout(1800)
        assert _count(page, "action-approve") == 0 and _count(page, "action-reject") == 0
        ctx.close()
        op, _review, aj, appr = _to_approval(env, ch, c)                  # contract by another person, review, approval bundle requested by env.req
        # (1)+(2) with the bundle waiting, the approver gets both buttons, enabled
        ctx, page = _signed_in(pw_browser, server, env, approver)
        page.goto(_changes_url(server, env, c)); page.wait_for_timeout(1800)
        for tid_ in ("action-approve", "action-reject"):
            expect(page.get_by_test_id(tid_)).to_be_visible(); expect(page.get_by_test_id(tid_)).to_be_enabled()
        expect(page.get_by_test_id("action-apply")).to_have_count(0)      # an approver has no apply at all (hidden, not disabled)
        ctx.close()
        # (3) the requester of the bundle, even if given the approver role as well, is NOT offered the decision on their own change
        project = env.tenant.replace("t-", "p-")
        PostgresIdentityStore(env.dsn).grant_membership(env.tenant, project, env.ws, env.req, ("requester", "change_approver"))
        ctx, page = _signed_in(pw_browser, server, env, (env.req, f"cred-r-{env.nonce}"))
        page.goto(_changes_url(server, env, c)); page.wait_for_timeout(1800)
        assert _count(page, "action-approve") == 0 and _count(page, "action-reject") == 0, "a person never gets the decision buttons for a bundle they requested"
        expect(page.get_by_test_id("action-apply")).to_be_disabled()     # in permission (change.apply) but not yet possible: disabled ...
        assert "Waiting for the approver" in (page.get_by_test_id("action-apply").get_attribute("title") or "")    # ... and says why
        ctx.close()
    finally:
        server.stop()
    # server side, independent of any page: the requester (now also an approver by role) is refused for their own bundle; the distinct approver is not
    dec = {"workspace_id": env.ws, "action": "approve", "reason": "x", "expected_approval_digest": appr["approval_digest"]}
    assert c.req.post(f"/v2/jobs/{aj}/decision", json=dec).status_code in (403, 409), "distinct approver required, whatever roles the requester holds"
    assert op.post(f"/v2/jobs/{aj}/decision", json={**dec, "expected_approval_digest": "0" * 64}).status_code == 409     # a wrong fingerprint is refused
    assert op.post(f"/v2/jobs/{aj}/decision", json=dec).status_code == 200                                                # the distinct approver may decide
