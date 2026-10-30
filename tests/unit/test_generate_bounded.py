"""R21: `generate_bounded` bounds a provider call by the wall clock (deadline + grace) without adding a retry."""
import threading
import time

import pytest

from infra.worker import live_analysis as LA


class _Client:
    def __init__(self, behave):
        self.behave, self.closed, self.calls = behave, threading.Event(), 0
        outer = self

        class _Models:
            def generate_content(self, **kw):
                outer.calls += 1
                return outer.behave()
        self.models = _Models()

    def close(self):
        self.closed.set()


@pytest.fixture(autouse=True)
def _close(monkeypatch):
    monkeypatch.setattr(LA, "close_client", lambda c: c.close())
    monkeypatch.setattr(LA, "WALL_CLOCK_GRACE_S", 0.2)


def test_an_answer_inside_the_bound_is_returned_and_the_client_closed():
    c = _Client(lambda: "ok")
    assert LA.generate_bounded(c, 0.5, model="m") == "ok" and c.closed.is_set() and c.calls == 1


def test_a_call_still_open_at_deadline_plus_grace_raises_a_timeout_and_is_not_retried():
    c = _Client(lambda: time.sleep(1.5) or "late")
    t0 = time.monotonic()
    with pytest.raises(LA.WallClockTimeout) as e:
        LA.generate_bounded(c, 0.3, model="m")
    assert time.monotonic() - t0 < 1.0 and isinstance(e.value, TimeoutError) and c.calls == 1
    assert c.closed.wait(3)                                                    # the abandoned call closes its own client when the SDK returns


def test_a_provider_error_is_raised_on_the_callers_thread():
    class Boom(Exception):
        code = 500
    c = _Client(lambda: (_ for _ in ()).throw(Boom("server error")))
    with pytest.raises(Boom):
        LA.generate_bounded(c, 0.5, model="m")


def test_without_a_deadline_the_plain_call_is_used():
    c = _Client(lambda: "ok")
    assert LA.generate_bounded(c, None, model="m") == "ok"


def test_r22_an_abandoned_call_is_closed_so_its_transport_stops_not_only_the_wait():
    """A real genai client (httpx transport) against a local server that holds the request for 5 s: at the bound the caller closes the call's client, the
    request in flight fails on its own thread at once, and no answer can arrive later (the server never gets to answer on that connection)."""
    import http.server
    import json
    import socketserver
    from google import genai
    from google.genai import types

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(5)
            body = json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}]}).encode()
            try:
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Slow)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = genai.Client(api_key="test-key", http_options=types.HttpOptions(base_url=f"http://127.0.0.1:{srv.server_address[1]}/", api_version="v1beta",
                                                                                 retry_options=types.HttpRetryOptions(attempts=1)))
        t0 = time.monotonic()
        with pytest.raises(LA.WallClockTimeout) as e:
            LA.generate_bounded(client, 0.4, model="m", contents="x", config={"http_options": {"timeout": 30_000}})
        took = time.monotonic() - t0
        assert took < 2.0 and "had not yet returned" not in str(e.value)               # the call's thread ended right after the close
        assert not [t for t in threading.enumerate() if t.name == "provider-call" and t.is_alive()]   # R23: no call thread kept (Linux included)
        assert client._api_client._httpx_client._transport._pool.connections == []                       # R23: no connection kept open by the client
        # the provider side is NOT claimed stopped: the server may still work on the request; its cost stays booked at the call's ceiling
    finally:
        srv.shutdown()
