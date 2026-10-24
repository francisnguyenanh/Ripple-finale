"""AH-04: a provider client belongs to one call; it is closed after success, timeout, malformed answer or exception; the SDK's own retry is pinned off; the
semantic adapter keeps no client between calls. No network: SDK clients are replaced by recording fakes."""
from __future__ import annotations

import types

import pytest

from infra.worker import live_analysis as la


class FakeClient:
    instances: list = []

    def __init__(self, behaviour="ok", **kwargs):
        self.kwargs, self.closed, self.calls, self.behaviour = kwargs, 0, 0, behaviour
        self.models = types.SimpleNamespace(generate_content=self._generate)
        FakeClient.instances.append(self)

    def _generate(self, **kw):
        self.calls += 1
        if self.behaviour == "timeout":
            raise TimeoutError("timed out")
        if self.behaviour == "error":
            raise RuntimeError("provider error")
        return types.SimpleNamespace(text="{}" if self.behaviour == "ok" else "not json", usage_metadata=None, candidates=[])

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("behaviour", ["ok", "malformed", "timeout", "error"])
def test_one_call_then_closed_whatever_the_outcome(behaviour):
    c = FakeClient(behaviour)
    if behaviour in ("timeout", "error"):
        with pytest.raises((TimeoutError, RuntimeError)):
            la.generate_and_close(c, model="m", contents="p", config={})
    else:
        la.generate_and_close(c, model="m", contents="p", config={})
    assert c.calls == 1 and c.closed == 1


def test_a_failing_close_does_not_replace_the_result_and_a_fake_without_close_is_fine():
    class BadClose(FakeClient):
        def close(self):
            raise OSError("close failed")
    assert la.generate_and_close(BadClose("ok"), model="m", contents="p", config={}).text == "{}"
    bare = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=lambda **kw: "r"))
    assert la.generate_and_close(bare, model="m") == "r"


def test_the_constructed_client_has_sdk_retry_pinned_to_one_attempt(monkeypatch):
    captured = {}

    class Router:
        routing = {"text_generation": {"backend": "aistudio", "account_id": "acct"}}

    import ai_module.accounts as accounts
    import ai_module.router as router
    from google import genai
    monkeypatch.setattr(router, "AIRouter", lambda: Router())
    monkeypatch.setattr(accounts, "get_account", lambda a: {"id": a})
    monkeypatch.setattr(accounts, "resolve_aistudio_api_key", lambda acc: "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kw: captured.update(kw) or FakeClient(**kw))
    la._client_for_model("gemini-3.5-flash-lite")
    opts = captured["http_options"]
    assert opts.retry_options is not None and opts.retry_options.attempts == 1
    from google.genai import _api_client
    stop = _api_client.retry_args(opts.retry_options)["stop"]
    assert stop.max_attempt_number == 1                          # the SDK itself makes exactly one attempt


def test_the_semantic_adapter_builds_and_closes_one_client_per_call():
    from infra.adapters.semantic_vertex import VertexSemanticProvider
    FakeClient.instances = []
    p = VertexSemanticProvider("gemini-3.5-flash-lite", client_factory=lambda m: FakeClient("ok"))
    for _ in range(3):
        p.call("expand", {"query": "q", "items": [], "candidates": []}, timeout=5)
    assert len(FakeClient.instances) == 3 and all(c.closed == 1 and c.calls == 1 for c in FakeClient.instances)
    assert getattr(p, "_held_client", None) is None              # nothing kept between calls
    FakeClient.instances = []
    q = VertexSemanticProvider("gemini-3.5-flash-lite", client_factory=lambda m: FakeClient("error"))
    with pytest.raises(Exception):
        q.call("expand", {"query": "q", "items": [], "candidates": []}, timeout=5)
    assert FakeClient.instances[0].closed == 1


def test_bounded_sustained_run_leaves_no_open_clients():
    FakeClient.instances = []
    for i in range(200):
        c = FakeClient("error" if i % 7 == 0 else "ok")
        try:
            la.generate_and_close(c, model="m", contents="p", config={})
        except RuntimeError:
            pass
    assert len(FakeClient.instances) == 200 and sum(1 for c in FakeClient.instances if c.closed != 1) == 0
