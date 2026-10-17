"""Code evidence in the Explorer (browser; stub provider only, SIMULATED): a rule read from a Python file shows where in the code the quote sits, in words in the
Business view (function name and line span) and exactly as stored in the Technical view; the code is the original text, nothing is executed."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import goto_view, login, open_app, select_workspace, tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.conftest import HARBOR_INPUT, create_job, drain, job_body  # noqa: E402
from tests.pitch_brief.test_python_code_ingest import APP, CodeStub, _import  # noqa: E402
from tests.ui_next.textscan import assert_business_clean  # noqa: E402

WAIT = 30000


def test_a_rule_read_from_code_shows_function_and_line_span_in_words_and_raw_in_technical(env, monkeypatch, pw_browser):
    from infra.worker import extraction
    admin, req = env.admin_client(), env.req_client()
    files = [(f"harbor_demo/app/{n}", (APP / n).read_bytes(), "implementation") for n in ("routing.py", "emergency.py", "config_loader.py")]
    arts, rev = _import(env, admin, files)
    drain(env)
    stub = CodeStub()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    for n in ("routing.py", "emergency.py", "config_loader.py"):
        jid, r = create_job(req, env, "extraction", {"artifact_id": arts[f"harbor_demo/app/{n}"], "source_role": "implementation"})
        assert r.status_code == 200, r.text
        assert admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
        drain(env)
        assert job_body(admin, env, jid)["execution_status"] == "COMPLETED"
    server = ServerThread().start()
    try:
        for technical in (False, True):
            ctx = pw_browser.new_context(base_url=server.url, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.set_default_timeout(WAIT)
            open_app(page, server.url)
            login(page, env.admin, f"cred-a-{env.nonce}")
            select_workspace(page, env.ws)
            if technical:
                page.goto(f"{server.url}/app-next/#/ws/{env.ws}/explorer?mode=technical")
            else:
                goto_view(page, "Explorer")
            expect(tid(page, "explorer-tree")).to_be_visible(timeout=WAIT)
            row = page.locator('[data-type="candidate"]', has_text=re.compile("Emergency manager threshold|emergency_manager_threshold")).first
            expect(row).to_be_visible(timeout=WAIT)
            row.get_by_test_id("rule-toggle").click()
            ev = page.locator('[data-testid="evidence-item"]').first
            expect(ev).to_be_visible()
            text = ev.inner_text()
            assert "emergency.py" in text and "net_amount_jpy >= 1_000_000" in text
            if technical:
                assert '"type":"code_span"' in text.replace(" ", "") and '"qualname":"route_emergency"' in text.replace(" ", "")
            else:
                assert re.search(r"function route_emergency, lines \d+-\d+", text), text
                assert "code_span" not in text and "qualname" not in text
                assert_business_clean(page, "Explorer, rule read from code")
            if not technical:
                from tests.ui_next.test_sources_workspace import _row
                goto_view(page, "Sources")
                code = _row(page, "routing.py")
                expect(code.get_by_test_id("code-coverage-limit")).to_contain_text("absence is not proof")
                assert _row(page, "routing.py").get_by_test_id("stage-analysis").get_attribute("data-state") == "analysed"
            ctx.close()
    finally:
        server.stop()
