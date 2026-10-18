"""Every response carries a server-generated X-Request-Id (success and error, also the rate guard's own answers): random, never taken from the client,
different per request, and the same id appears in the warning log line of an error response. It grants nothing and contains nothing about the caller."""
from __future__ import annotations

import logging
import re

import starlette.testclient as tc

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _client():
    from infra.judge import app
    return tc.TestClient(app, base_url="https://judge.test")


def test_success_and_error_both_carry_a_fresh_server_id_and_a_client_value_is_ignored(caplog):
    c = _client()
    ok = c.get("/app-next/")
    err = c.get("/v2/workspaces", headers={"X-Request-Id": "attacker-chosen"})          # anonymous: refused
    assert ok.status_code == 200 and err.status_code in (401, 403, 503)
    a, b = ok.headers.get("x-request-id", ""), err.headers.get("x-request-id", "")
    assert HEX32.match(a) and HEX32.match(b) and a != b
    assert b != "attacker-chosen" and "attacker" not in err.text


def test_an_error_response_is_logged_with_its_request_id(caplog):
    caplog.set_level(logging.WARNING, logger="ripple.http")
    r = _client().get("/v2/workspaces")
    rid = r.headers["x-request-id"]
    assert any(rid in rec.getMessage() and "/v2/workspaces" in rec.getMessage() for rec in caplog.records), [x.getMessage() for x in caplog.records]


def test_the_id_is_not_a_credential():
    c = _client()
    first = c.get("/v2/workspaces")
    again = c.get("/v2/workspaces", headers={"X-Request-Id": first.headers["x-request-id"]})   # replaying an id changes nothing
    assert again.status_code == first.status_code and again.headers["x-request-id"] != first.headers["x-request-id"]
