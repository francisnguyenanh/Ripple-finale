"""Job kinds "change.risk_memo" (SUR-06) and "ma.synthesis_memo" (SUR-08): advisory memos written by a pinned model role over an immutable pack of server facts (one call each).

The pack holds only what every allowed reader of the change / run already sees (rule titles, algorithm severities and reasons, open questions, review-board finding statements,
severities, raisers and resolutions); no source evidence goes into the prompt. The server checks every statement (core/change_intelligence/v2/advisory_memo.py) and lists the
items the memo did not address. A memo is stored on the job only: it changes no severity, verdict, required review, relation or change. The asker's rights are rechecked
when the job runs; a review-board report that moved since the request is refused (`board_report_moved`), never silently re-read.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from core.change_intelligence.v2 import advisory_memo as am
from core.change_intelligence.v2 import business_impact as bim
from infra.adapters import v2_business, v2_business_change
from infra.worker.change_draft import _canon_json
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.explain import call_pinned_json

RISK_KIND = RISK_ROLE = "change.risk_memo"
SYNTH_KIND = SYNTH_ROLE = "ma.synthesis_memo"
_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config"
CONFIG_PATHS = {RISK_KIND: _CONFIG / "change_risk_memo.json", SYNTH_KIND: _CONFIG / "ma_synthesis_memo.json"}
BOARD_READY = ("REVIEW_READY", "INCOMPLETE")
_MINORITY = ("unresolved", "upheld_by_raiser_disputed_by_others")


def load_config(kind: str = RISK_KIND) -> tuple[dict, str]:
    raw = CONFIG_PATHS[kind].read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _owner_config(kind: str) -> tuple[dict, str]:
    """The run reads its config through the module the job binding reads (infra/worker/risk_memo.py, synthesis_memo.py): one source for the id and the run."""
    import importlib
    return importlib.import_module({RISK_KIND: "infra.worker.risk_memo", SYNTH_KIND: "infra.worker.synthesis_memo"}[kind]).load_config()


def board_pack(report: dict) -> list:
    """Facts of a review-board report: each finding (severity floor; must be addressed when it is a minority / unresolved finding or critical / high), every
    unresolved disagreement, missing required roles and the human action the board asks for."""
    pack = []
    for f in sorted(report.get("findings") or [], key=lambda f: f["claim_id"]):
        disputes = "; ".join(f"{d.get('by')}: {d.get('reason')}" for d in f.get("disputes") or [])
        minority = f.get("resolution") in _MINORITY
        text = (f"{f.get('category')} finding raised by {', '.join(f.get('raised_by') or [])}: {f.get('statement')} (resolution {f.get('resolution')}"
                + (f"; disputed: {disputes}" if disputes else "") + ")")
        why = "minority or unresolved finding" if minority else ("critical or high finding" if am.severity_rank(f.get("severity")) and am.severity_rank(f.get("severity")) >= 2 else "")
        pack.append(am.pack_item(f["claim_id"], "finding", text, floor=f.get("severity"), must_address=bool(why), reason=why))
    for i, d in enumerate(report.get("unresolved_disagreements") or []):
        text = d if isinstance(d, str) else json.dumps(d, sort_keys=True, default=str)
        pack.append(am.pack_item(f"disagreement:{i + 1}", "disagreement", text[:500], must_address=True, reason="unresolved disagreement"))
    if report.get("missing_required_roles"):
        pack.append(am.pack_item("board:missing_roles", "missing_roles", "Required reviewer roles without a result: " + ", ".join(report["missing_required_roles"]),
                                 floor="high", must_address=True, reason="required reviewer missing"))
    if report.get("human_action_needed"):
        text = report["human_action_needed"] if isinstance(report["human_action_needed"], str) else json.dumps(report["human_action_needed"], sort_keys=True, default=str)
        pack.append(am.pack_item("board:human_action", "human_action", text[:500], must_address=True, reason="human action asked by the board"))
    return pack


def change_pack(intent: dict, report: dict, rules: dict) -> list:
    """Facts of the impact report of a confirmed change: the operations, each affected rule with the algorithm's severity (floor; HIGH must be addressed), the open gaps."""
    pack = []
    for o in intent["operations"]:
        title = rules[o["rule_id"]]["title"] if o["rule_id"] in rules else o["rule_id"]
        pack.append(am.pack_item(f"op:{o['op_id']}", "operation", bim._action(o, title)))
    for a in report["affected_rules"]:
        text = f"{a['title']} (link {a['link']}; algorithm severity {a['severity']}; " + "; ".join(a.get("reasons") or []) + ")"
        pack.append(am.pack_item(f"rule:{a['rule_id']}", "affected_rule", text, floor=a["severity"], must_address=str(a["severity"]).upper() == "HIGH",
                                 reason="HIGH severity by the algorithm" if str(a["severity"]).upper() == "HIGH" else ""))
    for i, g in enumerate(report.get("gaps") or []):
        pack.append(am.pack_item(f"gap:{i + 1}", "open_question", g.get("text") or g.get("kind"), must_address=True, reason="open question of the impact report"))
    return pack


def validate_payload(kind: str, payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("attempt"), int) or isinstance(payload.get("attempt"), bool) or not 1 <= payload["attempt"] <= 3:
        return "payload_invalid"
    if kind == RISK_KIND:
        if not isinstance(payload.get("intent_id"), str) or not payload["intent_id"].startswith("chg-"):
            return "intent_required"
        if payload.get("board_run_id") is not None and not isinstance(payload.get("board_report_hash"), str):
            return "payload_invalid"
    elif not isinstance(payload.get("run_id"), str) or not isinstance(payload.get("board_report_hash"), str):
        return "run_required"
    return None


def _board(store, tenant, ws, run_id, expected_hash, change_id=None) -> dict:
    from infra.adapters import v2_ma
    run = v2_ma._run(store, tenant, ws, run_id)
    if run is None or run["workflow"] != "review_board" or (change_id and run["change_id"] != change_id):
        raise NonRetryableExecutionError("board_run_not_found", "the review-board run is not a run of this change")
    if run["status"] not in BOARD_READY or run["report"] is None:
        raise NonRetryableExecutionError("board_not_finished", "the review-board run has no final report")
    if run["report_hash"] != expected_hash:
        raise NonRetryableExecutionError("board_report_moved", "the review-board report changed after the request")
    return run


class _MemoExecutor:
    external = True
    kind = role = ""

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        payload = ctx.job["payload"]
        err = validate_payload(self.kind, payload)
        if err:
            raise NonRetryableExecutionError(err)
        frozen, sha = _owner_config(self.kind)
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        asker = ctx.store.job_record(tenant, ws, ctx.job["job_id"]).get("requested_by")
        perms = v2_business.current_permissions(ctx.store, tenant, ws, asker)
        if not {"project.read", "ai.use"} <= perms or "ai_suspended" in v2_business.current_roles(ctx.store, tenant, ws, asker):
            raise NonRetryableExecutionError("authority_revoked", "the person who asked no longer holds the permission")
        change_text, pack, binding = self._pack(ctx, tenant, ws, payload)
        limit = int(frozen["limits"]["max_pack_items"])
        must = [p for p in pack if p["must_address"]]
        pack = (must + [p for p in pack if not p["must_address"]])[:max(limit, len(must))]       # must-address facts are never cut to fit the cap
        prompt = frozen["prompt_template"].replace("{change}", change_text).replace("{facts}", am.prompt_lines(pack))
        if len(prompt) > int(frozen["limits"]["max_prompt_chars"]) * 2:
            raise NonRetryableExecutionError("budget_exceeded", "the facts exceed the memo's prompt limit")
        from infra.worker import extraction
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "the facts exceed the input ceiling")
        ctx.request_context.update(memo_frozen=frozen, memo_sha=sha, memo_pack=pack, memo_binding=binding)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        rc = ctx.request_context
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, self.role, am.MODEL_KEYS)
        try:
            model = am.validate_model_object(obj, rc["memo_frozen"]["limits"], rc["memo_frozen"]["statement_kinds"])
        except ValueError as exc:
            raise NonRetryableExecutionError("schema_violation", str(exc)) from exc
        memo = am.check(model, rc["memo_pack"], rc["memo_frozen"]["limits"])
        result = {"schema": am.SCHEMA, "executor_id": self.executor_id, "config_sha256": rc["memo_sha"], **rc["memo_binding"], "model_status": model["status"],
                  "memo": memo, "pack": [{k: p[k] for k in ("ref", "kind", "floor", "must_address")} for p in rc["memo_pack"]], "label": am.LABEL,
                  "provider_calls": 1, "applies_nothing": True, "evidence_in_prompt": False, "model": {"pinned": model_id, "served": actual}}
        return ExecutionOutcome(result=json.loads(_canon_json(result)), model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)


class RiskMemoExecutor(_MemoExecutor):
    kind, role, executor_id = RISK_KIND, RISK_ROLE, "change.risk_memo.v1"

    def _pack(self, ctx, tenant, ws, payload):
        got = v2_business_change.pinned_for_intent(ctx.store, tenant, ws, payload["intent_id"])
        if got is None:
            raise NonRetryableExecutionError("intent_not_confirmed", "the change request is not confirmed")
        intent, snap, artifacts, pin = got
        rules = {r["rule_id"]: r for r in snap["rules"]}
        report = bim.build_report(intent["operations"], rules, snap["relations"], pin=pin, known_artifacts=artifacts)
        pack = change_pack(intent, report, rules)
        binding = {"intent_id": payload["intent_id"], "pin": pin, "report_hash": report["report_hash"], "board_run_id": None, "board_report_hash": None}
        if payload.get("board_run_id"):
            run = _board(ctx.store, tenant, ws, payload["board_run_id"], payload["board_report_hash"], change_id=payload["intent_id"])
            pack += board_pack(run["report"])
            binding.update(board_run_id=payload["board_run_id"], board_report_hash=run["report_hash"])
        return intent["request"], pack, binding


class SynthesisMemoExecutor(_MemoExecutor):
    kind, role, executor_id = SYNTH_KIND, SYNTH_ROLE, "ma.synthesis_memo.v1"

    def _pack(self, ctx, tenant, ws, payload):
        run = _board(ctx.store, tenant, ws, payload["run_id"], payload["board_report_hash"])
        intent = v2_business_change.read_intent(ctx.store, tenant, ws, run["change_id"])
        request = (intent or {}).get("request") or "the change reviewed by the board"
        return request, board_pack(run["report"]), {"run_id": payload["run_id"], "change_id": run["change_id"], "board_report_hash": run["report_hash"],
                                                     "board_status": run["status"]}
