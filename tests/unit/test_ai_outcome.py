"""LLM-00 (2026-10-18): a completed AI job records what the model contributed, apart from the job's success (pure)."""
from infra.worker import ai_outcome as ao


def test_retained_rejected_and_server_items_are_counted_apart():
    qa = ao.summarize("knowledge.qa", {"status": "PARTIAL", "claims": [{"origin": "model"}, {"origin": "server_applicability"}], "dropped": [{}, {}]}, 1)
    assert qa == {"model_attempted": True, "provider_calls": 1, "retained": 1, "rejected": 2, "server_added": 1, "contribution": "retained", "degraded": None,
                  "execution_mode": "undeclared"}                                                       # no declared provider mode: never presented as real
    gone = ao.summarize("gap.answer_interpret", {"proposed_answer": None}, 1)
    assert gone["contribution"] == "rejected" and gone["retained"] == 0                                 # a call happened and nothing it proposed survived
    abst = ao.summarize("knowledge.qa", {"status": "ABSTAINED", "claims": [], "dropped": []}, 0)
    assert abst["contribution"] == "none" and abst["model_attempted"] is False and abst["degraded"] == "abstained"
    var = ao.summarize("document.variant", {"status": "fallback"}, 1)
    assert var["contribution"] == "rejected" and var["degraded"] == "deterministic_fallback"
    iq = ao.summarize("gap.interview_question", {"questioner": "deterministic_fallback", "fallback_reason": "not_grounded"}, 1)
    assert iq["contribution"] == "rejected" and iq["degraded"] == "deterministic_fallback:not_grounded"


def test_other_kinds_and_non_dict_results_are_left_untouched():
    assert ao.annotate("document.generate", {"x": 1}, 0) == {"x": 1} and ao.annotate("knowledge.qa", None, 1) is None
    out = ao.annotate("change.understand", {"operations": [{}], "rejected": [], "refused": [{}]}, 1)
    assert out["ai_outcome"]["retained"] == 1 and out["ai_outcome"]["rejected"] == 1 and out["operations"] == [{}]


def test_execution_mode_follows_the_declared_provider_and_a_job_without_a_call_is_deterministic(monkeypatch):
    assert ao.summarize("knowledge.qa", {"claims": []}, 1, "SIMULATED")["execution_mode"] == "synthetic"
    assert ao.summarize("knowledge.qa", {"claims": []}, 1, "live")["execution_mode"] == "real"
    assert ao.summarize("knowledge.qa", {"claims": []}, 1, "LIVE_DISARMED")["execution_mode"] == "undeclared"   # a disarmed server cannot have made a real call
    assert ao.summarize("knowledge.qa", {"claims": []}, 0, "LIVE")["execution_mode"] == "deterministic"
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "SIMULATED")
    assert ao.annotate("knowledge.qa", {"claims": [{"origin": "model"}]}, 1)["ai_outcome"]["execution_mode"] == "synthetic"



def test_request_assistant_outcome_counts_model_blocks_apart_from_server_authority_lines():
    """AH-05.3: a request-assistant answer whose model blocks were all refused shows `rejected`, never a retained AI contribution; the server's authority
    sentences are server_added; schema validity is never a semantic-verification claim (no such field exists)."""
    from infra.worker import ai_outcome
    refused = {"status": "refused", "blocks": [{"kind": "authority", "text": "You hold the current step."}], "rejected": [{"index": 0}, {"index": 1}]}
    s = ai_outcome.summarize("request.qa", refused, 1, "LIVE")
    assert (s["retained"], s["rejected"], s["server_added"], s["contribution"], s["execution_mode"]) == (0, 2, 1, "rejected", "real")
    kept = {"status": "answered", "blocks": [{"kind": "authority", "text": "x"}, {"kind": "cause", "text": "y"}, {"kind": "fact", "text": "z"}], "rejected": []}
    s2 = ai_outcome.summarize("request.qa", kept, 1, "SIMULATED")
    assert (s2["retained"], s2["server_added"], s2["contribution"], s2["execution_mode"]) == (2, 1, "retained", "synthetic")
    assert "verified" not in str(s2)
    ex = ai_outcome.summarize("extraction", {"candidates": [1, 2], "rejected": [{"reason": "schema_invalid"}]}, 1, None)
    assert (ex["retained"], ex["rejected"], ex["execution_mode"]) == (2, 1, "undeclared")
