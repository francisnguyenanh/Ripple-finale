"""Job kind "request.qa", executor "request.qa.v1", pinned role "request.assistant": one question of a private request-assistant thread -> a checked answer (RQA-03).

Prepare (immediately before the provider call) rechecks, from the server's own records: the thread's creator still holds `request.assistant.ask` (not suspended) and may
still read the request; the context pack rebuilt NOW under that person's rights has the same manifest hash as when the question was asked (otherwise `context_changed`:
the person asks again; nothing is answered from a context they could no longer see, or from a state that moved). The prompt carries only that pack, the person's last few
questions with the kept answer blocks of this thread, and the question. Execute makes one pinned call; `core/change_intelligence/v2/request_assistant.check_answer` keeps or
refuses each block. The result also carries the next steps the person may take, computed by the server from the task view (never by the model). Nothing is written to any
business record; the answer is published into the thread only when read back under the same authorization (`infra/adapters/v2_request_threads.py`).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time

from core.change_intelligence.v2 import request_assistant as ra
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import (ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget, ProviderCallCeilingExceeded,
                                          ProviderExecutionError, ProviderRateLimitError, ProviderTimeoutError)
from infra.worker.explain import call_pinned_json, last_service_tier

EXECUTOR_ID = "request.qa.v1"
ROLE = "request.assistant"
ROLE_SUPPLEMENT = "request.assistant.complete"                     # R17: the one supplementary call (its own pin, thinking level and deadline)
KIND = "request.qa"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "request_assistant.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def job_id_for(thread_id: str, message_id: str) -> str:
    from infra.worker import job_binding
    return "rqa-" + hashlib.sha256(json.dumps([thread_id, message_id, job_binding.fingerprint(KIND)]).encode("utf-8")).hexdigest()[:24]


class RequestAssistantExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        from infra.adapters import v2_business
        from infra.adapters import v2_request_assistant as vra
        from infra.adapters import v2_request_tasks
        from infra.adapters.v2_policy import permissions_for
        from infra.adapters.v2_postgres import TenantScopeError
        from infra import operator_flags
        ctx.request_context["rqa_started"] = time.monotonic()                                  # R18: the job's 45 s deadline runs from the attempt's start, over both calls
        if not operator_flags.assistant_on():                                                # switched off after the ask: stop before any provider call (normal accounting)
            raise NonRetryableExecutionError("assistant_off")
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        p = ctx.job["payload"] or {}
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        if not all(isinstance(p.get(k), str) and p.get(k) for k in ("thread_id", "message_id", "family", "request_id", "manifest_hash", "asked_by")):
            raise NonRetryableExecutionError("payload_invalid")
        with ctx.store._tx() as cur:
            cur.execute("""SELECT t.created_by, m.question, m.seq FROM v2_request_threads t JOIN v2_request_messages m ON m.tenant_id=t.tenant_id AND m.workspace_id=t.workspace_id
                           AND m.thread_id=t.thread_id WHERE t.tenant_id=%s AND t.workspace_id=%s AND t.thread_id=%s AND m.message_id=%s""", (tenant, ws, p["thread_id"], p["message_id"]))
            row = cur.fetchone()
        if row is None or row[0] != p["asked_by"]:
            raise NonRetryableExecutionError("thread_mismatch")
        asker, question, seq = row
        roles = v2_business.current_roles(ctx.store, tenant, ws, asker)
        if "request.assistant.ask" not in permissions_for(roles):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        authority = {"tenant_id": tenant, "workspace_id": ws, "account_id": asker, "roles": tuple(roles)}
        kept_tv: dict = {}
        try:
            pack = vra.build_context(ctx.store, tenant, ws, p["family"], p["request_id"], authority, keep=kept_tv)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("request_not_readable", "the person who asked can no longer read this request") from exc
        except ra.ContextOverflow as exc:                                                      # AIG-02: the mandatory facts no longer fit: refused before the provider
            raise NonRetryableExecutionError("context_overflow", exc.reason) from exc
        if pack["manifest_hash"] != p["manifest_hash"]:
            raise NonRetryableExecutionError("context_changed", "the request or the person's access changed after the question; ask again")
        frozen, sha = load_config()
        with ctx.store._tx() as cur:                                                          # the person's own earlier turns of THIS thread, kept blocks only
            cur.execute("""SELECT question, answer FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s AND seq < %s AND answer IS NOT NULL
                           ORDER BY seq DESC LIMIT %s""", (tenant, ws, p["thread_id"], seq, int(frozen["limits"]["max_history_turns"])))
            hist = [{"question": r[0], "blocks": ((r[1] if isinstance(r[1], dict) else json.loads(r[1] or "{}")) or {}).get("blocks") or []} for r in reversed(cur.fetchall())]
        perspective = ", ".join(sorted(roles)) or "member"
        try:
            prompt = ra.build_prompt(frozen, pack, hist, question, perspective=perspective)
        except ValueError as exc:
            raise NonRetryableExecutionError("budget_exceeded", "the request context is too large for one question") from exc
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the request context exceeds the input ceiling")
        tv = kept_tv["task_view"]                                                              # R19: the view the pack was built from, under the same rights, not read again
        next_actions = [{"action": a["action"], "label": a["label"]} for a in tv["permitted_actions"] if a.get("current")]   # only steps the asker is asked to do now
        ctx.request_context.update(rqa_frozen=frozen, rqa_sha=sha, rqa_pack=pack, rqa_next=next_actions, rqa_question=question, rqa_perspective=perspective,
                                   rqa_timing={"prepare_s": round(time.monotonic() - ctx.request_context["rqa_started"], 3), "calls": []})
        planned = 1 + (int(frozen["limits"].get("max_supplement_calls") or 0) if frozen.get("supplement_template") else 0) + (1 if _recovery(frozen) else 0)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=min(planned, MAX_CALLS_PER_QUESTION), external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        timing = rc.setdefault("rqa_timing", {"calls": []})
        model, model_id, actual, in_t, out_t, th_t, recovery = self._answer(ctx, request, calls)
        t_checks = time.monotonic()
        out = ra.check_answer(model, rc["rqa_pack"], rc["rqa_frozen"]["limits"], question=rc.get("rqa_question"))   # checks/20: declared names are verified against the question and the pack
        timing["checks_s"] = round(time.monotonic() - t_checks, 3)
        frozen, limits, pack = rc["rqa_frozen"], rc["rqa_frozen"]["limits"], rc["rqa_pack"]
        calls_made, model2, usage2 = (2 if recovery else 1), None, None
        open_ = ra.open_parts(out, pack["facts"]) if frozen.get("supplement_template") and int(limits.get("max_supplement_calls") or 0) > 0 and recovery is None else []   # R19: never a third call
        if open_ and out["outcome"] in ("answered", "partial", "insufficient_context"):          # R17: at most one supplementary call, only for the open parts
            sup_outcome, out2 = "completed", None
            role2 = frozen.get("supplement_role") or ROLE_SUPPLEMENT
            from infra.worker.budget_config import role_request_timeout
            job_deadline = role_request_timeout(ctx.config, ROLE)                               # R18: ONE deadline for the whole job (45 s), not one per call
            left = (job_deadline - (time.monotonic() - rc.get("rqa_started", time.monotonic()))) if job_deadline else None
            try:
                if role2 not in (getattr(ctx.config, "role_model_ids", None) or {}):
                    raise _NotConfigured()
                if left is not None and left < MIN_SUPPLEMENT_SECONDS:
                    raise _NoTimeLeft()
                prompt2 = ra.supplement_prompt(frozen, pack, rc.get("rqa_question") or "", out, open_, perspective=rc.get("rqa_perspective") or "member")
                from infra.worker import extraction
                if extraction._input_token_bound(prompt2, frozen["output_schema"]) > ctx.config.max_input_tokens_per_call:
                    raise ValueError("context_too_large")
                req2 = ExecutionRequest(prompt=prompt2, schema=frozen["output_schema"], provider_calls_planned=request.provider_calls_planned, external=True)
                obj2, _m2, _a2, in2, out2_t, th2 = call_pinned_json(ctx, req2, calls, role2, set(ra.ANSWER6_KEYS), deadline_left=left)
                calls_made, usage2 = 2, (in2, out2_t, th2)
                model2 = ra.validate_model_object(obj2, limits)
                out2 = ra.check_answer(model2, pack, limits, question=rc.get("rqa_question"))
            except _NotConfigured:
                sup_outcome = "not_configured"
            except _NoTimeLeft:                                                                  # the job's deadline leaves no room: no call, the first pass stands
                sup_outcome = "timeout"
            except ProviderCallCeilingExceeded:
                sup_outcome = "no_call_budget"
            except ProviderTimeoutError as exc:                                                 # the first pass stands; the call is counted and its usage (if any) booked
                sup_outcome, calls_made, usage2 = "timeout", 2, _usage_of(exc)
            except (ProviderExecutionError, ProviderRateLimitError) as exc:
                sup_outcome, calls_made, usage2 = "provider_failed", 2, _usage_of(exc)
            except (NonRetryableExecutionError, ValueError) as exc:                              # schema violation of the second object, or a prompt too large: nothing usable
                sup_outcome = "invalid"
                if usage2 is None and getattr(exc, "usage_outcome", None) is not None:
                    calls_made, usage2 = 2, _usage_of(exc)
            if usage2 is not None and len(rc.get("provider_usage") or []) < calls_made:            # a call that answered after its deadline: its usage, kept traceable
                rc.setdefault("provider_usage", []).append({"model_id": model_id, "input_tokens": usage2[0], "output_tokens": usage2[1], "thought_tokens": usage2[2], "after_deadline": True})
            out = ra.merge_supplement(out, out2, open_, sup_outcome, pack["facts"])
            out["parts"]["supplement"]["time_left_s"] = round(left, 3) if left is not None else None   # audit: what the job's deadline left for the second call
            if calls_made == 2 and usage2 is not None and usage2[0] is not None and usage2[1] is not None:
                in_t, out_t = (in_t or 0) + usage2[0], (out_t or 0) + usage2[1]
                th_t = (th_t or 0) + (usage2[2] or 0) if (th_t is not None or usage2[2] is not None) else None
            elif calls_made == 2:                                                                 # a second call with no reported usage: the attempt is priced fail-closed (estimate)
                in_t = out_t = None
        if recovery is not None:
            out = dict(out, parts=dict(out.get("parts") or {}, recovery={k: recovery[k] for k in ("reason", "primary_deadline_s", "time_left_s")}))
        timing["total_s"] = round(time.monotonic() - rc.get("rqa_started", time.monotonic()), 3)
        result = {"executor_id": EXECUTOR_ID, "config_sha256": rc["rqa_sha"], **out, "next_actions": rc["rqa_next"], "as_of": rc["rqa_pack"]["as_of"], "timing": timing,
                  **({"recovery": recovery} if recovery is not None else {}),
                  "model_answer": model, "checks_version": ra.CHECKS_VERSION, "answer_schema": ra.ANSWER_SCHEMA, "locale": ra.LOCALE,                                                    # the model's own object before the checks (evaluation: raw vs kept); no route returns it
                  "manifest_hash": rc["rqa_pack"]["manifest_hash"], "context_version": rc["rqa_pack"]["version"], "provider_calls": calls_made, "applies_nothing": True, "model": {"pinned": model_id, "served": actual},
                  **({"model_supplement": model2} if model2 is not None else {})}
        tier = last_service_tier(ctx)
        usage_rows = rc.get("provider_usage") or []
        if usage_rows:                                                                         # R7 / R19: what the provider reported serving, with or without a requested tier
            result["model"].update(traffic_type=usage_rows[-1].get("traffic_type"), service_tier=tier, location=usage_rows[-1].get("location") or "global")   # R23: where the answering call ran
            if usage_rows[-1].get("requested_tier"):
                result["model"]["requested_tier"] = usage_rows[-1]["requested_tier"]
        if recovery is not None:                                                                # R19: the answer came from the recovery call; the primary call's outcome is kept
            result["model"].update(answered_by="recovery", traffic_type=recovery.get("recovery_traffic_type"), requested_tier=None, service_tier=tier,
                                   primary={"requested_tier": recovery.get("primary_requested_tier"), "outcome": recovery["reason"]})
            if in_t is None or out_t is None:                                                   # a call with no reported usage: the whole attempt at the priciest ceiling either call could cost
                tier = recovery.get("primary_requested_tier") or tier
        priced = rc.get("rqa_priced_calls")
        if priced and recovery is None and calls_made == 2 and len(priced) == 1:              # R23: a supplement after a located answer call, priced at its own role
            priced = rc["rqa_priced_calls"] = (*priced, _priced(ctx, frozen.get("supplement_role") or ROLE_SUPPLEMENT, usage2))
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t, service_tier=tier,
                                priced_calls=priced if priced and (recovery is not None or len(priced) == calls_made) else None)

    def _answer(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget):
        """R19: the answer call, and at most ONE recovery call on the SAME prompt (same pack, same rights, same snapshot) when the primary call fails technically
        (a timeout at its own shorter deadline, a provider error, a rate limit, an unusable response, an object outside the schema) and enough of the job's deadline
        is left. The recovery call runs under its own role (its own model pin and tier; the evaluated configuration pins the same model at the standard tier) and
        only for the time left. Nothing is retried beyond it; a failed recovery fails the job as before. -> (model, pinned, served, in, out, thought, recovery|None)."""
        rc = ctx.request_context
        frozen, timing = rc["rqa_frozen"], rc["rqa_timing"]
        rec_cfg = _recovery(frozen)
        rec_role = (rec_cfg or {}).get("role")
        enabled = bool(rec_cfg) and rec_role in (getattr(ctx.config, "role_model_ids", None) or {})
        primary_deadline = float(rec_cfg["primary_deadline_s"]) if enabled else None
        t0 = time.monotonic()
        try:
            obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, set(ra.ANSWER6_KEYS), deadline_left=primary_deadline)
            timing["calls"].append({"role": ROLE, "s": round(time.monotonic() - t0, 3), "outcome": "answered", "traffic_type": _last_traffic(rc)})
            if _located(ctx, ROLE):                                                              # R23: an answer call at a multi-region is booked at its own price
                rc["rqa_priced_calls"] = (_priced(ctx, ROLE, (in_t, out_t, th_t)),)                 # (set before validation: a refused answer is booked the same way)
            try:
                return ra.validate_model_object(obj, frozen["limits"]), model_id, actual, in_t, out_t, th_t, None
            except ValueError as exc:
                raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        except (ProviderExecutionError, NonRetryableExecutionError) as exc:
            reason = _recoverable(exc)
            if not timing["calls"] or timing["calls"][-1].get("role") != ROLE:
                timing["calls"].append({"role": ROLE, "s": round(time.monotonic() - t0, 3), "outcome": reason or type(exc).__name__, "traffic_type": None})
            from infra.worker.budget_config import role_request_timeout, service_tier_for_role
            job_deadline = role_request_timeout(ctx.config, ROLE)
            left = (job_deadline - (time.monotonic() - rc.get("rqa_started", t0))) if job_deadline else None
            if not enabled or reason is None or (left is not None and left < float(rec_cfg.get("min_left_s", 8))):
                _keep_timeline(exc, timing, None)
                if _located(ctx, ROLE) and not getattr(exc, "priced_calls", None):              # R23: no recovery: the one call at its own location's usage or ceiling
                    exc.priced_calls = rc.get("rqa_priced_calls") or (_priced(ctx, ROLE, _usage_of(exc)),)   # an answer refused by shape: its reported usage
                raise
            usage1 = _usage_of(exc)
            if usage1 is not None and len(rc.get("provider_usage") or []) < 1:                   # an answer after the primary's deadline / a refused shape: its usage, traceable
                rc.setdefault("provider_usage", []).append({"model_id": getattr(getattr(exc, "usage_outcome", None), "model_id", None), "input_tokens": usage1[0],
                                                            "output_tokens": usage1[1], "thought_tokens": usage1[2], "after_deadline": reason == "timeout"})
            t1 = time.monotonic()
            from infra.worker.budget_config import location_for_role, model_for_role, output_ceiling_for_role, price_key
            p_key = price_key(model_for_role(ctx.config, ROLE), service_tier_for_role(ctx.config, ROLE), location_for_role(ctx.config, ROLE))
            p_call = {"price_key": p_key, "input_tokens": usage1[0] if usage1 else None,
                      "output_tokens": (usage1[1] + (usage1[2] or 0)) if usage1 and usage1[1] is not None else None,
                      "output_ceiling": output_ceiling_for_role(ctx.config, ROLE)}                       # R23: a missing side at this role's own ceiling
            try:
                obj, model_id, actual, in2, out2, th2 = call_pinned_json(ctx, request, calls, rec_role, set(ra.ANSWER6_KEYS), deadline_left=left)
            except (ProviderExecutionError, NonRetryableExecutionError) as exc2:
                # R20 (A157 H5-14): a failed recovery is booked per call too: the recovery call at ITS model and tier (its usage if it reported any, else its
                # ceiling), never at the answer call's price; the worker's failure path reads `priced_calls`
                u2 = _usage_of(exc2)
                exc2.priced_calls = (p_call, {"price_key": price_key(model_for_role(ctx.config, rec_role), service_tier_for_role(ctx.config, rec_role),
                                                                     location_for_role(ctx.config, rec_role)),
                                              "input_tokens": u2[0] if u2 else None, "output_tokens": (u2[1] + (u2[2] or 0)) if u2 and u2[1] is not None else None,
                                              "output_ceiling": output_ceiling_for_role(ctx.config, rec_role)})
                timing["calls"].append({"role": rec_role, "s": round(time.monotonic() - t1, 3), "outcome": _recoverable(exc2) or type(exc2).__name__, "traffic_type": None})
                _keep_timeline(exc2, timing, exc)
                raise
            timing["calls"].append({"role": rec_role, "s": round(time.monotonic() - t1, 3), "outcome": "answered", "traffic_type": _last_traffic(rc)})
            rows = rc.get("provider_usage") or []
            out_known2 = None if out2 is None else out2 + (th2 or 0)
            rec_loc = rows[-1].get("location") if rows and rows[-1].get("location") not in (None, "global") else None
            priced = (p_call, {"price_key": price_key(actual, rows[-1].get("service_tier") if rows else None, rec_loc), "input_tokens": in2, "output_tokens": out_known2,
                               "output_ceiling": output_ceiling_for_role(ctx.config, rec_role)})
            rc["rqa_priced_calls"] = priced                                                     # R19 / R21: each call priced on its own, at its own model, tier and location
            try:
                model = ra.validate_model_object(obj, frozen["limits"])
            except ValueError as exc2:
                err = NonRetryableExecutionError("schema_violation", str(exc2))
                err.priced_calls = priced                                                       # R21: a refused recovered answer is still booked per call
                raise err from exc2
            known = usage1 is not None and usage1[0] is not None and usage1[1] is not None
            in_t = (usage1[0] + in2) if known and in2 is not None else None
            out_t = (usage1[1] + out2) if known and out2 is not None else None
            th_t = ((usage1[2] or 0) + (th2 or 0)) if known and (usage1[2] is not None or th2 is not None) else (th2 if known else None)
            recovery = {"reason": reason, "primary_deadline_s": primary_deadline, "time_left_s": round(left, 3) if left is not None else None,
                        "primary_usage_reported": known, "primary_requested_tier": service_tier_for_role(ctx.config, ROLE),
                        "recovery_traffic_type": _last_traffic(rc), "primary_error": type(exc).__name__,
                        "primary_diagnostics": getattr(exc, "diagnostics", None),                    # R19: the provider's typed safe diagnostics of the failed call
                        "recovery_location": rec_loc or "global"}                                   # R21: where the recovery call was served
            return model, model_id, actual, in_t, out_t, th_t, recovery


def _located(ctx, role: str) -> bool:
    from infra.worker.budget_config import location_for_role
    return location_for_role(getattr(ctx, "config", None), role) is not None


def _priced(ctx, role: str, usage) -> dict:
    """R23: one call priced at its role's own model, tier and location; a side the provider did not report stays None (that call's own ceiling, never 0)."""
    from infra.worker.budget_config import location_for_role, model_for_role, output_ceiling_for_role, price_key, service_tier_for_role
    return {"price_key": price_key(model_for_role(ctx.config, role), service_tier_for_role(ctx.config, role), location_for_role(ctx.config, role)),
            "input_tokens": usage[0] if usage else None, "output_tokens": (usage[1] + (usage[2] or 0)) if usage and usage[1] is not None else None,
            "output_ceiling": output_ceiling_for_role(ctx.config, role)}                              # R23: a missing side at this role's own ceiling


MIN_SUPPLEMENT_SECONDS = 2.0                                    # R18: below this much of the job's deadline left, the second call is not attempted
MAX_CALLS_PER_QUESTION = 2                                        # R19: the answer call plus at most one more (recovery or supplement), never both


def _keep_timeline(exc: BaseException, timing: dict, primary: BaseException | None) -> None:
    """R21 (A160 H4-01: a failed job kept only its last error; when and how the answer call ended was lost): the failed attempt's safe diagnostics carry the
    call timeline (role, seconds, outcome; no prompt, no text) and, after a failed recovery, the answer call's error class and its own safe diagnostics. The
    worker stores them on the attempt row (`provider_diagnostics`)."""
    diag = dict(getattr(exc, "diagnostics", None) or {})
    diag["rqa_calls"] = [{"role": c.get("role"), "s": c.get("s"), "outcome": c.get("outcome")} for c in timing.get("calls") or []]
    diag["rqa_prepare_s"] = timing.get("prepare_s")
    if primary is not None:
        diag["rqa_primary_error"] = type(primary).__name__
        if isinstance(getattr(primary, "diagnostics", None), dict):
            diag["rqa_primary_diagnostics"] = primary.diagnostics
    try:
        exc.diagnostics = diag
    except AttributeError:                                       # an exception type without a writable attribute: nothing more to keep
        pass


def _recovery(frozen) -> dict | None:
    """The recovery block of the frozen config: {role, primary_deadline_s, min_left_s}, or None (no recovery call)."""
    r = (frozen or {}).get("recovery")
    return r if isinstance(r, dict) and r.get("role") and r.get("primary_deadline_s") else None


def _recoverable(exc) -> str | None:
    """The locked technical / validation conditions that allow the recovery call; anything else (configuration, authority, served model, budget) does not."""
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, ProviderRateLimitError):
        return "rate_limited"
    if isinstance(exc, ProviderExecutionError):
        return "provider_failed"
    if isinstance(exc, NonRetryableExecutionError) and getattr(exc, "reason", None) == "schema_violation":
        return "schema_violation"
    return None


def _last_traffic(rc) -> str | None:
    rows = rc.get("provider_usage") or []
    return rows[-1].get("traffic_type") if rows else None


class _NotConfigured(Exception):
    """The supplementary role is not pinned in this server's budget config: the first pass stands, with a note; no provider call is made."""


class _NoTimeLeft(Exception):
    """The job's deadline leaves less than MIN_SUPPLEMENT_SECONDS: the first pass stands, with the timeout note; no provider call is made."""


def _usage_of(exc) -> tuple | None:
    """The usage a failed second call still reported (an answer after its deadline, a refused shape), or None when the provider reported nothing."""
    u = getattr(exc, "usage_outcome", None)
    if u is None:
        return None
    return (getattr(u, "input_tokens", None), getattr(u, "output_tokens", None), getattr(u, "thought_tokens", None))
