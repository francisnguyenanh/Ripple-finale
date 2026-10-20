"""Job kinds "ma.specialist" (one specialist call of a multi-agent run; role `ma.specialist` pinned in the budget config) and "ma.join" (deterministic coordinator step).

The specialist reads ONLY the run's stored authorized context and its rubric, returns typed findings that the server validates against the authorized refs, writes its result into
its own child row BEFORE enqueuing the join job (so the coordinator never races the worker's completion write), and never talks to another child. Authority is rechecked when it
runs (the requester must still hold `ma.run` and `ai.use`; a moved pin fails the child visibly). The join job calls the coordinator's idempotent `advance`.
"""
from __future__ import annotations

import json

from core.change_intelligence.v2 import ma_orchestration as ma
from infra.adapters import v2_ma
from infra.adapters.v2_policy import permissions_for
from infra.worker import extraction
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

EXECUTOR_ID = "ma.specialist.v1"
JOIN_EXECUTOR_ID = "ma.join.v1"


def _clip(text, limit=1200):
    if text is None:
        return ""
    t = json.dumps(text, ensure_ascii=False) if not isinstance(text, str) else text
    return t if len(t) <= limit else t[: limit - 1] + "…"


def build_prompt(frozen: dict, context: dict, role: str, rebuttal_claims: list | None, refs: list, synthesis_inputs: list | None = None, repair: dict | None = None) -> str:
    rules = "\n".join(f"- id={r['rule_id']} | {r.get('title')} | before: {r.get('before')} | after: {r.get('after')}" + ("" if r.get("valid", True) else " | REFUSED") for r in context["rules"]) or "(none)"
    ops = "\n".join(f"- id={o['op_id']} {o.get('kind')} on {o.get('rule_id')}: {o.get('outcome')}" + (f" flags: {'; '.join(x for x in o.get('flags') or [] if x)}" if o.get("flags") else "") for o in context["operations"])
    change = (f"request: {_clip(context.get('request'), 600)}\noperations:\n{ops}" if ops else f"request: {_clip(context.get('request'), 600)}")
    counter = "\n".join(f"- id={c.get('rule_id')} disagrees with id={c.get('with')}: {c.get('title')} {_clip(c.get('text'), 300)}" for c in context["counterevidence"]) or "(none)"
    open_q = "\n".join(f"- id={q.get('ref')}: {_clip(q.get('text'), 300)}" for q in context["open_questions"]) or "(none)"
    files = "\n".join(f"- id={f['ref']} {f.get('action')} {f.get('kind')} {f.get('path') or ''}".rstrip() for f in context.get("files", []))
    if files:
        change += f"\nfiles and systems in the inventory:\n{files}"
    rebuttal = ""
    if rebuttal_claims and frozen.get("rebuttal_template"):
        claims = "\n".join(f"- claim_id={c['claim_id']} [{c['severity']}/{c['category']}] raised by {', '.join(c.get('raised_by', []))}: {c['statement']} (evidence: {', '.join(c['evidence_refs'])})" for c in rebuttal_claims)
        rebuttal = frozen["rebuttal_template"].replace("{claims}", claims)
    analyses = ""
    if synthesis_inputs and frozen.get("analyses_template"):
        lines = []
        for a in synthesis_inputs:
            lines.append(f"[{a['role']}] status {a.get('status')}")
            lines += [f"  - [{f['category']}/{f['severity']}] {f['statement']} (evidence: {', '.join(f.get('evidence_refs', []))})" for f in a.get("findings", [])]
            lines += [f"  note: {n}" for n in a.get("notes", [])]
            if a.get("simulation"):                                                      # LLM-S1: the engine's KPI table, one metric per line, the only numbers to state
                lines += [f"  scenario {k}: {v['label']} (policy {v['policy']})" for k, v in ma.scenario_labels(a["simulation"].get("comparison")).items()]
                lines += [f"  metric {k} = {v}" for k, v in sorted(ma.metric_table(a["simulation"].get("comparison")).items())]
                lines += [f"  limitation: {x}" for x in (a["simulation"].get("comparison") or {}).get("limitations", [])]
        analyses = frozen["analyses_template"].replace("{analyses}", "\n".join(lines) or "(none)")
    code = "\n".join(f"- id={c['ref']} rule={c.get('rule_id')} status={c['status']} path={c.get('path') or '?'}" + (f" module={c.get('module')} functions={', '.join(c.get('functions') or [])}" if c.get("module") else "") + f" binding={c.get('binding_status')}: {c.get('why')}"
                     + (f" | literals: {', '.join(l['literal'] + ' (line ' + str(l['line']) + ')' for l in c.get('literals') or [])}" if c.get("literals") else "")
                     + (f" | observed: {_clip(c.get('observed_logic'), 400)}" if c.get("observed_logic") else "") for c in context.get("code", [])) or "(no code evidence in this context)"
    kind = (frozen.get("role_kinds") or {}).get(role, "findings")
    instruction = (frozen.get("output_instructions") or {}).get(kind, "")
    if repair and frozen.get("repair_template"):
        fb = repair.get("feedback") or {}
        feedback = "\n".join([f"- candidate {fb.get('candidate_job_id')} patch {str(fb.get('patch_hash') or '')[:12]}: verdict {fb.get('verdict')}",
                              f"- protected tests: base {(fb.get('protected') or {}).get('base')} / patched {(fb.get('protected') or {}).get('patched')}"]
                             + [f"- protected command exit {c.get('exit')}: {_clip(c.get('output_tail'), 500)}" for c in (fb.get("commands") or [])[:3]]
                             + [f"- generated case {g.get('name')}: {g.get('reading')} (base {g.get('base')}, patched {g.get('patched')})" for g in (fb.get("regressions") or [])[:6]])
        sites = "\n".join(f"- path={s['path']} line={s['line']} literal={s['literal']}" for s in repair.get("sites") or []) or "(no site found for the current value)"
        analyses = analyses + frozen["repair_template"].replace("{feedback}", feedback).replace("{sites}", sites)
    prompt = frozen["prompt_template"].replace("{rubric}", frozen["rubrics"][role]).replace("{change}", change).replace("{rules}", rules).replace("{counterevidence}", counter) \
        .replace("{open_questions}", open_q).replace("{refs}", "\n".join(f"- {r}" for r in refs) or "(none)").replace("{rebuttal}", rebuttal).replace("{analyses}", analyses) \
        .replace("{code}", code).replace("{output_instruction}", instruction)
    if len(prompt) > frozen["limits"]["max_prompt_chars"]:
        raise NonRetryableExecutionError("context_too_large", "the authorized context exceeds the prompt ceiling")
    return prompt


def _refs_of(context: dict) -> list:
    refs = {r["rule_id"] for r in context["rules"] if r.get("rule_id")} | {c["rule_id"] for c in context["counterevidence"] if c.get("rule_id")} | {c["with"] for c in context["counterevidence"] if c.get("with")} \
        | {o["op_id"] for o in context["operations"] if o.get("op_id")} | {f["ref"] for f in context.get("files", []) if f.get("ref")} | {q["ref"] for q in context["open_questions"] if q.get("ref")} \
        | {c["ref"] for c in context.get("code", []) if c.get("ref")}
    return sorted(refs)


class MaSpecialistExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        for key in ("run_id", "child_id", "role", "round", "change_id", "requested_by"):
            if key not in p:
                raise NonRetryableExecutionError("payload_invalid", key)
        try:
            return self._prepare(ctx, p)
        except BaseException as exc:
            # a child that cannot even be prepared (pin moved, authority revoked, context too large, run not running) is a FAILED child the coordinator must learn about now,
            # not a DISPATCHED row that waits for an operator's advance
            reason = getattr(exc, "reason", None) or type(exc).__name__
            tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
            v2_ma.record_child_result(ctx.store, tenant, ws, p["child_id"], status="FAILED", result={"failed": str(reason)[:120], "role": p["role"], "round": p["round"]})
            self._enqueue_join(ctx.store, tenant, ws, p["run_id"], p["child_id"])
            raise

    def _prepare(self, ctx: ExecutionContext, p: dict) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        run = v2_ma._run(ctx.store, tenant, ws, p["run_id"])
        if run is None:
            raise NonRetryableExecutionError("run_not_found")
        if run["status"] not in ("RUNNING", "JOINING"):
            raise NonRetryableExecutionError("run_not_running", run["status"])
        from infra.adapters import v2_business
        roles = v2_business.current_roles(ctx.store, tenant, ws, p["requested_by"])
        perms = permissions_for(roles)
        if v2_ma.CAPABILITY not in perms or "ai.use" not in perms:
            raise NonRetryableExecutionError("authority_revoked", "the person who started the run may no longer run a review board here")
        if v2_ma.pin_moved(ctx.store, tenant, ws, run["change_id"], run["binding"]["pin"], run["workflow"]):
            raise NonRetryableExecutionError("pin_moved", "the business knowledge moved after the run started")
        frozen, sha = v2_ma.load_config(run["workflow"])
        if p["role"] not in frozen["rubrics"]:
            raise NonRetryableExecutionError("unknown_role", p["role"])
        kind = (frozen.get("role_kinds") or {}).get(p["role"], "findings")
        schema = (frozen.get("output_schemas") or {}).get(kind) or frozen.get("output_schema")
        refs = _refs_of(run["context"])
        repair = None
        if kind == "repair":
            repair = {"feedback": p.get("feedback") or {}, "sites": _repair_sites(run, {**p, "_workspace_id": ws}, frozen)}
            ctx.request_context["ma_repair_sites"] = repair["sites"]
        prompt = build_prompt(frozen, run["context"], p["role"], p.get("rebuttal_claims"), refs, p.get("synthesis_inputs"), repair)
        if extraction._input_token_bound(prompt, schema) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "specialist prompt exceeds the input ceiling")
        ctx.request_context.update({"ma_frozen": frozen, "ma_sha": sha, "ma_refs": refs, "ma_kind": kind, "ma_rule_ids": [r["rule_id"] for r in run["context"]["rules"] if r.get("rule_id")],
                                    "ma_known_claims": [c["claim_id"] for c in p.get("rebuttal_claims") or []]})
        return ExecutionRequest(prompt=prompt, schema=schema, provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc, p = ctx.request_context, ctx.job["payload"]
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        try:
            keys = (ma.MISMATCH_KEYS if rc.get("ma_kind") in ("drift", "mismatch") else ma.TESTS_KEYS if rc.get("ma_kind") == "tests" else ma.SCENARIOS_KEYS if rc.get("ma_kind") == "scenarios"
                    else ma.REPAIR_KEYS if rc.get("ma_kind") == "repair" else ma.INTERPRETATION_KEYS if rc.get("ma_kind") == "interpretation" else ma.SPECIALIST_KEYS)
            obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, v2_ma.ROLE, keys)
            try:
                if rc.get("ma_kind") == "repair":
                    validated = ma.validate_repair_object(obj, rc.get("ma_repair_sites") or [], limits=rc["ma_frozen"]["limits"])
                elif rc.get("ma_kind") in ("drift", "mismatch"):              # "mismatch" = jobs stored before the glossary fix
                    validated = ma.validate_mismatch_object(obj, rc["ma_refs"], rc["ma_rule_ids"], limits=rc["ma_frozen"]["limits"])
                elif rc.get("ma_kind") == "tests":
                    run = v2_ma._run(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], p["run_id"])
                    validated = ma.validate_tests_object(obj, run["context"].get("allowed_modules") or [], run["context"].get("allowed_functions") or [], limits=rc["ma_frozen"]["limits"])
                elif rc.get("ma_kind") == "interpretation":
                    metrics = {}
                    for a in p.get("synthesis_inputs") or []:
                        metrics.update(ma.metric_table((a.get("simulation") or {}).get("comparison")))
                    validated = ma.validate_interpretation_object(obj, metrics, limits=rc["ma_frozen"]["limits"])
                    validated.update(findings=[], disputes=[], rejected_disputes=[])
                elif rc.get("ma_kind") == "scenarios":
                    run = v2_ma._run(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], p["run_id"])
                    validated = ma.validate_scenarios_object(obj, run["context"].get("approver_roles") or [], limits=rc["ma_frozen"]["limits"])
                else:
                    validated = ma.validate_specialist_object(obj, rc["ma_refs"], known_claims=rc["ma_known_claims"], limits=rc["ma_frozen"]["limits"])
            except ValueError as exc:
                raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        except BaseException as exc:
            # the coordinator must learn about a failed child even when it is the last one: the row says FAILED and a join job is created before the error propagates
            reason = getattr(exc, "reason", None) or type(exc).__name__
            v2_ma.record_child_result(ctx.store, tenant, ws, p["child_id"], status="FAILED", result={"failed": str(reason)[:120], "role": p["role"], "round": p["round"]})
            self._enqueue_join(ctx.store, tenant, ws, p["run_id"], p["child_id"])
            raise
        new_candidate = None
        if rc.get("ma_kind") == "repair" and validated["sites"]:
            new_candidate = _enqueue_repair_candidate(ctx, p, validated["sites"])
        result = {"executor_id": EXECUTOR_ID, "run_id": p["run_id"], "child_id": p["child_id"], "role": p["role"], "round": p["round"], "config_sha256": rc["ma_sha"],
                  "sites": validated.get("sites", []), "rationale": validated.get("rationale"), "new_candidate_job_id": new_candidate,
                  "status": validated["status"], "findings": validated["findings"], "rejected": validated["rejected"], "disputes": validated["disputes"],
                  "rejected_disputes": validated["rejected_disputes"], "notes": validated["notes"], "rows": validated.get("rows", []), "cases": validated.get("cases", []), "scenarios": validated.get("scenarios", []), "kind": rc.get("ma_kind", "findings"),
                  "statements": validated.get("statements", []),
                  "model": {"pinned": model_id, "served": actual}, "provider_calls": 1, "applies_nothing": True}
        v2_ma.record_child_result(ctx.store, tenant, ws, p["child_id"], status="COMPLETED", result=result)
        self._enqueue_join(ctx.store, tenant, ws, p["run_id"], p["child_id"])
        return ExecutionOutcome(result=result, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)

    @staticmethod
    def _enqueue_join(store, tenant, ws, run_id, child_id) -> None:
        from infra.worker.verification import ensure_job
        try:
            ensure_job(store, tenant, ws, ma.join_job_id(run_id, child_id), v2_ma.KIND_JOIN, {"run_id": run_id, "after": child_id})
        except Exception:                                                                 # the operator's advance route recovers a missing join (runbook); the child result is durable
            pass

    @staticmethod
    def on_terminal_without_execution(store, tenant_id, workspace_id, job_id, reason) -> None:
        _child_ended_unrun(store, tenant_id, workspace_id, job_id, reason)


def _child_ended_unrun(store, tenant_id, workspace_id, job_id, reason) -> None:
    """Shared by the multi-agent child executors: the worker ended this child's job before any executor code ran; mark the child FAILED (only if still DISPATCHED) and wake the
    coordinator through the child's ordinary join job."""
    rec = store.job_record(tenant_id, workspace_id, job_id)
    p = rec.get("payload") or {}
    if not p.get("run_id") or not p.get("child_id"):
        return
    v2_ma.record_child_result(store, tenant_id, workspace_id, p["child_id"], status="FAILED", result={"failed": str(reason)[:120], "role": p.get("role"), "round": p.get("round"), "unrun": True})
    MaSpecialistExecutor._enqueue_join(store, tenant_id, workspace_id, p["run_id"], p["child_id"])


def _repair_sites(run: dict, p: dict, frozen: dict) -> list:
    """The literal sites of the decided rule's CURRENT value in the base checkout (allowlisted .py files only), scanned by the server; the repairer chooses among them."""
    import pathlib
    import shutil
    from core.change_intelligence.v2 import code_patch
    from infra.worker import code_change as cc
    cand = run["context"].get("candidate") or {}
    literals = [l["literal"] for c in run["context"].get("code", []) for l in c.get("literals") or []]
    values = {code_patch._norm_number(l) for l in literals} - {None}
    if len(values) != 1 or not cand.get("repo_id"):
        return []
    repo = cc.repo_config(run["context"].get("workspace_id") or p.get("workspace_id") or "", cand["repo_id"]) if False else cc.repo_config(p["_workspace_id"], cand["repo_id"])
    checkout = cc.clone(repo["path"], cand.get("base_sha"))
    try:
        files = cc.tracked_files(checkout)
        return code_patch.literal_sites(files, lambda rel: pathlib.Path(checkout, rel).read_text(encoding="utf-8"), before_value=next(iter(values)), allowlist=repo["allowlist"],
                                        protected=repo.get("protected") or code_patch.PROTECTED_DEFAULT, max_sites=int(frozen["limits"].get("max_sites", 12)))
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def _enqueue_repair_candidate(ctx, p: dict, sites: list) -> str:
    """The accepted repair becomes a new deterministic candidate job (same change, same repository, the parent's sites plus the chosen ones); the candidate wakes the coordinator."""
    from infra.worker import code_change as cc
    from infra.worker.verification import ensure_job
    tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
    parent = p["feedback"]["candidate_job_id"]
    parent_rec = ctx.store.job_record(tenant, ws, parent)
    base = parent_rec.get("payload") or {}
    root = base.get("parent_candidate_job_id") or parent
    n = int(p.get("attempt") or 1)
    jid = cc.repair_candidate_job_id(root, n)
    payload = {"change_id": base["change_id"], "repo_id": base["repo_id"], "requested_by": p["requested_by"], "extra_sites": (base.get("extra_sites") or []) + sites,
               "parent_candidate_job_id": parent, "ma_run_id": p["run_id"], "ma_child_id": p["child_id"]}
    ensure_job(ctx.store, tenant, ws, jid, "code.candidate", payload)
    return jid


class MaJoinExecutor:
    external = False
    executor_id = JOIN_EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("run_id"), str):
            raise NonRetryableExecutionError("payload_invalid")
        return ExecutionRequest(prompt="", schema=None, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        p = ctx.job["payload"]
        st = v2_ma.advance(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], p["run_id"], actor_id="worker")
        return ExecutionOutcome(result={"executor_id": JOIN_EXECUTOR_ID, "run_id": p["run_id"], "after": p.get("after"), "status": st["status"], "applies_nothing": True}, model_id=None)
