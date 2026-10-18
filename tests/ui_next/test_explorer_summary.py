"""UX-11: the Explorer's compact summary agrees with the graph read model (unique counts), and an incomplete reading is flagged
explicitly instead of looking like a complete one. Stub providers only; nothing is mutated."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.pitch_brief.chain import build  # noqa: E402
from tests.ui_next.test_explorer_tree import _go  # noqa: E402
from tests.ai10ui.contract import tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402

WAIT = 30000


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def test_summary_counts_match_the_graph_and_incomplete_is_flagged(env, ch, pw_browser):
    server = ServerThread().start()
    try:
        page, ctx = _go(pw_browser, server, env, (env.admin, f"cred-a-{env.nonce}"))
        graph = env.admin_client().get("/v2/knowledge/graph", params={"workspace_id": env.ws}).json()
        rules = [n for n in graph["nodes"] if n["type"] == "candidate"]
        files = [n for n in graph["nodes"] if n["type"] == "artifact"]
        summary = tid(page, "explorer-summary")
        expect(summary).to_be_visible(timeout=WAIT)
        text = summary.inner_text()
        assert f"Rules: {len(rules)}" in text, text                     # D4: each chip names what it counts (text, not colour)
        assert f"Files: {len(files)}" in text, text
        assert tid(page, "explorer-counts").is_hidden()                   # the long explanation waits behind "What these numbers mean"
        tid(page, "explorer-counts-more").locator("summary").click()
        expect(tid(page, "explorer-counts")).to_be_visible()
        cov = graph["coverage"]
        unanalysed = [a for a in cov.get("artifacts", []) if a.get("extraction") != "PENDING_HUMAN"]
        incomplete = bool(unanalysed) or bool(cov.get("truncated"))
        assert (tid(page, "explorer-incomplete").count() == 1) == incomplete, (incomplete, text)
        if incomplete:
            assert "unknown, not zero" in tid(page, "explorer-incomplete").inner_text()
        ctx.close()
    finally:
        server.stop()
