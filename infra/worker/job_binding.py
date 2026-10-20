"""What a stored AI result is bound to besides its inputs: the frozen prompt/schema of its role and the pinned model and thinking level (LHA-00 / LLM-00).

A job id that carries this binding makes the same inputs under a changed prompt, schema, model or thinking level a NEW job, never a replay of an answer produced
under the old configuration; a retry of the same job keeps its binding because its id does not change. Without a budget config (disarmed) the model part is None.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os

# job kind -> (worker module that owns the frozen config, pinned role whose model is used)
KINDS = {
    "knowledge.qa": ("infra.worker.knowledge_qa", "knowledge.qa"),
    "change.understand": ("infra.worker.change_understand", "change.understand"),
    "change.impact_deps": ("infra.worker.impact_dependencies", "change.impact_deps"),
    "change.draft_guidance": ("infra.worker.draft_guidance", "change.draft_guidance"),
    "knowledge.gap_proposals": ("infra.worker.knowledge_gap_proposals", "knowledge.gap_proposals"),
    "gap.answer_interpret": ("infra.worker.gap_answer_interpret", "gap.answer_interpret"),
    "gap.interview_question": ("infra.worker.gap_interview_question", "gap.interview"),
    "knowledge.mining": ("infra.worker.knowledge_mining", "knowledge.mining"),
    "change.risk_memo": ("infra.worker.risk_memo", "change.risk_memo"),
    "ma.synthesis_memo": ("infra.worker.synthesis_memo", "ma.synthesis_memo"),
    "ci.semantic_drift": ("infra.worker.ci_semantic_drift", "ci.semantic_drift"),
    "source.correspondence": ("infra.worker.source_correspondence", "source.correspondence"),
    "code.diff_proposal": ("infra.worker.code_diff_proposal", "code.diff_proposal"),
    "roi.analyst": ("infra.worker.roi_analyst", "roi.analyst"),
    "request.qa": ("infra.worker.request_assistant", "request.assistant"),
}


def binding(kind: str) -> dict:
    module, role = KINDS[kind]
    sha = importlib.import_module(module).load_config()[1]
    model = None
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        from infra.worker import budget_config, job_quota
        config = job_quota.load_worker_budget_config()
        model = {"model": budget_config.model_for_role(config, role), "thinking": budget_config.thinking_level_for_role(config, role)}
    return {"config": sha, "model": model}


def fingerprint(kind: str, extra=None) -> str:
    return hashlib.sha256(json.dumps({**binding(kind), "semantic": extra}, sort_keys=True).encode("utf-8")).hexdigest()[:12]
