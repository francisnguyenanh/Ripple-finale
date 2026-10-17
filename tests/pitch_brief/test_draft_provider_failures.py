"""change.draft when the provider answers but the answer cannot be used (live B6, 2026-10-09). Real routes/store/worker/executor, STUB provider.

What must hold: a cut-off, blocked or malformed answer is never a draft; the cause is kept (finish reason, usage, safety), a deterministic failure is
not retried with the same limits, the call is priced on the usage the provider REPORTED (an estimate only when there is none, and labelled so),
and a failed job can be retried once the cause is fixed without losing its history or charging the same attempt twice."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from infra.worker import change_draft
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import EXTRACT_MODEL, create_job, drain, job_body
from tests.pitch_brief.test_change_draft import Model, run

IN, OUT = Decimal("0.0015"), Decimal("0.0075")          # USD per 1k tokens of the pinned model in the test config
CAP = 12288                                              # max_output_tokens of the test config (no separate generation cap)


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


class Raw:
    """A stand-in client that returns one fixed response object per call."""

    def __init__(self, response):
        self.response, self.calls, self.prompts = response, 0, []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        self.prompts.append(kw.get("contents"))
        return self.response


def _resp(finish="MAX_TOKENS", usage=(900, None, None, 7044), text="", ratings=None, feedback=None, no_candidate=False, message=None):
    u = None if usage is None else SimpleNamespace(prompt_token_count=usage[0], candidates_token_count=usage[1],
                                                   thoughts_token_count=usage[2], total_token_count=usage[3])
    cand = SimpleNamespace(finish_reason=finish, safety_ratings=ratings or [], finish_message=message)
    return SimpleNamespace(text=text, model_version=EXTRACT_MODEL, candidates=[] if no_candidate else [cand], usage_metadata=u,
                           prompt_feedback=feedback, response_id="resp-test-1")


def _failed(env, ch, monkeypatch, client):
    jid, body = run(env, ch, monkeypatch, client)
    assert body["execution_status"] != "COMPLETED" and body["execution_result"] is None       # never a draft
    return jid, body, env.store.job_attempts(env.tenant, env.ws, jid)


def _diag(att):
    return att["request_context"]["provider_diagnostics"]


def test_truncated_answer_is_not_a_draft_is_not_retried_and_is_priced_on_reported_usage(env, ch, monkeypatch):
    m = Raw(_resp("MAX_TOKENS", usage=(900, None, None, 7044), message="cut"))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    assert body["execution_status"] == "FAILED"
    assert str(body["execution_reason"]).startswith("provider_output_truncated")
    assert "finish_reason=MAX_TOKENS" in body["execution_reason"] and "did not finish normally" in body["execution_reason"]
    assert m.calls == 1 and len(att) == 1                                                     # the same limits would end the same way: no second call
    a = att[0]
    assert a["outcome"] == "FAILED" and a["retryable"] is False and a["provider_calls"] == 1
    # priced on what the provider reported: 900 in, 7044-900 = 6144 generated (output + thinking), not the 0.12816 ceiling
    assert Decimal(a["cost_usd"]) == Decimal(900) / 1000 * IN + Decimal(6144) / 1000 * OUT
    assert a["request_context"]["cost_basis"] == "provider_usage"
    d = _diag(a)
    assert d["finish_reason"] == "MAX_TOKENS" and d["usage"]["total_tokens"] == 7044 and d["usage"]["input_tokens"] == 900
    assert d["requested_output_cap"] == CAP and d["response_id"] == "resp-test-1" and d["finish_message"] == "cut"
    assert "<evidence>" not in json.dumps(a["request_context"]) and a["request_context"]["prompt_chars"] > 1000     # size kept, text never


def test_blocked_answer_is_not_retried_and_keeps_the_safety_metadata(env, ch, monkeypatch):
    rating = SimpleNamespace(category=SimpleNamespace(name="HARM_CATEGORY_DANGEROUS_CONTENT"), probability=SimpleNamespace(name="HIGH"), blocked=True)
    m = Raw(_resp("SAFETY", usage=(900, 0, None, 900), ratings=[rating]))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    assert str(body["execution_reason"]).startswith("provider_response_blocked") and m.calls == 1 and len(att) == 1
    assert att[0]["retryable"] is False
    assert _diag(att[0])["safety_flagged"] == [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH", "blocked": True}]


def test_a_blocked_prompt_is_named_and_not_retried(env, ch, monkeypatch):
    fb = SimpleNamespace(block_reason=SimpleNamespace(name="PROHIBITED_CONTENT"))
    m = Raw(_resp(no_candidate=True, usage=(900, None, None, 900), feedback=fb))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    assert str(body["execution_reason"]).startswith("provider_prompt_blocked") and "PROHIBITED_CONTENT" in body["execution_reason"]
    assert m.calls == 1 and att[0]["retryable"] is False and _diag(att[0])["block_reason"] == "PROHIBITED_CONTENT"


def test_missing_usage_is_charged_at_the_ceiling_and_labelled_an_estimate(env, ch, monkeypatch):
    m = Raw(_resp("MAX_TOKENS", usage=None))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    a = att[0]
    assert Decimal(a["cost_usd"]) == Decimal(24000) / 1000 * IN + Decimal(CAP) / 1000 * OUT          # fail closed: the full ceiling
    assert a["request_context"]["cost_basis"] == "estimate_no_usage"
    assert _diag(a)["finish_reason"] == "MAX_TOKENS" and "usage" not in _diag(a)                    # nothing was reported, nothing invented


def test_a_transient_finish_reason_stays_retryable_and_bounded(env, ch, monkeypatch):
    m = Raw(_resp("OTHER", usage=(900, 5, None, 905)))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    assert att[0]["outcome"] == "RETRYABLE_FAILURE" and att[0]["retryable"] is True and "finish_reason=OTHER" in att[0]["reason"]
    assert m.calls <= 2                                                                         # max_retries 1: at most one more attempt
    assert Decimal(att[0]["cost_usd"]) == Decimal(900) / 1000 * IN + Decimal(5) / 1000 * OUT


def test_malformed_json_is_never_a_draft_keeps_usage_and_says_where_it_failed(env, ch, monkeypatch):
    m = Raw(_resp("STOP", usage=(900, 300, None, 1200), text="not json at all"))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    assert att[0]["outcome"] == "RETRYABLE_FAILURE" and "ProviderInvalidResponseError" in att[0]["reason"]
    assert Decimal(att[0]["cost_usd"]) == Decimal(900) / 1000 * IN + Decimal(300) / 1000 * OUT       # the provider reported usage: priced as reported
    assert att[0]["request_context"]["cost_basis"] == "provider_usage" and _diag(att[0])["stage"] == "parse_or_validate"


def test_thinking_tokens_reported_separately_are_billed_with_a_failed_answer(env, ch, monkeypatch):
    m = Raw(_resp("MAX_TOKENS", usage=(900, 40, 6000, 6940)))
    jid, body, att = _failed(env, ch, monkeypatch, m)
    assert Decimal(att[0]["cost_usd"]) == Decimal(900) / 1000 * IN + Decimal(6040) / 1000 * OUT


def test_a_failed_job_can_be_retried_after_the_fix_keeps_its_history_and_is_not_charged_twice(env, ch, monkeypatch):
    bad = Raw(_resp("MAX_TOKENS", usage=(900, None, None, 7044)))
    jid, body, att = _failed(env, ch, monkeypatch, bad)
    first_cost = Decimal(att[0]["cost_usd"])
    good = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: good)
    r = env.req_client().post(f"/v2/jobs/{jid}/retry", json={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    # right after the retry is queued: a NEW reservation (one more full job budget) is held ABOVE the already settled attempt; nothing is re-charged
    with env.store._tx() as cur:
        cur.execute("SELECT entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, jid))
        state, held, reserved = cur.fetchone()
    new_reservation = 2 * (Decimal(24000) / 1000 * IN + Decimal(CAP) / 1000 * OUT)                  # 2 attempts x worst call of the test config
    assert state == "RESERVED" and Decimal(reserved) == first_cost + new_reservation and Decimal(held) == first_cost + new_reservation
    assert len(env.store.job_attempts(env.tenant, env.ws, jid)) == 1                                  # the failed attempt is still there, untouched
    drain(env)
    done = job_body(ch.admin, env, jid)
    assert done["execution_status"] == "COMPLETED" and good.calls == 1 and bad.calls == 1
    atts = env.store.job_attempts(env.tenant, env.ws, jid)
    assert [a["outcome"] for a in atts] == ["FAILED", "COMPLETED"]                              # the failed attempt stays in the history
    assert atts[0]["attempt_key"] != atts[1]["attempt_key"] and Decimal(atts[0]["cost_usd"]) == first_cost       # its own attempt row, its own usage; the old charge is unchanged
    assert atts[1]["request_context"]["cost_basis"] == "provider_usage" and atts[1]["input_tokens"] == 900 and atts[1]["output_tokens"] == 300
    with env.store._tx() as cur:
        cur.execute("SELECT count(*), sum(cost_usd), max(entry_state) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (env.tenant, env.ws, jid))
        n, total, state = cur.fetchone()
    assert n == 1 and state == "SETTLED"                                                       # one ledger row for the job
    assert Decimal(total) == first_cost + Decimal(atts[1]["cost_usd"])                          # the sum of what each attempt really cost: nothing twice


def test_a_member_whose_ai_use_is_switched_off_cannot_retry_a_failed_model_job(env, ch, monkeypatch):
    from tests.pitch_brief.test_role_model import member
    bad = Raw(_resp("MAX_TOKENS", usage=(900, None, None, 7044)))
    jid, body, att = _failed(env, ch, monkeypatch, bad)
    sus = member(env, "requester", "ai_suspended")
    r = sus.post(f"/v2/jobs/{jid}/retry", json={"workspace_id": env.ws})
    assert r.status_code == 403, r.text
    assert job_body(ch.admin, env, jid)["execution_status"] == "FAILED" and bad.calls == 1       # nothing was queued, nothing spent


def test_the_draft_call_bounds_the_thinking_level_and_keeps_the_token_cap(env, monkeypatch):
    """A real run spent 11,796 of 12,288 tokens thinking (485 on the answer, MAX_TOKENS, nothing usable). The call now carries a thinking level; the output cap is still the approved one."""
    from tests.pitch_brief.chain import build
    from tests.pitch_brief.test_change_draft import Model, run
    ch = build(env, monkeypatch, finguide_scope={"category": "all"})
    model = Model()
    run(env, ch, monkeypatch, model)
    cfg = model.configs[0]
    assert cfg["thinking_config"] == {"thinking_level": "LOW"}
    assert cfg["max_output_tokens"] and cfg["temperature"] == 0.0 and cfg["response_mime_type"] == "application/json"
