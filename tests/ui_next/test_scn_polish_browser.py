"""Plan 17 phase 2 (SCN-13..17) on the React build: real server, worker, Chromium and routes on the throwaway PostgreSQL; SYNTHETIC assistant model (no paid call).
- SCN-13: colour contrast measured on the rendered command bar, account menu, workspace select, side navigation, form control boundaries and focus outlines, in the
  light theme, the system dark theme and the explicit `data-theme` overrides (computed from the browser's own styles, not from the token table); axe on the shell.
- SCN-15: My work tables run edge to edge in their panel (header band, first column aligned with the panel header; History: test_scn_inspector_browser); a request id
  sent in place of a title is shown as its family with a short id (full id for assistive technology and the Technical view); same rows, ids and links.
- SCN-14: the side navigation follows the window until the person chooses (mount 1280 then 1440, mount 1440, toggle then resize / reload, unreadable or unknown
  stored value); the toggle keeps focus and a re-read of the page never overrides the choice."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_request_assistant_browser import rq_ui  # noqa: E402,F401
from tests.pitch_brief.conftest import env  # noqa: E402,F401
from tests.ui_next.test_a11y import scoped_violations  # noqa: E402

# In-page WCAG contrast: the element's computed colour against what the person actually sees behind it: the background layers up its ancestors, semi-transparent
# ones composited over the first opaque one (SCN-18: opacity variants such as `bg-header-surface/50`). `prop` is a colour property of the element (`color`,
# `outlineColor`, `borderTopColor`) or `ring` (the colour of its box-shadow ring); `color` and `ring` are measured on the element's own background, outline and
# border on its parent's.
MEASURE = """([sel, prop]) => {
  const el = document.querySelector(sel); if (!el) return null;
  const rgb = (s) => { const m = s && s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null; const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const over = (top, under) => ({r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1});
  const bgOf = (n) => { const layers = []; for (let x = n; x; x = x.parentElement) { const c = rgb(getComputedStyle(x).backgroundColor); if (c && c.a > 0) { layers.push(c); if (c.a >= 0.99) break; } }
    let out = {r: 255, g: 255, b: 255, a: 1}; for (const c of layers.reverse()) out = over(c, out); return out; };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const own = prop === 'color' || prop === 'ring';
  const bg = own ? bgOf(el) : bgOf(el.parentElement);
  // a box-shadow lists several layers (Tailwind keeps transparent placeholders first): the ring is the last visible colour
  const ring = () => [...getComputedStyle(el).boxShadow.matchAll(/rgba?\\([^)]+\\)/g)].map((m) => rgb(m[0])).filter((c) => c && c.a > 0).pop() || null;
  let fg = prop === 'ring' ? ring() : rgb(getComputedStyle(el)[prop]);
  if (!fg) return null;
  if (fg.a < 1) fg = over(fg, bg);
  const a = lum(fg), b = lum(bg);
  return Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100;
}"""


def _ratio(page, sel, prop="color"):
    r = page.evaluate(MEASURE, [sel, prop])
    assert r is not None, f"not rendered: {sel}"
    return r


def _measure_shell(page, label):
    """Text >= 4.5:1, focus outline and control boundary >= 3:1, on the real rendered page."""
    out = {}
    hdr = 'header.rp-cmd'
    for name, sel in (("brand", f'{hdr} [data-testid="brand"]'), ("workspace label", f'{hdr} label span'), ("role value", f'{hdr} [data-testid="workspace-roles"]'),
                      ("lease chip", f'{hdr} [data-testid="lease-chip"]'), ("mode pressed", f'{hdr} [aria-pressed="true"]'), ("mode not pressed", f'{hdr} [aria-pressed="false"]'),
                      ("account button", f'{hdr} [data-testid="session-account"]'), ("workspace select", f'{hdr} [data-testid="ws-select"]'),
                      ("nav active", '[data-testid="side-nav"] a[aria-current="page"]'), ("nav inactive", '[data-testid="side-nav"] a:not([aria-current])'),
                      ("nav label", '[data-testid="nav-context"] span')):
        if page.locator(sel).count():
            out[name] = (_ratio(page, sel), 4.5)
    page.get_by_test_id("account-menu-button").click()
    out["account menu text"] = (_ratio(page, '#account-menu p'), 4.5)
    out["account menu sign-out"] = (_ratio(page, '#account-menu [data-testid="signout"]'), 4.5)
    page.keyboard.press("Escape")
    page.get_by_test_id("account-menu-button").focus()                                    # focus returns here; its outline is drawn on the navy bar
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    out["focus outline on the bar"] = (_ratio(page, '[data-testid="account-menu-button"]', "outlineColor"), 3.0)
    page.get_by_test_id("account-menu-button").click()
    page.locator('#account-menu [data-testid="signout"]').focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    out["focus outline in the menu"] = (_ratio(page, '#account-menu [data-testid="signout"]', "outlineColor"), 3.0)
    page.keyboard.press("Escape")
    opt = page.evaluate("() => { const o = document.querySelector('header.rp-cmd [data-testid=\"ws-select\"] option'); const s = getComputedStyle(o); return [s.color, s.backgroundColor]; }")
    assert opt[0] != opt[1], f"{label}: option text and background are the same colour {opt}"
    out["option text"] = (_ratio(page, f'{hdr} [data-testid="ws-select"] option'), 4.5)
    # SCN-18 navy controls: select boundary on the bar; the pressed view's ring on its segment; the unpressed view on hover (composited); keyboard focus.
    out["select boundary"] = (_ratio(page, f'{hdr} [data-testid="ws-select"]', "borderTopColor"), 3.0)
    out["pressed ring"] = (_ratio(page, f'{hdr} [aria-pressed="true"]', "ring"), 3.0)
    off = page.locator(f'{hdr} [aria-pressed="false"]')
    calm = off.evaluate("(e) => { const s = getComputedStyle(e); return [s.boxShadow, s.fontWeight]; }")
    on = page.locator(f'{hdr} [aria-pressed="true"]').evaluate("(e) => { const s = getComputedStyle(e); return [s.boxShadow, s.fontWeight]; }")
    off.hover()
    page.wait_for_timeout(200)
    out["not pressed, hover"] = (_ratio(page, f'{hdr} [aria-pressed="false"]'), 4.5)
    hov = off.evaluate("(e) => { const s = getComputedStyle(e); return [s.boxShadow, s.fontWeight]; }")
    assert on[0] != "none" and hov[0] in ("none", calm[0]) and on[1] != hov[1], (on, hov)    # pressed = ring + weight, never only colour; hover shows neither
    page.mouse.move(0, 400)
    off.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement && document.activeElement.getAttribute('aria-pressed')")
    if focused is not None:
        out["focus outline on the switch"] = (_ratio(page, f'{hdr} [aria-pressed="{focused}"]', "outlineColor"), 3.0)
    page.locator(f'{hdr} [data-testid="ws-select"]').focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    out["focus outline on the select"] = (_ratio(page, f'{hdr} [data-testid="ws-select"]', "outlineColor"), 3.0)
    if os.environ.get("SCN_MEASURE_DIR"):                                                  # optional record of the measured values (development evidence)
        with (pathlib.Path(os.environ["SCN_MEASURE_DIR"]) / "contrast.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"theme": label, "ratios": {k: v[0] for k, v in out.items()}}) + "\n")
    bad = {k: v for k, v in out.items() if v[0] < v[1]}
    assert not bad, f"{label}: contrast below target {bad} (all: {out})"
    return out


@pytest.mark.parametrize("scheme,theme", [("light", None), ("dark", None), ("light", "dark"), ("dark", "light")])
def test_command_bar_menu_select_and_side_nav_meet_contrast_in_every_theme(rq_ui, pw_browser, scheme, theme):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    page.emulate_media(color_scheme=scheme)
    if theme:
        page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.get_by_test_id("count-mine")).to_be_visible(timeout=WAIT)
    bg = page.evaluate("() => getComputedStyle(document.querySelector('header.rp-cmd')).backgroundColor")
    canvas = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert bg != canvas                                                                   # three zones: the bar is not the canvas colour
    _measure_shell(page, f"{scheme}/{theme}")
    # form control boundary (WCAG 1.4.11): a text field of the page against its panel
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions?ui_section=all")
    field = page.locator('main textarea:visible').first
    expect(field).to_be_visible(timeout=WAIT)
    fid = field.evaluate("(e) => { e.setAttribute('data-measure', '1'); return true; }")
    assert fid and _ratio(page, '[data-measure="1"]', "borderTopColor") >= 3.0
    found = [v for v in scoped_violations(page, "header.rp-cmd") + scoped_violations(page, '[data-testid="side-nav"]')]
    assert not found, found
    page.context.close()


def _rail(page):
    nav = page.get_by_test_id("side-nav")
    return nav.get_attribute("data-rail"), nav.get_attribute("data-rail-source")


def test_side_nav_follows_the_window_until_the_person_chooses(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")     # mounted at 1280 (the case of the bug)
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "true", timeout=WAIT)
    assert _rail(page) == ("true", "auto")
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "false")
    nav = page.get_by_role("navigation", name="Main")
    for label in ("Overview", "Sources", "Questions & Decisions", "History", "Support & Access"):                # names visible, not only icons
        expect(nav.get_by_text(label, exact=True)).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 900})
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "true")
    expect(nav.get_by_role("link", name="Sources")).to_be_visible()                          # the rail keeps an accessible name per item
    page.set_viewport_size({"width": 1440, "height": 900})
    toggle = page.get_by_test_id("nav-rail-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    toggle.focus()
    page.keyboard.press("Enter")                                                            # keyboard: the person collapses it at 1440
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "true")
    assert _rail(page) == ("true", "user")
    assert page.evaluate("() => document.activeElement && document.activeElement.dataset.testid") == "nav-rail-toggle"
    expect(toggle).to_have_attribute("aria-label", "Expand the menu")
    page.evaluate("() => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')); }")   # the work list re-reads
    page.wait_for_load_state("networkidle")
    assert _rail(page) == ("true", "user")
    assert page.evaluate("() => document.activeElement && document.activeElement.dataset.testid") == "nav-rail-toggle"
    page.set_viewport_size({"width": 1600, "height": 900})                                 # a wider window does not undo the choice
    assert _rail(page) == ("true", "user")
    page.reload()
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "true", timeout=WAIT)
    assert _rail(page) == ("true", "user")                                                 # kept for the session
    page.get_by_test_id("nav-rail-toggle").click()
    page.set_viewport_size({"width": 1280, "height": 900})
    assert _rail(page) == ("false", "user")                                                # an expanded choice also survives a narrower window
    page.evaluate("() => sessionStorage.setItem('ripple.nav.rail', 'maybe')")              # an unknown stored value is no choice
    page.reload()
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail-source", "auto", timeout=WAIT)
    assert _rail(page) == ("true", "auto")
    page.context.close()


def test_mounted_at_1440_expanded_and_unreadable_storage_falls_back_to_the_window(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1440, "height": 900})
    ctx.add_init_script("""(() => { const g = Storage.prototype.getItem, s = Storage.prototype.setItem;
      Storage.prototype.getItem = function (k) { if (k === 'ripple.nav.rail') throw new Error('blocked'); return g.call(this, k); };
      Storage.prototype.setItem = function (k, v) { if (k === 'ripple.nav.rail') throw new Error('blocked'); return s.call(this, k, v); }; })();""")
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.url + "/app-next/")
    page.get_by_label("Account ID").fill(f"approver-{env.nonce}")
    page.get_by_label("Credential").fill("cv")
    page.get_by_role("button", name="Sign in").click()
    page.get_by_label("Workspace").select_option(value=env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/overview")
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "false", timeout=WAIT)
    page.get_by_test_id("nav-rail-toggle").click()                                          # the choice still applies to this page
    assert _rail(page) == ("true", "user")
    page.reload()
    expect(page.get_by_test_id("side-nav")).to_have_attribute("data-rail", "false", timeout=WAIT)   # nothing could be stored: the window decides again
    assert not errors, errors
    ctx.close()


EDGES = """(sel) => { const t = document.querySelector(sel); if (!t) return null; const card = t.closest('section[data-flush="true"]'); const th = t.querySelector('thead tr');
  const firstCell = t.querySelector('tbody tr td'); const head = card.querySelector(':scope > header');
  const r = (e) => e.getBoundingClientRect(); const cs = (e) => getComputedStyle(e);
  return {table_left: r(t).left, table_right: r(t).right, card_left: r(card).left, card_right: r(card).right, card_border: parseFloat(cs(card).borderLeftWidth),
    head_bg: cs(th).backgroundColor, row_bg: cs(t.querySelector('tbody tr')).backgroundColor, cell_pad_left: parseFloat(cs(firstCell).paddingLeft),
    header_pad_left: parseFloat(cs(head).paddingLeft), sideways: document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1}; }"""


def _flush_ok(page, sel):
    m = page.evaluate(EDGES, sel)
    assert m, f"not rendered: {sel}"
    assert abs(m["table_left"] - (m["card_left"] + m["card_border"])) <= 1 and abs(m["table_right"] - (m["card_right"] - m["card_border"])) <= 1, m   # edge to edge
    assert m["head_bg"] != m["row_bg"], m                                                  # the column header is a band of its own
    assert m["cell_pad_left"] == m["header_pad_left"] == 16, m                             # first column lines up with the panel header text
    assert not m["sideways"], m
    return m


def test_my_work_table_runs_edge_to_edge_and_an_id_is_not_shown_as_a_title(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.get_by_test_id("count-mine")).to_have_attribute("data-count", "2", timeout=WAIT)
    _flush_ok(page, '[data-testid="work-table-mine"] table')
    titles = page.locator('[data-testid="work-todo"][data-section="mine"] [data-testid="work-title"]')
    expect(titles.first).to_have_attribute("data-fallback", "false")                        # a request with its own words keeps them
    real = {page.locator('[data-testid="work-todo"]').nth(i).get_attribute("data-proposal") for i in range(2)}
    # The same list, with the server's id fallback in place of the title of one request (what the server sends when a request has no words yet).
    def swap(route):
        resp = route.fetch()
        body = resp.json()
        for key in ("todo",):
            for it in body.get(key, []):
                if it["proposal_job_id"] == cid:
                    it["title"] = it["proposal_job_id"]
        for it in (body.get("sections") or {}).get("mine", []):
            if it["proposal_job_id"] == cid:
                it["title"] = it["proposal_job_id"]
        route.fulfill(response=resp, json=body)
    page.route("**/v2/work/inbox*", swap)
    page.reload()
    row = page.locator(f'[data-testid="work-todo"][data-proposal="{cid}"]')
    expect(row.get_by_test_id("work-title")).to_have_attribute("data-fallback", "true", timeout=WAIT)
    expect(row.get_by_test_id("work-title")).to_contain_text("Business knowledge change")
    short = row.get_by_test_id("work-title-id").inner_text()
    assert short.startswith(cid[:6]) and (len(cid) <= 16 or short.endswith("…")), short
    assert cid in row.get_by_test_id("work-title").text_content()                          # the full id stays available (assistive technology)
    expect(row.get_by_test_id("work-open-todo")).to_have_attribute("aria-label", f"Open: Business knowledge change {cid}")
    href_before = row.get_by_test_id("work-open-todo").get_attribute("href")
    assert {page.locator('[data-testid="work-todo"]').nth(i).get_attribute("data-proposal") for i in range(2)} == real   # same rows, same ids
    page.get_by_test_id("mode-technical").click()
    expect(row.get_by_test_id("work-title-id")).to_have_text(cid)                          # Technical view: the whole id
    assert row.get_by_test_id("work-open-todo").get_attribute("href").split("?")[0] == href_before.split("?")[0]
    page.get_by_test_id("mode-business").click()
    page.unroute("**/v2/work/inbox*")
    page.set_viewport_size({"width": 390, "height": 844})                                  # phone: labelled stacked rows, nothing sideways
    page.reload()
    expect(page.get_by_test_id("count-mine")).to_have_attribute("data-count", "2", timeout=WAIT)
    first = page.locator('[data-testid="work-table-mine"] tbody tr td').first
    assert first.evaluate("(e) => getComputedStyle(e, '::before').content").strip('"') == "Request"
    assert not page.evaluate("() => document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1")
    page.context.close()


def test_overview_next_step_shrinks_only_when_it_repeats_the_first_row_by_identity(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.get_by_test_id("count-mine")).to_have_attribute("data-count", "2", timeout=WAIT)
    nxt = page.get_by_test_id("next-step")
    expect(nxt).to_have_attribute("data-compact", "yes")
    first = page.locator('[data-testid="work-todo"][data-section="mine"]').first
    first_id = first.get_attribute("data-proposal")
    assert page.get_by_test_id("overview-cta").get_attribute("href") == first.get_by_test_id("work-open-todo").get_attribute("href")   # same request, by link
    expect(nxt).to_contain_text("2 requests are waiting for you")
    expect(page.get_by_test_id("overview-counts")).to_be_visible()                        # the three counts stay
    stats = page.get_by_test_id("overview-stats")
    expect(stats.locator("a")).to_have_count(5)                                            # every Current state figure stays, with its unit text and link
    assert stats.locator('[data-state="known"]').count() + stats.locator('[data-state="unavailable"]').count() == 5
    stats.locator("a").first.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    assert stats.locator("a").first.evaluate("(e) => getComputedStyle(e).outlineStyle") != "none"

    def mutate(fn):
        def handler(route):
            resp = route.fetch()
            body = resp.json()
            fn(body)
            route.fulfill(response=resp, json=body)
        page.unroute("**/v2/work/inbox*")
        page.route("**/v2/work/inbox*", handler)
        page.reload()
        expect(page.get_by_test_id("count-mine")).to_be_visible(timeout=WAIT)

    mutate(lambda b: b["sections"]["mine"].reverse())                                      # the first visible row is now ANOTHER request: no shrink
    expect(page.locator('[data-testid="work-todo"][data-section="mine"]').first).not_to_have_attribute("data-proposal", first_id)
    expect(page.get_by_test_id("next-step")).to_have_attribute("data-compact", "no")
    expect(page.get_by_test_id("next-step")).to_contain_text("waiting for you in My work")
    mutate(lambda b: b.update(unavailable=1))                                              # a partial list: the full line and the warning stay
    expect(page.get_by_test_id("work-partial")).to_be_visible()
    expect(page.get_by_test_id("next-step")).to_have_attribute("data-compact", "no")
    page.unroute("**/v2/work/inbox*")
    page.context.close()
    rp = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash="overview")   # nothing waits for the requester: another next step, full line
    expect(rp.get_by_test_id("next-step")).to_have_attribute("data-compact", "no", timeout=WAIT)
    rp.context.close()


def _active(page, want=None):
    """The focused element's id or test id; with `want`, wait briefly for focus to settle (effects run after the DOM update the test saw)."""
    js = "() => { const a = document.activeElement; return a ? (a.id || a.dataset.testid || a.tagName) : null; }"
    if want is not None:
        page.wait_for_function(f"() => {{ const a = document.activeElement; const v = a ? (a.id || a.dataset.testid || a.tagName) : null; return {list(want)!r}.includes(v); }}", timeout=3000)
    return page.evaluate(js)


def test_compact_navigator_on_an_opened_request_and_jump_to_the_decision_controls(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash=f"questions?chg={cid}")
    page.set_viewport_size({"width": 1440, "height": 900})
    _posts_seen = []
    page.on("request", lambda r: _posts_seen.append(r.url) if r.method == "POST" else None)
    nav = page.get_by_test_id("questions-sections")
    expect(nav).to_have_attribute("data-compact", "true", timeout=WAIT)
    expect(page.get_by_role("navigation", name="Questions sections")).to_have_count(1)    # one navigator
    expect(nav.get_by_test_id("qsec-toggle")).to_contain_text("Request a change")
    expect(nav.get_by_test_id("qsec-all")).to_be_visible()
    frame = page.get_by_test_id("task-frame")
    expect(frame).to_have_attribute("data-asked", "true", timeout=WAIT)
    top = page.evaluate("() => { const e = document.querySelector('[data-testid=\"task-my-obligation\"]'); return Math.round(e.getBoundingClientRect().top + window.scrollY); }")
    assert top < 520, top                                                                  # plan 17 §10 SCN-17 target on the b02 setting (approver, 1440 x 900)
    expect(page.get_by_test_id("task-blockers")).to_have_count(0)                          # (no warning was removed to get there: this request has none)
    jump = frame.get_by_test_id("task-jump")
    expect(jump).to_have_text("Go to the decision controls")
    expect(page.get_by_test_id("rev-decide")).to_contain_text("Your decision")             # asked now: "Your decision"
    page.emulate_media(reduced_motion="reduce")
    jump.click()
    assert _active(page) == f"rev-reason-{cid}"
    assert page.evaluate("() => { const r = document.activeElement.getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; }")   # no smooth wait needed
    assert not _posts_seen, _posts_seen                                                    # scroll and focus only
    page.get_by_test_id("rev-reason").fill("Draft reason kept")
    nav.get_by_test_id("qsec-toggle").focus()
    page.keyboard.press("Enter")                                                           # keyboard opens the switcher
    expect(nav.get_by_test_id("qsec-rules")).to_be_visible()
    nav.get_by_test_id("qsec-rules").click()
    expect(page.get_by_test_id("questions-sections")).not_to_have_attribute("data-compact", "true")   # not deep in "rules": the full navigator
    assert "ui_section=rules" in page.url
    assert _active(page, ("qsec-rules",)) == "qsec-rules"                                                   # focus follows the choice into the new layout
    page.get_by_test_id("qsec-change").click()                                            # back to the request's own section (choices replace the URL, as before)
    expect(page.get_by_test_id("questions-sections")).to_have_attribute("data-compact", "true", timeout=WAIT)
    assert _active(page, ("qsec-toggle", "qsec-change")) in ("qsec-toggle", "qsec-change")
    expect(page.get_by_test_id("rev-reason")).to_have_value("Draft reason kept")           # sections stay mounted: the draft survives
    page.reload()                                                                          # the URL alone restores the same state
    expect(page.get_by_test_id("questions-sections")).to_have_attribute("data-compact", "true", timeout=WAIT)
    page.get_by_test_id("qsec-all").click()
    expect(page.get_by_test_id("questions-sections")).not_to_have_attribute("data-compact", "true")
    assert _active(page, ("qsec-all",)) == "qsec-all"
    expect(page.get_by_test_id("rev-decide")).to_be_visible(timeout=WAIT)
    expect(page.get_by_test_id("task-jump")).to_be_visible()
    page.evaluate("() => document.querySelector('[data-testid=\"rev-decide\"]').setAttribute('hidden', '')")   # a hidden control is never a target
    page.get_by_test_id("task-jump").click()
    assert _active(page) == "task-jump"
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions")                            # the default section never folds the navigator
    expect(page.get_by_test_id("qsection-open")).to_be_visible(timeout=WAIT)
    expect(page.get_by_test_id("questions-sections")).not_to_have_attribute("data-folded", "true")
    page.context.close()


def test_jump_follows_the_asked_obligation_eligible_not_asked_gets_none_and_a_disabled_target_shows_its_reason(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    ap = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash=f"questions?chg={cid}")    # may decide, not asked
    expect(ap.get_by_test_id("task-frame")).to_be_visible(timeout=WAIT)
    expect(ap.get_by_test_id("task-eligible-not-asked")).to_be_visible()
    expect(ap.get_by_test_id("task-jump")).to_have_count(0)
    if ap.get_by_test_id("rev-decide").count():
        expect(ap.get_by_test_id("rev-decide")).not_to_contain_text("Your decision")
    rp = sign_in(pw_browser, server, env.ws, env.req, f"cred-r-{env.nonce}", page_hash=f"questions?chg={cid}")      # waiting requester
    expect(rp.get_by_test_id("task-frame")).to_have_attribute("data-asked", "false", timeout=WAIT)
    expect(rp.get_by_test_id("task-jump")).to_have_count(0)
    rp.context.close()
    a = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash=f"questions?chg={cid}")         # the approver decides (the real control)
    a.get_by_test_id("rev-reason").fill("Checked with Finance.")
    a.get_by_test_id("rev-approve").click()
    expect(a.get_by_test_id("rev-note")).to_contain_text("Knowledge change: Accepted", timeout=WAIT)
    a.context.close()
    ap.reload()                                                                            # now the knowledge owner is asked to commit
    jump = ap.get_by_test_id("task-jump")
    expect(jump).to_have_text("Go to the commit control", timeout=WAIT)
    jump.click()
    assert _active(ap) == f"rev-commit-{cid}"
    assert ap.get_by_test_id("rev-completion").get_attribute("data-knowledge") == "not_committed"   # focus only: nothing committed
    # The same page when the review says the commit control is not available right now: the link goes to the stated reason, not to a dead button.
    def no_commit(route):
        resp = route.fetch()
        body = resp.json()
        body["can"]["commit_knowledge"] = False
        body["can"].setdefault("reasons", {})["commit_knowledge"] = "Injected for the test: a newer version must be read first."
        route.fulfill(response=resp, json=body)
    ap.route("**/review?*", no_commit)
    ap.reload()
    expect(ap.get_by_test_id("task-jump-disabled")).to_be_visible(timeout=WAIT)
    ap.get_by_test_id("task-jump").click()
    assert _active(ap) == f"rev-why-commit-{cid}"
    expect(ap.get_by_test_id("rev-commit")).to_be_disabled()
    ap.context.close()


def test_count_filters_are_compact_rows_and_keep_their_meaning(rq_ui, pw_browser):
    """SCN-19: the counts block is at least 16 px lower than phase 2 (88 px on the same fixture / 1440 x 900 / approver), each count stays >= 44 px to touch,
    nothing overflows at 390 px, and the pressed filter is marked by a ring besides aria-pressed."""
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    page.set_viewport_size({"width": 1440, "height": 900})
    counts = page.get_by_test_id("overview-counts")
    expect(page.get_by_test_id("count-mine")).to_have_attribute("data-count", "2", timeout=WAIT)
    h = counts.bounding_box()["height"]
    assert h <= 88 - 16, h
    for k in ("mine", "waiting_human", "blocked"):
        box = page.get_by_test_id(f"count-{k}").bounding_box()
        assert box["height"] >= 44, (k, box)
    tile = page.get_by_test_id("count-mine")
    row = tile.evaluate("(e) => { const [a, b] = e.children; const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect(); return [Math.abs(ra.top + ra.height / 2 - (rb.top + rb.height / 2)) < 12, getComputedStyle(b).fontVariantNumeric]; }")
    assert row[0] and "tabular-nums" in row[1], row                                         # label and number on one row, tabular number
    tile.click()
    expect(tile).to_have_attribute("aria-pressed", "true")
    assert "inset" in tile.evaluate("(e) => getComputedStyle(e).boxShadow")
    tile.click()
    expect(tile).to_have_attribute("aria-pressed", "false")
    tile.click()                                                                           # a pressed filter, then another workspace (not readable by this account)
    page.goto(f"{server.url}/app-next/#/ws/ws-not-mine-{env.nonce}/overview")
    page.wait_for_load_state("networkidle")
    expect(page.locator('[data-testid="count-mine"][data-count="2"]')).to_have_count(0)    # the previous workspace's counts and filter never carry over
    expect(page.locator('[data-testid="count-mine"][aria-pressed="true"]')).to_have_count(0)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/overview")
    expect(page.get_by_test_id("count-mine")).to_have_attribute("data-count", "2", timeout=WAIT)
    expect(page.get_by_test_id("count-mine")).to_have_attribute("aria-pressed", "false")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    for k in ("mine", "waiting_human", "blocked"):
        over = page.get_by_test_id(f"count-{k}").evaluate("(e) => [...e.querySelectorAll('span')].some((s) => s.scrollWidth > s.clientWidth + 1) || e.scrollWidth > e.clientWidth + 1")
        assert not over, k
        assert page.get_by_test_id(f"count-{k}").bounding_box()["height"] >= 44
    assert not page.evaluate("() => document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1")
    expect(page.get_by_test_id("overview-counts-scope")).to_contain_text("loaded list")
    page.context.close()


def test_before_and_after_are_clause_lists_with_the_servers_exact_words(rq_ui, pw_browser):
    """SCN-20 on the real review (threshold 1,000,000 -> 1,500,000 JPY): clauses split on the server's " | ", only the changed clause marked (tag from CSS, not
    text), the box text is still "Before: <server string>" with no second copy, side by side at 1440 and stacked at 390, nothing posted."""
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash=f"questions?chg={cid}")
    page.set_viewport_size({"width": 1440, "height": 900})
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    rule = page.locator('[data-testid="rev-rule"]').filter(has=page.locator('[data-testid="rev-clauses"][data-structured="true"]')).first
    expect(rule).to_be_visible(timeout=WAIT)
    before, after = rule.get_by_test_id("rev-before"), rule.get_by_test_id("rev-after")
    bt, at = before.text_content(), after.text_content()
    assert bt.startswith("Before: actor: ") and at.startswith("After: actor: ") and " | when: " in bt and " | when: " in at, (bt, at)
    assert bt.count("actor: ") == 1 and at.count("actor: ") == 1                           # one copy of the text, not a list plus a hidden duplicate
    assert " | " not in after.inner_text()                                                # the delimiter is not drawn
    changed = after.locator('[data-testid="rev-clause"][data-mark="changed"]')
    expect(changed).to_have_count(1)
    expect(changed).to_contain_text("1500000")
    assert changed.evaluate("(e) => getComputedStyle(e, '::after').content") == '"changed"'
    assert "changed" not in changed.text_content()
    expect(before.locator('[data-testid="rev-clause"][data-mark="changed"]')).to_contain_text("1000000")
    assert after.locator('[data-testid="rev-clause"][data-mark="same"]').count() >= 2
    b, a = before.bounding_box(), after.bounding_box()
    assert abs(b["y"] - a["y"]) < 2 and a["x"] > b["x"]                                   # side by side
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    b, a = before.bounding_box(), after.bounding_box()
    assert a["y"] >= b["y"] + b["height"] - 1                                               # stacked
    assert not page.evaluate("() => document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1")
    assert not posts, posts
    page.context.close()
