"""A SUGGESTED post-change check, built by the server from validated data, for an authorised person to confirm (never saved by itself).

Why: asking an approver to type a file path, a dotted key and a value is not a business task. Everything needed is already stored:
  * WHERE to look (file, key / cell, kind) comes from the typed proposal edit, which the server already validated against the stored file;
  * WHAT must be there afterwards comes from the CONFIRMED REQUEST (the value the requester confirmed), NOT from the proposal's own `after`.
    The two are then compared: if the proposal does not agree with the confirmed request the suggestion says so (that disagreement is what the
    check exists to catch) and a person has to look. The proposal's value is never used as the expected value;
  * with several edits, or a request that carries no single value, there is no confirmed value to use: the item is left for the person to state
    (`needs_human_value`), in plain words - nothing is guessed.
The suggestion is bound to the proposal (its blob hashes and edits) and to the source revision it was built on: if either moves, it is `stale` and
cannot be confirmed. It is a MECHANICAL check (the stored file says the value). It does not prove the change does what the business wanted; the
`limits` field says so, and the business check stays with the approver and the reviewer.

No AI is used: the sentence is a fixed template over the confirmed request, so a person reads exactly what the server will test.
"""

from __future__ import annotations

import re

from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.verification_contract import EDIT_KIND_TO_CONTRACT_KIND, normalize_locator

LIMITS = (
    "This check is mechanical: after the change is applied the system reads the file again and compares the value. It shows the value was written.",
    "It does not show that the change achieves what the business wanted, or that other documents agree. The review, the simulation and the approver's judgement cover that.",
)


def _number(value):
    """A request value as a number when it is one ("300,000", 300000, "300000 JPY"); otherwise None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(-?\d[\d,]*(?:\.\d+)?)\s*[A-Za-z%]*\s*", value)
        if m:
            text = m.group(1).replace(",", "")
            return float(text) if "." in text else int(text)
    return None


def _same(a, b) -> bool:
    na, nb = _number(a), _number(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b


def _shown(value, currency) -> str:
    n = _number(value)
    if n is not None and not isinstance(value, bool):
        text = f"{n:,}"
        return f"{currency} {text}" if currency else text
    return str(value)


def applied_job_id(store, tenant: str, ws: str, proposal_job_id: str) -> str | None:
    """The completed apply job of THIS proposal (apply -> approval -> proposal), or None. Read-only."""
    with store._tx() as cur:
        cur.execute("""SELECT a.job_id FROM v2_jobs a JOIN v2_jobs ap ON ap.tenant_id=a.tenant_id AND ap.workspace_id=a.workspace_id AND ap.job_id = a.payload->>'approval_job_id'
            WHERE a.tenant_id=%s AND a.workspace_id=%s AND a.kind='apply' AND a.execution_status='COMPLETED' AND ap.payload->>'proposal_job_id'=%s LIMIT 1""",
                    (tenant, ws, proposal_job_id))
        row = cur.fetchone()
        return row[0] if row else None


def was_applied(store, tenant: str, ws: str, proposal_job_id: str) -> bool:
    return applied_job_id(store, tenant, ws, proposal_job_id) is not None


def staleness(store, tenant: str, ws: str, proposal: dict) -> str | None:
    """Why a contract/suggestion of this proposal no longer applies, or None. A contract is for ONE proposal on ONE source revision.
    A proposal that was applied is HISTORY (the head moved BECAUSE of it): it is not 'out of date', the contract it carried served its verification."""
    pinned = ((proposal.get("execution_result") or {}).get("pinned_input") or {}).get("pinned_source_revision_id")
    head = store.head(tenant, ws)
    if pinned and head.source_revision_id and pinned != head.source_revision_id:
        if was_applied(store, tenant, ws, proposal.get("job_id")):
            return None
        return "the files changed after this proposal was prepared (a newer revision exists); prepare a new proposal"
    return None


def build(store, tenant: str, ws: str, proposal_job_id: str) -> dict:
    proposal = store.job_record(tenant, ws, proposal_job_id)
    if proposal.get("kind") != "proposal":
        raise TenantScopeError("not a proposal job")
    res = proposal.get("execution_result") or {}
    base = {"proposal_job_id": proposal_job_id, "mechanical_only": True, "limits": list(LIMITS), "uses_ai": False}
    if proposal.get("execution_status") != "COMPLETED" or not res.get("edits"):
        return {**base, "items": [], "stale": False, "complete": False, "missing": ["the proposal is not ready or has no edit to check"]}
    pinned = (res.get("pinned_input") or {}).get("pinned_source_revision_id")
    kind = EDIT_KIND_TO_CONTRACT_KIND.get(res.get("edit_kind"))
    stale = staleness(store, tenant, ws, proposal)
    path = None
    if pinned:
        try:
            view = store.source_revision_view(tenant, ws, pinned)
            path = next((a["logical_path"] or a["artifact_id"] for a in view["artifacts"] if a["artifact_id"] == res.get("target_artifact_id")), None)
        except TenantScopeError:
            path = None
    sf = {}
    try:
        sf = (store.get_intent(tenant, ws, res.get("intent_id")) or {}).get("structured_form") or {}
    except Exception:
        sf = {}
    edits = res.get("edits") or []
    confirmed = sf.get("new_value") if len(edits) == 1 else None
    currency = sf.get("currency")
    subject = (sf.get("subject") or "").strip()
    items, missing = [], []
    if kind is None or not path:
        missing.append("this kind of edit has no automatic check, or its file could not be resolved")
    for e in edits if (kind and path) else []:
        locator = normalize_locator(kind, e.get("path") if kind == "yaml_value_equals" else f"{e.get('sheet')}!{e.get('cell')}")
        if locator is None:
            missing.append("an edit has no valid location to check")
            continue
        proposed = e.get("after")
        has_value = confirmed is not None and (_number(confirmed) is not None or (isinstance(confirmed, str) and confirmed.strip() != ""))
        expected = (_number(confirmed) if _number(confirmed) is not None else confirmed) if has_value else None
        agrees = None if expected is None else _same(expected, proposed)
        what = subject or locator
        item = {
            "kind": kind, "logical_path": path, "locator": locator, "expected": expected,
            "needs_human_value": expected is None, "agrees_with_proposal": agrees,
            "proposal_shows": {"before": e.get("before"), "after": proposed},
            "sentence": (f"After this change, {what} should be {_shown(expected, currency)} in {path.split('/')[-1]}." if expected is not None
                         else f"After this change, the value at {locator} in {path.split('/')[-1]} must be a value you state."),
            "basis": "confirmed_request" if expected is not None else "needs_a_person",
            "note": "Confirmed request: " + (subject or "value") if expected is not None else "",
        }
        if expected is None:
            missing.append(f"no single confirmed value exists for {locator}: a person has to state what must be there")
        elif agrees is False:
            missing.append(f"the proposal writes {proposed} at {locator} but the confirmed request says {expected}: look before confirming")
        items.append(item)
    digest = canonical_hash({"proposal_job_id": proposal_job_id, "pinned": pinned, "blobs": [res.get("before_blob_hash"), res.get("after_blob_hash"), res.get("staged_blob_hash")],
                             "items": [(i["kind"], i["logical_path"], i["locator"], i["expected"]) for i in items]})
    return {**base, "items": items, "stale": bool(stale), "stale_reason": stale, "source_revision_id": pinned, "suggestion_sha256": digest,
            "complete": bool(items) and not stale and not missing, "missing": missing}
