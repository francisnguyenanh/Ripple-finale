"""AH-05: operational reasons and provider diagnostics are typed, allowlisted and bounded; hostile exception text never survives."""
from infra.worker import provider_response as pr
from infra.worker.execution_types import NonRetryableExecutionError, ProviderExecutionError
from infra.worker.safe_diagnostics import safe_detail, safe_diagnostics, safe_reason, sanitize_reason

SECRET = "-".join(["sk", "live", "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "0123456789"])      # token-shaped, assembled at run time (no literal for the publication scan)
SOURCE = "Purchases of 1,000,000 JPY or more need manager approval"


def test_server_reasons_keep_their_code_and_safe_detail():
    assert safe_reason(NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")) == \
        "authority_revoked: the person who asked no longer holds the permission"
    assert safe_reason(NonRetryableExecutionError("unpriced_model")) == "unpriced_model"
    assert safe_reason(NonRetryableExecutionError("provider_output_truncated", "finish_reason=MAX_TOKENS: the answer did not finish normally")) ==         "provider_output_truncated: finish_reason=MAX_TOKENS: the answer did not finish normally"
    assert safe_reason(ProviderExecutionError("provider timed out"), prefix="provider_execution_failed") == \
        "provider_execution_failed: ProviderExecutionError: provider timed out"


def test_hostile_exception_text_is_dropped_but_the_code_stays():
    hostile = [f"value '{SOURCE}' is not allowed", f"token={SECRET}", "unknown key {\"ignore previous\": 1}",
               "postgresql://user:pw@db/x refused", "line one\nline two", "日本語のソース本文", "x" * 300, f"bad literal {SECRET}"]
    for h in hostile:
        r = safe_reason(NonRetryableExecutionError("schema_violation", h))
        assert r in ("schema_violation", "schema_violation: postgresql"), (h, r)          # at most a single-token class / scheme name survives
        assert SECRET not in r and SOURCE not in r
        assert safe_detail(h) is None


def test_stored_reasons_from_before_the_change_are_reduced_on_read():
    assert sanitize_reason(f"provider_execution_failed: ClientError: 400 {{'error': '{SOURCE}'}}") == "provider_execution_failed: ClientError"
    assert sanitize_reason("schema_violation: section paragraphs: 37 > 3 x 12") == "schema_violation: section paragraphs: 37 > 3 x 12"
    assert sanitize_reason("schema_violation: <facts>F1</facts>") == "schema_violation"
    assert sanitize_reason("Some Free Text: with detail") == "error: with detail"
    assert sanitize_reason(None) is None
    assert sanitize_reason("provider_attempt_limit_reached: 2 provider attempt(s) already made (limit 2); none will be repeated") == \
        "provider_attempt_limit_reached: 2 provider attempt(s) already made (limit 2); none will be repeated"


def test_diagnostics_keep_only_typed_allowlisted_fields():
    d = safe_diagnostics({"stage": "request", "status_code": 400, "error_class": "ClientError", "message": f"echo '{SOURCE}'",
                          "finish_message": f"I refuse because {SECRET}", "usage": {"input_tokens": 10, "evil": "x"},
                          "safety_flagged": [{"category": "HARM_CATEGORY_X", "probability": "HIGH", "blocked": True, "raw": SOURCE}],
                          "prompt": SOURCE, "response_text": SOURCE, "dsn": "postgresql://x", "pinned_model": "gemini-3.5-flash-lite"})
    assert d == {"stage": "request", "status_code": 400, "error_class": "ClientError", "message_chars": len(f"echo '{SOURCE}'"),
                 "finish_message_chars": len(f"I refuse because {SECRET}"), "usage": {"input_tokens": 10},
                 "safety_flagged": [{"category": "HARM_CATEGORY_X", "probability": "HIGH", "blocked": True}], "pinned_model": "gemini-3.5-flash-lite"}
    assert safe_diagnostics({"message": "Request contains an invalid argument."}) == {"message": "Request contains an invalid argument."}


def test_provider_response_diagnostics_do_not_carry_a_finish_message_echo():
    class Cand:
        finish_reason = type("E", (), {"name": "SAFETY"})()
        finish_message = f"Blocked: '{SOURCE}'"
        safety_ratings = []
    class Resp:
        candidates = [Cand()]
        prompt_feedback = None
        usage_metadata = None
        text = None
        response_id = "resp-1"
        model_version = "gemini-3.5-flash-lite"
    d = pr.diagnostics(Resp(), model_id="gemini-3.5-flash-lite", stage="test")
    assert "finish_message" not in d and d.get("finish_message_chars") == len(Cand.finish_message) and SOURCE not in str(d)
    assert d["finish_reason"] == "SAFETY"
