"""The SIMULATED banner is server-rendered, escaped text from the operator environment only."""
from fastapi.testclient import TestClient

from infra import judge


def test_notice_is_rendered_escaped_and_absent_when_unset(monkeypatch):
    c = TestClient(judge.app, base_url="https://judge.test")
    monkeypatch.delenv("RIPPLE_UI_NOTICE", raising=False)
    assert 'data-testid="ui-notice"' not in c.get("/app").text
    monkeypatch.setenv("RIPPLE_UI_NOTICE", "SIMULATED <script>alert(1)</script>")
    monkeypatch.setenv("RIPPLE_UI_NOTICE_DETAILS", "HEAD abc <img src=x onerror=alert(2)>")
    html = c.get("/app").text
    assert 'data-testid="ui-notice"' in html and "SIMULATED" in html and "<script>alert(1)" not in html
    assert 'data-testid="ui-notice-details"' in html and "<img src=x" not in html and "&lt;img" in html
    monkeypatch.delenv("RIPPLE_UI_NOTICE_DETAILS")
    assert 'ui-notice-details' not in c.get("/app").text            # short text only when there are no details
