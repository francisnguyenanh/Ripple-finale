"""Offline provider fixture; both confirmation calls still use the public API."""
from unittest.mock import patch
from app.api.router import Request


def model_result(**overrides):
    parsed = dict(text="request", target="approval_threshold", proposed_value=500000,
                  rationale="requested", unit="JPY", scope="all orders", ambiguity=None)
    parsed.update(overrides)
    return {"parsed": parsed, "model": "test-provider", "region": "offline", "latency_ms": 0}


def confirm_intent(api, session_id, actor="judge@example.com", timestamp="2026-09-20T14:00:00Z", value=500000):
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value=model_result(proposed_value=value)):
        candidate = api.call(Request("intent.parse", session_id=session_id, actor=actor,
            timestamp=timestamp, payload={"text": f"For all orders lower approval threshold to JPY {value}"}))
    assert candidate.ok, candidate.error
    assert candidate.data["validation"]["ok"], candidate.data
    reply = api.call(Request("intent.create", session_id=session_id, actor=actor, timestamp=timestamp,
        payload={k: candidate.data[k] for k in ("candidate_id", "content_hash")}))
    assert reply.ok, reply.error
    return reply
