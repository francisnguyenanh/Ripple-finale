"""Job kind "sim.analysis", executor_id "sim_analysis.v1", role "simulation.analyze": an AI reading of a STORED simulation result.

The server builds every fact (`build_facts`) from stored records the caller may read: the simulation job (tested edits, counts, changed
cases, coverage, what was not simulated, limits), the draft it was built from (what is still open) and, when named, the impact job.
The model separates (1) the measured change (count and direction restated, case refs), (2) its interpretation, (3) gaps, (4) options and
(5) questions. The server checks: refs are real case refs of THIS simulation, count and direction equal the measured ones, every number in
the text is a number from the facts, the text is safe. It cannot prove a causal sentence true, so interpretation is labelled AI
interpretation and a statement without a case ref is marked ungrounded. Nothing here verifies, resolves a gap, approves or applies.
Role `simulation.analyze` must be pinned in the budget config (no fallback): without it the job fails closed before any call.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from infra import chat_safety
from infra.adapters.v2_postgres import StoreConflict, TenantScopeError
from infra.worker import extraction
from infra.worker.change_draft import _canon_json, _clip
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.explain import _ID, call_pinned_json, numbers_in

EXECUTOR_ID = "sim_analysis.v1"
ROLE = "simulation.analyze"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "sim_analysis.json"
MAX_REFS = 40
_BASES = ("untested_edit", "draft_unaddressed", "draft_not_covered", "draft_open_item", "simulation_limit", "model_inference")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _rec(store, tenant, ws, job_id, kind=None):
    try:
        rec = store.job_record(tenant, ws, job_id)
    except Exception as exc:
        raise TenantScopeError("subject not found") from exc
    if kind and rec.get("kind") != kind:
        raise TenantScopeError("subject not found")
    return rec


def build_facts(store, tenant: str, ws: str, simulation_job_id: str, impact_job_id: str | None = None) -> dict:
    sim_rec = _rec(store, tenant, ws, simulation_job_id, "simulation")
    if sim_rec.get("execution_status") != "COMPLETED":
        raise StoreConflict("the simulation is not finished")
    res = sim_rec.get("execution_result") or {}
    runs = [f for f in (res.get("families") or []) if isinstance(f, dict) and isinstance(f.get("harbor"), dict) and f.get("status") == "EXECUTED"]
    if not runs and isinstance(res.get("harbor"), dict) and res.get("status") == "EXECUTED":
        runs = [{"case": res["harbor"].get("case"), "status": "EXECUTED", "harbor": res["harbor"]}]
    if res.get("status") not in ("EXECUTED", "PARTIAL") or not runs:
        raise StoreConflict("no executed simulation result to analyse (status %s)" % res.get("status"))
    built = res.get("built_from") or {}
    facts: dict = {"subject": "The result of a simulation of a requested change.",
                   "facts_origin": {"simulation_job_id": simulation_job_id, "draft_job_id": built.get("draft_job_id"),
                                    "impact_job_id": impact_job_id, "source_revision_id": built.get("baseline_source_revision_id") or (res.get("pinned_input") or {}).get("pinned_source_revision_id")},
                   "simulation": {"status": res.get("status"), "direction": res.get("direction") or "as_submitted",
                                  "not_simulated_reason": res.get("reason"), "families": []}}
    refs: dict = {}
    for run in runs:
        h = run["harbor"]
        fam = {"case": run.get("case"), "cases_simulated": h.get("transactions"), "changed_count": h.get("changed_count"),
               "changes_not_explained": h.get("unexplained_changes"),
               "edits": [{"key": e.get("path"), "from": e.get("from"), "to": e.get("to")} for e in h.get("edits") or []],
               "outcomes_before": h.get("by_route_before"), "outcomes_after": h.get("by_route_after"),
               "limits": [str(x)[:240] for x in (h.get("limits") or [])[:4]],
               "edits_the_code_does_not_read": len(h.get("inert_edits") or []),
               "behaviour_not_controlled_by_config": len(h.get("not_controlled") or [])}
        facts["simulation"]["families"].append(fam)
        for row in (h.get("changed") or [])[:MAX_REFS]:
            ref = f"{run.get('case')}:{row.get('id')}"
            refs[ref] = {"amount": row.get("amount_jpy"), "label": row.get("label"), "before": (row.get("before") or {}).get("outcome"),
                         "after": (row.get("after") or {}).get("outcome"), "caused_by": [c.get("config_key") for c in row.get("cause") or []]}
    facts["case_refs"] = dict(list(refs.items())[:MAX_REFS])
    facts["changed_count_total"] = sum(f["changed_count"] or 0 for f in facts["simulation"]["families"])
    facts["untested_edits"] = [{"file": u.get("logical_path"), "key": u.get("path")} for u in (res.get("untested_edits") or [])]
    if built.get("draft_job_id"):
        try:
            d = (_rec(store, tenant, ws, built["draft_job_id"], "change.draft").get("execution_result") or {})
            facts["draft_open"] = {
                "summary": _clip(d.get("summary"), 400),
                "unaddressed": [{"file": u.get("logical_path"), "where": u.get("location")} for u in (d.get("unaddressed") or [])[:6]],
                "not_covered": [{"file": u.get("logical_path"), "reason": _clip(u.get("reason"), 120)} for u in (d.get("not_covered") or [])[:6]],
                "open_items": [_clip(o.get("question") or o.get("summary") or o.get("reason") or "", 160)
                               for o in (d.get("open_items") or [])[:6] if isinstance(o, dict)],
                "questions_for_human": [_clip(q if isinstance(q, str) else (q or {}).get("question", ""), 160)
                                        for q in (d.get("questions_for_human") or [])[:4]]}
        except TenantScopeError:
            facts["draft_open"] = None
    if impact_job_id:
        try:
            ir = (_rec(store, tenant, ws, impact_job_id, "impact").get("execution_result") or {})
            facts["impact_hints"] = {"direct": len(ir.get("direct") or []), "indirect": len(ir.get("indirect") or []),
                                     "unknown": len(ir.get("unknown") or [])}
        except TenantScopeError:
            facts["impact_hints"] = None
    return facts


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict) or not payload["facts"] \
            or not isinstance(payload.get("snapshot"), dict):
        return "facts_required"
    return None


def verify_output(obj: dict, facts: dict, limits: dict) -> dict:
    """Server checks on the model's object. Raises NonRetryableExecutionError (rejected, nothing stored) or returns the result body."""
    def bad(why):
        raise NonRetryableExecutionError("analysis_rejected", why)
    allowed_refs = set((facts.get("case_refs") or {}).keys())
    allowed_numbers = numbers_in(facts) | set(map(float, range(0, 11)))
    max_chars = limits["max_statement_chars"]

    def clean(text):
        if not isinstance(text, str) or not text.strip():
            bad("empty_text")
        t = " ".join(text.split())
        if len(t) > max_chars:
            bad("text_too_long")
        if chat_safety.unsafe_output(t) or _ID.search(t):
            bad("unsafe_or_id")
        if [n for n in numbers_in(t) if n not in allowed_numbers]:
            bad("number_not_in_facts")
        return t

    def refs_of(value):
        if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
            bad("case_refs_must_be_a_list_of_strings")
        if [r for r in value if r not in allowed_refs]:
            bad("unknown_case_ref")
        return list(dict.fromkeys(value))

    for key, cap in limits["max_items"].items():
        if not isinstance(obj.get(key), list) or len(obj[key]) > cap:
            bad(f"bad_or_too_many_{key}")
    ch = obj["changed"]
    if not isinstance(ch, dict) or ch.get("changed_count") != facts.get("changed_count_total"):
        bad("changed_count_differs_from_the_measured_count")
    measured_direction = (facts.get("simulation") or {}).get("direction")
    if ch.get("direction") != measured_direction:
        bad("direction_differs_from_the_measured_direction")
    changed = {"changed_count": ch["changed_count"], "direction": measured_direction, "case_refs": refs_of(ch.get("case_refs"))[:5]}
    interpretation = []
    for item in obj["interpretation"]:
        r = refs_of(item.get("case_refs"))
        interpretation.append({"statement": clean(item.get("statement")), "case_refs": r, "grounded_in_cases": bool(r)})
    gaps = []
    for item in obj["gaps"]:
        if item.get("basis") not in _BASES:
            bad("unknown_gap_basis")
        gaps.append({"statement": clean(item.get("statement")), "basis": item["basis"]})
    options = [{"option": clean(o.get("option")), "assumption": clean(o.get("assumption"))} for o in obj["options"]]
    questions = [clean(q) for q in obj["questions"]]
    return {"changed": changed, "interpretation": interpretation, "gaps": gaps, "options": options, "questions": questions}


class SimAnalysisExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        if validate_payload(ctx.job["payload"]):
            raise NonRetryableExecutionError("facts_required")
        if ROLE not in (getattr(config, "role_model_ids", None) or {}):
            raise NonRetryableExecutionError("role_model_not_pinned", f"role_model_ids has no {ROLE!r} entry")
        frozen, sha = load_config()
        facts = ctx.job["payload"]["facts"]
        blob = _canon_json(facts)
        if len(blob) > frozen["limits"]["max_facts_chars"]:
            raise NonRetryableExecutionError("budget_exceeded", "facts exceed the limit")
        prompt = frozen["prompt_template"].replace("{facts}", blob)
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "analysis request exceeds input ceiling")
        ctx.request_context.update(sa_facts=facts, sa_limits=frozen["limits"], sa_sha=sha, sa_snapshot=ctx.job["payload"]["snapshot"])
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        keys = {"changed", "interpretation", "gaps", "options", "questions"}
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, keys)
        rc = ctx.request_context
        try:
            body = verify_output(obj, rc["sa_facts"], rc["sa_limits"])
        except NonRetryableExecutionError as exc:
            exc.usage_outcome = ExecutionOutcome(result={}, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
            raise
        result = {**body, "snapshot": rc["sa_snapshot"], "executor_id": EXECUTOR_ID, "draft_only": True, "applies_nothing": True,
                  "labels": {"changed": "Measured by the simulation (restated)", "interpretation": "AI interpretation, not a measured fact",
                             "gaps": "Open or untested, from stored facts", "options": "Suggestions for a person, not decisions"},
                  "provider_calls": 1, "config_sha256": rc["sa_sha"],
                  "endpoint": {"backend": "vertex", "location": "global", "pinned_model": model_id, "served_model": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t,
                                thought_tokens=th_t)
