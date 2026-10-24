"""Request and read the SUR-07 ROI analyst memo (`roi.analyst`) over a SYNTHETIC replay dataset.

One job per (dataset hash, revisions, assumptions, attempt, frozen config + pinned model); the same request again makes no new call; a read never calls a model. Real data is
refused before any job exists: a dataset must be marked `synthetic: true` and labelled `SYNTHETIC...` until a data owner grants real data.
"""
from __future__ import annotations

import hashlib
import json

from core.change_intelligence.v2 import roi_analyst as ra
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters import v2_advisory_memo

KIND = "roi.analyst"
_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the metric pack was too large to send",
                 "schema_violation": "the model's answer could not be used", "revision_not_accepted": "a named revision is not accepted knowledge",
                 "replay_refused": "the replay engine refused the inputs", "real_data_not_granted": "real data has not been granted", "role_model_not_pinned": "no model is configured"}


def job_id(records, baseline_revision: int, target_revision: int, assumptions: dict, label: str, attempt: int) -> str:
    from infra.worker import job_binding
    key = [canonical_hash({"records": records}), baseline_revision, target_revision, canonical_hash({"a": assumptions}), label, attempt, job_binding.fingerprint(KIND)]
    return "roa-" + hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()[:24]


def state_of(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running"
    p = rec.get("payload") or {}
    out = {"job_id": rec.get("job_id"), "attempt": p.get("attempt"), "label": ra.LABEL, "synthetic": True, "dataset_label": p.get("label"), "status": status}
    if status in ("failed", "cancelled"):
        code = rec.get("execution_reason") or ""
        key = next((k for k in _REASON_WORDS if code.startswith(k)), None)
        out.update(reason="No analyst memo was written" + (f": {_REASON_WORDS[key]}" if key else "") + ".", reason_code=key or "other")
    elif status == "done" and rec.get("execution_result"):
        res = rec["execution_result"]
        out.update({k: res[k] for k in ("model_status", "statements", "assumption_questions", "scenarios", "rejected", "metrics", "report_hash", "dataset",
                                        "baseline_revision", "target_revision")}, model=res.get("model"), ai_outcome=res.get("ai_outcome"),
                   notes=res.get("notes") or [])                                                          # config v2: the model's doubts about the data (memos before v2 have none)
    else:
        out["reason"] = "The memo is being written."
    return out


def request(store, tenant, ws, jid, payload, authority, *, quota) -> dict:
    return v2_advisory_memo.request(store, tenant, ws, KIND, jid, payload, authority, quota=quota, render=state_of)
