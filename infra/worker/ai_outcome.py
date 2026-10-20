"""What the model actually contributed to a completed AI job (LLM-00): attempted / retained / rejected, kept apart from the job's own success.

A completed job is not a successful AI contribution by itself: the server may reject every proposal, keep only part of it, add its own deterministic items, or
fall back to deterministic content. `annotate` adds `ai_outcome` to the stored result of the AI job kinds below, computed from what the server kept:
  model_attempted / provider_calls, retained (model proposals kept), rejected (model proposals refused), server_added (deterministic items the server added),
  contribution: retained | rejected (the model was called and nothing it proposed survived) | none (no model call), and a degraded reason when the job fell back.
It never changes what the job decided; it only counts it. Readers use it to tell valid AI output from degraded assistance.
"""
from __future__ import annotations


def _qa(r):
    claims = r.get("claims") or []
    server = sum(1 for c in claims if c.get("origin") == "server_applicability")
    return len(claims) - server, len(r.get("dropped") or []), server, ("abstained" if r.get("status") == "ABSTAINED" else None)


def _understand(r):
    return len(r.get("operations") or []), len(r.get("rejected") or []) + len(r.get("refused") or []), 0, None


def _interpret(r):
    kept = 1 if r.get("proposed_answer") else 0
    return kept, 1 - kept, 0, None


def _accepted_rejected(r):
    return len(r.get("accepted") or []), len(r.get("rejected") or []), 0, None


def _proposals(r):
    return len(r.get("gaps") or []) + len(r.get("relations") or []), len(r.get("rejected") or []), 0, None


def _mining(r):
    kept = len(r.get("hypotheses") or []) + len(r.get("candidate_pairs") or []) + len(r.get("classified") or [])     # group: hypotheses; candidates: pairs; unbound: classifications
    return kept, len(r.get("rejected") or []), 0, None


def _variant(r):
    fallback = r.get("status") == "fallback"
    return (0 if fallback else 1), (1 if fallback else 0), 0, ("deterministic_fallback" if fallback else None)


def _interview(r):
    model = r.get("questioner") == "model"
    return (1 if model else 0), (0 if model else 1), 0, (None if model else f"deterministic_fallback:{r.get('fallback_reason')}")


def _specialist(r):
    kept = len(r.get("findings") or []) + len(r.get("cases") or []) + len(r.get("scenarios") or []) + len(r.get("rows") or []) + len(r.get("sites") or [])
    return kept, len(r.get("rejected") or []) + len(r.get("rejected_disputes") or []), 0, None


def _memo(r):
    m = r.get("memo") or {}
    return len(m.get("kept") or []), len(m.get("rejected") or []), 0, None


def _drift(r):
    s = r.get("semantic") or {}
    return len(s.get("kept") or []), len(s.get("rejected") or []), 0, ("nothing_sendable" if r.get("model_status") == "not_run" else None)


def _correspondence(r):
    return len(r.get("proposals") or []), len(r.get("rejected") or []), 0, ("nothing_to_reconnect" if r.get("model_status") == "not_run" else None)


def _code_proposal(r):
    return len(r.get("kept_edits") or []), len(r.get("rejected") or []), 0, ("nothing_offered" if r.get("model_status") == "not_run" else None)


def _roi_analyst(r):
    kept = len(r.get("statements") or []) + len(r.get("assumption_questions") or []) + len(r.get("scenarios") or [])
    return kept, len(r.get("rejected") or []), 0, None


EXTRACT = {"knowledge.qa": _qa, "change.understand": _understand, "gap.answer_interpret": _interpret, "change.impact_deps": _accepted_rejected,
           "change.draft_guidance": _accepted_rejected, "knowledge.gap_proposals": _proposals, "knowledge.mining": _mining, "document.variant": _variant,
           "gap.interview_question": _interview, "ma.specialist": _specialist, "change.risk_memo": _memo, "ma.synthesis_memo": _memo, "ci.semantic_drift": _drift, "source.correspondence": _correspondence, "code.diff_proposal": _code_proposal, "roi.analyst": _roi_analyst}


def summarize(kind: str, result, provider_calls: int):
    if kind not in EXTRACT or not isinstance(result, dict):
        return None
    retained, rejected, server_added, degraded = EXTRACT[kind](result)
    attempted = int(provider_calls or 0) > 0
    contribution = "none" if not attempted else ("retained" if retained > 0 else "rejected")
    return {"model_attempted": attempted, "provider_calls": int(provider_calls or 0), "retained": retained, "rejected": rejected, "server_added": server_added,
            "contribution": contribution, "degraded": degraded}


def annotate(kind: str, result, provider_calls: int):
    summary = summarize(kind, result, provider_calls)
    return result if summary is None else {**result, "ai_outcome": summary}
