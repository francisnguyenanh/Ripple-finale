"""Plan 17 §4.1 action journey as an automated check (HK-P17 b; hackathon profile, not human usability evidence): an approver goes from the Overview's My work
to the request, finds its evidence through the assistant, asks one question and reaches the decision controls. Real server, worker, Chromium and routes on the
throwaway PostgreSQL; SYNTHETIC assistant model (no paid call).

Counted: every click / fill the person makes (page loads are not counted). Checked: no control on the path decides anything (no decision row, no decision POST),
each clicked control names what it does (open, ask, show, go to), and the path is no longer than the pre-Plan-17 path. The same test runs unchanged on the
pre-Plan-17 bundle (`RIPPLE_UI_NEXT_DIR`), which is how BASELINE_ACTIONS was measured (HK-P17 record)."""
from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: E402,F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402
from tests.final_loop.test_request_assistant_browser import rq_ui  # noqa: E402,F401
from tests.pitch_brief.conftest import env  # noqa: E402,F401

BASELINE_ACTIONS = 6                    # pre-Plan-17 bundle (source 223205b1), same path, measured with this test (HK-P17)
MIN_TEXT_PX = 12                        # fixed before measuring (HK-P17 c): no visible text on the path renders below 12 px
SMALLEST_TEXT = """() => { let min = 99, where = ''; for (const e of document.querySelectorAll('body *')) {
  if (![...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  if (!r.width || !r.height || cs.visibility === 'hidden' || e.closest('[aria-hidden="true"], .sr-only')) continue;
  const px = parseFloat(cs.fontSize); if (px < min) { min = px; where = (e.getAttribute('data-testid') || e.tagName) + ': ' + e.textContent.trim().slice(0, 40); } }
  return [min, where]; }"""
DECIDING = re.compile(r"^(approve|reject|decide|commit|confirm|request changes)\b", re.I)


def test_overview_to_evidence_assistant_and_decision_controls_without_extra_steps(rq_ui, pw_browser):
    q, model, server, cid = rq_ui
    env = q.env
    page = sign_in(pw_browser, server, env.ws, f"approver-{env.nonce}", "cv", page_hash="overview")
    page.set_viewport_size({"width": 1440, "height": 900})
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    actions = []

    def act(loc, kind="click", text=None):
        label = (loc.get_attribute("aria-label") or loc.inner_text() or "").strip()
        actions.append({"kind": kind, "label": label[:80]})
        assert kind == "fill" or not DECIDING.match(label), f"a control on the path reads like a decision: {label!r}"
        loc.fill(text) if kind == "fill" else loc.click()

    before = counts(env)
    sizes = {}
    row = page.locator(f'[data-testid="work-todo"][data-proposal="{cid}"]')
    expect(row).to_be_visible(timeout=WAIT)
    sizes["overview"] = page.evaluate(SMALLEST_TEXT)
    act(row.get_by_test_id("work-open-todo"))                                                   # 1 open the request from My work
    frame = page.get_by_test_id("task-frame")
    expect(frame).to_be_visible(timeout=WAIT)
    sizes["request"] = page.evaluate(SMALLEST_TEXT)
    act(frame.get_by_test_id("assistant-launch"))                                               # 2 open the assistant beside the task
    panel = frame.get_by_test_id("request-assistant")
    expect(panel.get_by_test_id("assistant-question")).to_be_visible(timeout=WAIT)
    act(panel.get_by_test_id("assistant-question"), "fill", "What does this change affect?")    # 3 type the question
    act(panel.get_by_test_id("assistant-ask"))                                                  # 4 ask (one paid call in production; synthetic here)
    msg = panel.locator('[data-testid="assistant-message"]').first
    expect(msg).to_have_attribute("data-state", "answered", timeout=60000)
    act(msg.locator('[data-testid="assistant-citation-source"]').first)                        # 5 show the evidence behind the answer
    expect(msg.get_by_test_id("assistant-source").first).to_have_attribute("data-available", "true", timeout=WAIT)
    expect(msg.get_by_test_id("assistant-source-item").first).to_be_visible()
    go = msg.locator('[data-testid="assistant-action-go"][data-action="approve_knowledge"]')
    expect(go).to_contain_text("Go to")
    act(go)                                                                                     # 6 go to the decision controls (navigation only)
    expect(page.get_by_test_id("rev-reason")).to_be_focused(timeout=WAIT)
    sizes["answered"] = page.evaluate(SMALLEST_TEXT)
    assert counts(env)["v2_business_decisions"] == before["v2_business_decisions"]              # nothing on the path decided anything
    assert not [u for u in posts if "/decision" in u or "/decide" in u], posts
    assert model.calls == 1
    n = len(actions)
    if os.environ.get("SCN_MEASURE_DIR"):
        with (pathlib.Path(os.environ["SCN_MEASURE_DIR"]) / "action_journey.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"bundle": os.environ.get("RIPPLE_UI_NEXT_DIR") or "committed", "actions": n, "path": actions, "smallest_text_px": sizes}) + "\n")
    assert n <= BASELINE_ACTIONS, actions
    assert all(px >= MIN_TEXT_PX for px, _ in sizes.values()), sizes
    page.context.close()
