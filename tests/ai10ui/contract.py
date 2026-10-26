"""THE UI contract the AI10 browser tests bind to (a proposed interface).

Everything the tests know about the shell -- URL, routes, locators, machine-readable state
attributes, user-step helpers -- lives in THIS module. When the actual markup
differs, adapt it here once (a guessed selector is a proposal, not a binding contract).
The proposal prefers role/label/text locators and `data-testid`; state that a test must wait
on or compare carries a machine value in a `data-*` attribute AND a human-readable text.

SHELL
  GET /app            served by infra/judge.py, 200 text/html, CSP header with script-src 'self'
                      and style-src 'self' (NO 'unsafe-inline'), no inline <script> body, no inline
                      event-handler attributes. Hash routes; the exact route grammar is free, but
                      after ANY step `page.url` must be a shareable deep link: loading it in a fresh
                      tab of a signed-in member of the same workspace shows the same view/entity.
LOGIN / SESSION
  label "Account ID" (textbox), label "Credential" (password), button "Sign in",
  [login-error] role=alert on failure, [session-account] text = signed-in account id,
  button "Sign out", [status-live] role=status aria-live=polite (short status messages).
WORKSPACE
  <select> labelled "Workspace" whose option VALUEs are workspace ids (option text contains the id),
  [workspace-roles] text = the member's roles as listed by GET /v2/workspaces,
  [head-source-revision] / [head-knowledge-revision] text contains the pinned revision id or "none".
NAV (landmark navigation, links): Sources, Analysis, Knowledge, Questions, Changes, History.
SOURCES
  file input labelled "Upload source files" (multiple), button "Prepare import",
  [import-item] rows (data-logical-path, data-disposition, text has the reason when skipped),
  checkbox [import-ack] labelled "I acknowledge the skipped files" shown when an item is
  skip_required (button "Commit import" stays disabled until it is checked),
  button "Commit import", [revision-id] text, [coverage-admission] [coverage-analysis]
  [coverage-index] [coverage-semantic] (data-value = the DTO field, text human readable),
  [artifact-row] (data-logical-path) with buttons "Analyze", "Evidence", link "Export".
  [observation-item] buttons; [evidence-panel] with [evidence-file] [evidence-locator]
  [evidence-revision] [evidence-value].
JOBS  (job detail is a deep-linkable view; the UI never keeps job truth in local storage)
  [job-panel] (data-job-id), [job-status] (data-state = execution_status), [job-cost] (text has
  the USD value of the cost ledger), [job-error] (reason text), buttons "Cancel" / "Retry",
  [job-refresh-failed] + button "Refresh status" when polling fails (recovery, never "running forever").
GENERIC STATES  on every data view: [state-loading] while a request is in flight, [state-empty],
  [state-partial], [state-error] (+ button "Try again"), [notice-stale] (+ button "Reload state"),
  [error-banner] role=alert (data-error-code = server error code or HTTP status).
KNOWLEDGE / QUESTIONS
  [queue-item] (data-question-id) with [queue-item-prompt], [queue-item-status]
  (data-state PENDING|ANSWERED); opening it shows [claim] rows (data-claim-id,
  data-provenance = observed|inferred|human-confirmed|unknown) each with a [provenance-badge]
  (text = the same word); textbox labelled "Your answer" + button "Submit answer" (only with
  knowledge.answer); textbox labelled "Rationale" + button "Confirm" (only with knowledge.confirm);
  [finding-row] (data-finding-id) with button "Resolve conflict" (only with conflict.resolve);
  [km-revision] rows (data-revision-id) + [km-compare-from] [km-compare-to] selects and
  [km-compare-result] (from/to source revision + epoch), button "Accept knowledge" only where a
  server candidate exists.
CHANGE FLOW (T02), all under [flow-*]; every action button is [action-<name>], every stage
  state is [state-<name>] with data-state = the server's machine value:
  intent   field-intent-text, field-intent-target (select, option text shows the rule subject),
           action-parse-intent, action-confirm-intent (knowledge.confirm), state-intent
  analysis action-run-impact, state-impact, action-run-simulation, state-simulation,
           sim-denominator, sim-mode, sim-limits
  proposal field-proposal-artifact (select by logical path), field-proposal-kind (select;
           values yaml_typed_edit|xlsx_typed_edit|markdown|python), field-proposal-path
           (yaml key), field-proposal-sheet + field-proposal-cell (xlsx), field-proposal-expected,
           field-proposal-value (numeric text is sent as a NUMBER, never a string),
           action-prepare-proposal, state-proposal,
           diff-view (diff-line rows, data-kind add|remove|context, text = the exact line)
  review   action-run-review, state-review, review-role rows (role + verdict), tool-check rows
  approval action-prepare-approval, state-approval, approval-digest (text = exact digest),
           diff-after-hash (text = the proposal's after blob hash), review-disclosure (text says the
           roles are the same deterministic evaluator when the server says so),
           manual-work-item, limitation-item, blocker-item (text = the server's blocked reason
           name + reason code), field-decision-reason,
           action-approve, action-reject, state-approval-decision (APPROVED|REJECTED|NONE)
  apply    action-apply, state-apply (job status; data-job-id = the apply job id = the change id),
           state-change (change lifecycle name from
           GET /v2/changes: SOURCE_PUBLISHED|REANALYSING|MACHINE_VERIFIED|AWAITING_ACCEPTANCE|
           COMPLETED|FAILED|RECOVERY_REQUIRED|STALE),
           state-verification, state-reanalysis, state-knowledge, state-completion,
           state-outcome (text "Completed" ONLY with a server completion decision),
           reopened-decision-item (one per carry_forward.reopened decision, text = reason),
           source-before / source-after / km-prior / km-candidate (revision ids from the DTO)
  accept   action-accept-km, action-accept-result, action-request-changes, action-reject-result,
           report-digest, completion-reasons (reason codes), finding-item, field-work-proof,
           action-submit-work-proof, export-link (data-logical-path)
HISTORY / RESTORE / REFRESH
  revision-row (data-revision-id, data-trigger, data-current, data-restored-from),
  history-diff-from / history-diff-to (selects, option value = revision id) + history-diff with
  diff-added / diff-removed / diff-changed (rows, data-artifact-id),
  action-restore-preview (one per revision row), restore-current, restore-target, restore-digest,
  restore-delta (delta-item rows, data-artifact-id + data-kind added|removed|changed),
  affected-decision-item, stale-km-item, restore-loss-warning, field-restore-reason,
  action-restore-approve, action-restore-apply (a DIFFERENT restore operator, via the shared
  link), restore-status (data-state), history-diff;
  field-refresh-files, action-refresh-prepare, refresh-diff (diff rows: data-artifact-id +
  data-kind added|changed|deleted|renamed), refresh-provenance (text/data-value
  external_observation), refresh-ack (checkbox, required when files are deleted),
  action-refresh-commit, refresh-status (data-state), stale-approval-item (data-job-id),
  stale-reason-item.
"""

from __future__ import annotations

import json
import os
import re
import time

from playwright.sync_api import expect

WAIT_MS = 20000
SHORT_MS = 4000
NAV = ("Sources", "Analysis", "Knowledge", "Questions", "Changes", "History")


def tid(page, name):
    return page.get_by_test_id(name)


# ------------------------------------------------------------------ shell / session

UI_BASE = "/" + os.environ.get("UI_BASE", "app").strip("/") + ("/" if os.environ.get("UI_BASE", "app").strip("/") == "app-next" else "")     # "/app-next/" runs the same journeys against the React build


def open_app(page, base_url):
    """Load the shell; a missing route is reported as a MISSING UI (the right red reason)."""
    resp = page.goto(base_url + UI_BASE)
    status = resp.status if resp is not None else None
    assert status == 200, f"missing UI shell: GET {UI_BASE} -> {status}"
    return resp


def login(page, account, credential, *, expect_ok=True):
    page.get_by_label("Account ID").fill(account)
    page.get_by_label("Credential").fill(credential)
    page.get_by_role("button", name="Sign in").click()
    if expect_ok:
        expect(tid(page, "session-account")).to_have_text(account, timeout=WAIT_MS)
        if os.environ.get("UI_MODE") == "technical":      # raw state names and ids are asserted by these journeys
            tid(page, "mode-technical").click()


def tech_url(url):
    """Bridge for the legacy journeys on the new interface: it keeps the Technical view in the LINK (mode=technical), so a journey that
    navigates by a raw URL after signing in would silently fall back to Business. Legacy `/app` is unaffected."""
    if UI_BASE.startswith("/app-next") and os.environ.get("UI_MODE") == "technical" and "mode=technical" not in url:
        tail = url.split("#", 1)[-1]
        return url + ("&" if "?" in tail else "?") + "mode=technical"
    return url


def open_account_menu(page):
    """SCN-05: on a wide screen sign-out sits in the account disclosure of the command bar; open it as a person would (narrow screens show it in the menu)."""
    btn = page.get_by_test_id("account-menu-button")
    if btn.count() and btn.is_visible() and btn.get_attribute("aria-expanded") != "true":
        btn.click()


def logout(page):
    open_account_menu(page)
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_label("Account ID")).to_be_visible(timeout=WAIT_MS)


def sign_in_fresh(world, name, *, view=None, workspace=None):
    """New browser context + page, shell opened, signed in as fixture account `name`."""
    page = world.new_page()
    open_app(page, world.server.url)
    acct = world.acct(name)
    login(page, acct["account"], acct["credential"])
    if workspace is not None:
        select_workspace(page, workspace)
    if view:
        goto_view(page, view)
    return page


def select_workspace(page, workspace_id):
    # on a small screen (new interface) the workspace picker sits inside the menu: open it first, as a person would. Right after a viewport change React may
    # not have re-rendered the narrow layout yet, so wait until either the picker or the menu button is the visible control (no race on a stale layout).
    page.wait_for_function("""() => { const sel = [...document.querySelectorAll('[data-testid="ws-select"]')].some(e => e.getClientRects().length);
        const btn = document.querySelector('[data-testid="menu-button"]'); const narrow = window.innerWidth < 1024;
        return narrow ? !!btn : sel; }""")
    if not page.get_by_label("Workspace").is_visible() and page.get_by_test_id("menu-button").count() and page.get_by_test_id("menu-button").is_visible():
        page.get_by_test_id("menu-button").click()
    page.get_by_label("Workspace").select_option(value=workspace_id)
    expect(page.get_by_label("Workspace")).to_have_value(workspace_id, timeout=WAIT_MS)


_NEXT_NAV = {"Questions": "Questions & Decisions", "Knowledge": "Questions & Decisions", "Analysis": "Sources", "Sandbox": "Support & Access", "Support": "Support & Access"}


def goto_view(page, name):
    if UI_BASE.startswith("/app-next"):
        name = _NEXT_NAV.get(name, name)
    link = page.get_by_role("navigation").get_by_role("link", name=name, exact=True)
    # on a narrow layout (small screen, or a desktop at 200 % zoom) the pages are inside the menu: open it first, as a person would
    if not link.is_visible() and page.get_by_test_id("menu-button").count() and page.get_by_test_id("menu-button").is_visible():
        page.get_by_test_id("menu-button").click()
    link.click()


def open_link(page, url):
    """Open a shared deep link in a FRESH page load (a person pasting a URL)."""
    page.goto("about:blank")
    page.goto(url)


def wait_state(page, testid, value, *, timeout=WAIT_MS):
    expect(tid(page, testid)).to_have_attribute("data-state", value, timeout=timeout)


def visible_text(page):
    return page.locator("body").inner_text()


def denied_visible(page):
    """A server denial is rendered (never swallowed): an alert carrying the HTTP status or code."""
    banner = tid(page, "error-banner")
    expect(banner).to_be_visible(timeout=WAIT_MS)
    return banner


# ------------------------------------------------------------------ T01 steps

def upload_and_import(page, files, *, commit=True):
    """files = [(name, bytes)]; the person picks files, previews the manifest and commits."""
    page.get_by_label("Upload source files").set_input_files(
        [{"name": n, "mimeType": "application/octet-stream", "buffer": b} for n, b in files])
    page.get_by_role("button", name="Prepare import").click()
    expect(tid(page, "import-item").first).to_be_visible(timeout=WAIT_MS)
    if commit:
        if tid(page, "import-ack").count():
            tid(page, "import-ack").check()
        page.get_by_role("button", name="Commit import").click()
        expect(tid(page, "revision-id")).not_to_have_text("", timeout=WAIT_MS)


def artifact_row(page, logical_path):
    return page.locator(f'[data-testid="artifact-row"][data-logical-path="{logical_path}"]')


def claim_row(page, claim_id):
    return page.locator(f'[data-testid="claim"][data-claim-id="{claim_id}"]')


def open_queue_item(page, prompt_fragment):
    item = tid(page, "queue-item").filter(has_text=prompt_fragment).first
    expect(item).to_be_visible(timeout=WAIT_MS)
    item.click()
    return item


def submit_answer(page, text):
    page.get_by_label("Your answer").fill(text)
    page.get_by_role("button", name="Submit answer").click()


def confirm_first_claim(page, rationale="confirmed by the owner"):
    page.get_by_label("Rationale").fill(rationale)
    page.get_by_role("button", name="Confirm", exact=True).click()


# ------------------------------------------------------------------ T02 steps

def click_action(page, name):
    btn = tid(page, f"action-{name}")
    expect(btn).to_be_enabled(timeout=WAIT_MS)
    btn.click()


def parse_intent(page, text, target_contains):
    adv = tid(page, "advanced-input")          # the form-based request is under "Advanced input" (chat is the main input)
    expect(adv).to_be_attached(timeout=WAIT_MS)           # the view may still be loading
    if adv.get_attribute("open") is None:
        adv.locator("summary").click()
        expect(adv).to_have_attribute("open", "", timeout=WAIT_MS)
    page.get_by_label("Describe the change").fill(text)
    sel = tid(page, "field-intent-target")
    options = sel.locator("option")
    expect(options.first).to_be_attached(timeout=WAIT_MS)
    value = None
    for i in range(options.count()):
        if target_contains.lower() in (options.nth(i).inner_text() or "").lower():
            value = options.nth(i).get_attribute("value")
    assert value, f"no rule option containing {target_contains!r}"
    sel.select_option(value=value)
    click_action(page, "parse-intent")
    wait_state(page, "state-intent", "DRAFT")


def confirm_intent(page):
    click_action(page, "confirm-intent")
    wait_state(page, "state-intent", "CONFIRMED")


def run_stage(page, action, state, value="COMPLETED"):
    click_action(page, action)
    wait_state(page, state, value)


def prepare_proposal(page, artifact_path, expected, proposed, *, kind="yaml_typed_edit",
                     path=None, sheet=None, cell=None):
    if tid(page, "proposal-advanced").count():                                         # the by-hand form sits under Advanced in the current UI
        tid(page, "proposal-advanced").evaluate("e => { e.open = true; }")
    tid(page, "field-proposal-artifact").select_option(label=artifact_path)
    tid(page, "field-proposal-kind").select_option(value=kind)
    if kind == "yaml_typed_edit":
        tid(page, "field-proposal-path").fill(path)
    if kind == "xlsx_typed_edit":
        tid(page, "field-proposal-sheet").fill(sheet)
        tid(page, "field-proposal-cell").fill(cell)
    if kind in ("yaml_typed_edit", "xlsx_typed_edit"):
        tid(page, "field-proposal-expected").fill(str(expected))
        tid(page, "field-proposal-value").fill(str(proposed))
    click_action(page, "prepare-proposal")
    wait_state(page, "state-proposal", "COMPLETED")


def approval_digest_text(page):
    return tid(page, "approval-digest").inner_text().strip()


def approve(page, reason="reviewed the exact diff and digest"):
    tid(page, "field-decision-reason").fill(reason)
    click_action(page, "approve")
    wait_state(page, "state-approval-decision", "APPROVED")


def digest_of(text):
    m = re.search(r"sha256:[0-9a-f]{64}|[0-9a-f]{64}", text or "")
    return m.group(0) if m else (text or "").strip()


def login_if_needed(page, world, name):
    if page.get_by_label("Account ID").count() and page.get_by_label("Account ID").first.is_visible():
        acct = world.acct(name)
        login(page, acct["account"], acct["credential"])


def open_flow_as(world, name, flow_url):
    """Another person opens the shared flow link with their own credential."""
    page = sign_in_fresh(world, name)
    open_link(page, flow_url)
    login_if_needed(page, world, name)
    return page


def run_change_to_approval(world, *, text="Set the refund window to 30 days.", target="refund",
                           artifact_path="config/refund.yaml", expected=14, proposed=30,
                           kind="yaml_typed_edit", path="refund_window_days", sheet=None, cell=None,
                           requester="analyst", owner="reviewer"):
    """The real chain in the UI up to a PREPARED approval: requester parses the intent, the
    knowledge owner confirms it (a different person, via the shared link), the requester runs
    impact, simulation, proposal, review and prepares the approval. Every id is discovered through
    user action. Returns the requester page (its `url` is the shareable flow link)."""
    rp = sign_in_fresh(world, requester, view="Changes", workspace=world.workspace)
    parse_intent(rp, text, target)
    op = open_flow_as(world, owner, rp.url)
    confirm_intent(op)
    op.context.close()
    rp.reload()
    wait_state(rp, "state-intent", "CONFIRMED")
    run_stage(rp, "run-impact", "state-impact")
    run_stage(rp, "run-simulation", "state-simulation")
    prepare_proposal(rp, artifact_path, expected, proposed, kind=kind, path=path, sheet=sheet, cell=cell)
    run_stage(rp, "run-review", "state-review")
    run_stage(rp, "prepare-approval", "state-approval")
    return rp


def run_change_to_applied(world, **kw):
    """... then a distinct approver approves through the shared link and the requester applies.
    Returns (requester_page, approver_page, approval_digest_shown_to_the_approver)."""
    rp = run_change_to_approval(world, **kw)
    ap = open_flow_as(world, "approver", rp.url)
    shown = digest_of(approval_digest_text(ap))
    approve(ap)
    rp.reload()
    run_stage(rp, "apply", "state-apply")
    return rp, ap, shown


def observed_requests(page, url_part, method="POST"):
    """Bodies of the requests the page issued (recorded by the fixture's request listener)."""
    out = []
    for req in page._ai10.requests:
        if url_part in req["url"] and req["method"] == method:
            try:
                out.append(json.loads(req["body"]) if req["body"] else {})
            except ValueError:
                out.append({"_raw": req["body"]})
    return out


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def text(self):
        return self._body


def page_fetch(page, method, path, body=None):
    """A raw same-origin request made BY THE PAGE with its own session cookie (what a compromised or edited client
    could send). The browser adds Origin and the cookie itself. `context.request` is not used: Playwright's Node-side
    request context does not send a `Secure` cookie to http://127.0.0.1 (test-side limitation; the product cookie
    attributes are pinned separately in test_csrf_origin_header_and_cookie_flags)."""
    got = page.evaluate("""async ([method, path, body]) => {
        const opts = {method, credentials: 'same-origin', headers: {'X-Ripple-Judge': '1'}};
        if (body !== null) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
        const r = await fetch(path, opts); return [r.status, await r.text()]; }""", [method, path, body])
    return _Resp(got[0], got[1])


def watch_provenance(page):
    """Record EVERY transition of any data-provenance attribute to `human-confirmed` (even one that a later re-render
    undoes): an optimistic local flip is a violation whether or not it is reverted."""
    page.evaluate("""() => { window.__provSeen = [];
        new MutationObserver(list => { for (const m of list) {
            const v = m.target.getAttribute && m.target.getAttribute('data-provenance');
            if (v === 'human-confirmed') window.__provSeen.push(v); } })
          .observe(document.body, {subtree: true, attributes: true, attributeFilter: ['data-provenance'], childList: true}); }""")


def provenance_flips(page):
    return page.evaluate("window.__provSeen ? window.__provSeen.length : -1")


# ---------------------------------------------------------------- R-5: success-message channel (status live region)
# Every success text app/ui/app_*.js can put in #status after a write ; a
# generic pattern so a new/renamed success message is still caught.
SUCCESS_TEXT = re.compile(r"recorded|accepted|committed|resolved|approved|confirmed by you", re.I)


def watch_status(page):
    """Record EVERY text the #status live region (and the page's role=status/alert regions) ever shows from now on,
    including a success flash that is overwritten in the same task: the added Text nodes are captured, not only the
    final textContent. Install AFTER the last reload/navigation that precedes the action under test."""
    page.evaluate("""() => { window.__statusSeen = [];
        const rec = t => { if (t) window.__statusSeen.push(String(t)); };
        const roots = [...document.querySelectorAll('#status, [role=status], [aria-live]')];
        for (const r of roots) rec(r.textContent);
        const mo = new MutationObserver(list => { for (const m of list) {
            for (const n of m.addedNodes) rec(n.textContent);
            if (m.type === 'characterData') rec(m.target.data); }
          for (const r of roots) rec(r.textContent); });
        for (const r of roots) mo.observe(r, {subtree: true, childList: true, characterData: true}); }""")


def status_seen(page):
    got = page.evaluate("window.__statusSeen === undefined ? null : window.__statusSeen")
    assert got is not None, "watch_status was not installed on this document (reloaded after installing?)"
    return got


def success_seen(page):
    return [t for t in status_seen(page) if SUCCESS_TEXT.search(t)]


def assert_no_success(page, *, wait=700):
    """Failure path: no success text may EVER have appeared in the status channel (not even briefly)."""
    page.wait_for_timeout(wait)
    hits = success_seen(page)
    assert not hits, f"a success message was shown for a refused write: {hits}"


def expect_success(page, *, timeout=WAIT_MS):
    """Positive control for the observer itself: the lifted-fault retry DOES show a success text."""
    end = time.time() + timeout / 1000
    while time.time() < end:
        if success_seen(page):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"no success text was recorded; status channel saw {status_seen(page)}")
