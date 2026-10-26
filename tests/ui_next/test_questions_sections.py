"""UX-06: the Questions task navigator. Default shows every section (existing links keep working); choosing a section shows only it,
keeps typed text in a hidden section, makes no request and no POST, and an unknown ui_section value is ignored. Stub providers only."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402

WAIT = 30000
BUSINESS = ["ask", "change", "rules", "gaps"]
SOURCE = ["open", "findings", "versions"]


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(browser, server, env, query="", empty_queue=False):
    ctx = browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(WAIT)
    if empty_queue:                                                  # a workspace where no question waits for a person
        page.route("**/v2/hitl/queue*", lambda r: r.fulfill(status=200, content_type="application/json", body='{"items": [], "hidden": {}}'))
    page._calls = []
    page.on("request", lambda r: page._calls.append((r.method, r.url)))
    open_app(page, server.url)
    login(page, env.admin, f"cred-a-{env.nonce}")
    select_workspace(page, env.ws)
    page.goto(f"{server.url}/app-next/#/ws/{env.ws}/questions{query}")
    expect(tid(page, "questions-sections")).to_be_visible(timeout=WAIT)
    return page, ctx


def test_default_is_open_questions_and_all_sections_is_one_click_and_kept_in_the_link(env, ch, pw_browser):
    """D2 = B (owner decision): the page opens on Open questions; All sections stays one click away and is kept in the link."""
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env)
        expect(tid(page, "qsec-open")).to_have_attribute("aria-current", "true")
        expect(tid(page, "qsection-open")).to_be_visible()
        expect(tid(page, "queue-item").first).to_be_visible()
        for sid in BUSINESS + ["findings", "versions"]:
            expect(tid(page, f"qsection-{sid}")).to_be_hidden()
        tid(page, "qsec-all").click()
        expect(tid(page, "qsec-all")).to_have_attribute("aria-current", "true")
        for sid in BUSINESS + SOURCE:
            expect(tid(page, f"qsection-{sid}")).to_be_visible()
        assert "ui_section=all" in page.url
        page.reload()                                                # a pasted or reloaded link keeps All sections
        expect(tid(page, "qsection-ask")).to_be_visible()
        expect(tid(page, "qsection-open")).to_be_visible()
        ctx.close()
    finally:
        server.stop()


def test_switching_sections_keeps_typed_text_and_sends_no_request(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "?ui_section=ask")
        tid(page, "qa-input").fill("what is the manager approval limit?")
        before = len(page._calls)
        tid(page, "qsec-findings").click()
        expect(tid(page, "qsection-ask")).to_be_hidden()
        tid(page, "qsec-ask").click()
        expect(tid(page, "qa-input")).to_have_value("what is the manager approval limit?")
        page.wait_for_timeout(500)
        new = page._calls[before:]
        assert not [c for c in new if c[0] != "GET"], new
        assert not [c for c in new if "/v2/" in c[1]], f"switching a section must not read or start anything: {new}"
        ctx.close()
    finally:
        server.stop()


def test_deep_link_selects_the_section_and_unknown_value_is_ignored(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "?ui_section=gaps")
        expect(tid(page, "qsection-gaps")).to_be_visible()
        expect(tid(page, "qsection-ask")).to_be_hidden()
        ctx.close()
        page, ctx = _open(pw_browser, server, env, "?ui_section=does-not-exist")
        expect(tid(page, "qsection-ask")).to_be_visible()
        expect(tid(page, "qsection-versions")).to_be_visible()
        ctx.close()
    finally:
        server.stop()


def test_a_link_into_another_section_opens_it_and_a_named_section_wins(env, ch, pw_browser):
    """Links made before D2 keep working: a change link opens Request a change, a finding link opens Findings; ui_section in the URL wins."""
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, "?chg=chg-not-a-real-change")
        expect(tid(page, "qsection-change")).to_be_visible()
        expect(tid(page, "qsection-open")).to_be_hidden()
        ctx.close()
        page, ctx = _open(pw_browser, server, env, "?finding=finding-x")
        expect(tid(page, "qsection-findings")).to_be_visible()
        expect(tid(page, "qsection-open")).to_be_hidden()
        ctx.close()
        page, ctx = _open(pw_browser, server, env, "?ui_section=rules&chg=chg-not-a-real-change")
        expect(tid(page, "qsection-rules")).to_be_visible()
        expect(tid(page, "qsection-change")).to_be_hidden()
        ctx.close()
    finally:
        server.stop()


def test_no_open_question_says_so_and_offers_the_other_sections(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env, empty_queue=True)
        expect(tid(page, "qsection-open")).to_be_visible()
        expect(tid(page, "state-empty")).to_contain_text("No question is waiting for a human")
        expect(tid(page, "open-empty-next")).to_be_visible()
        tid(page, "open-empty-ask").click()
        expect(tid(page, "qsection-ask")).to_be_visible()
        expect(tid(page, "qsection-open")).to_be_hidden()
        tid(page, "qsec-open").click()
        tid(page, "open-empty-all").click()
        for sid in BUSINESS + SOURCE:
            expect(tid(page, f"qsection-{sid}")).to_be_visible()
        expect(tid(page, "open-empty-next")).to_be_hidden()          # the shortcuts belong to the Open questions view only
        ctx.close()
    finally:
        server.stop()


def test_navigator_is_keyboard_operable_on_a_narrow_screen(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _open(pw_browser, server, env)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(tid(page, "questions-sections")).to_have_attribute("data-folded", "true")      # SCN-05: one labelled disclosure on a phone, the content is not pushed down
        tid(page, "qsec-toggle").focus()
        page.keyboard.press("Enter")                                                             # opens with the keyboard
        expect(tid(page, "qsec-rules")).to_be_visible()
        tid(page, "qsec-rules").focus()
        page.keyboard.press("Enter")
        expect(tid(page, "qsection-rules")).to_be_visible()
        expect(tid(page, "qsection-ask")).to_be_hidden()
        expect(tid(page, "qsec-rules")).to_be_hidden()                                          # choosing folds it again
        expect(tid(page, "qsec-toggle")).to_contain_text("Rules")                              # and it names the section shown
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        ctx.close()
    finally:
        server.stop()
