# -*- coding: utf-8 -*-
"""Executable simulation sandbox, job kind "simulation", executor_id
"simulation.run.v1".

Contract:

  - Reuses the generic `POST /v2/jobs` / `GET /v2/jobs/{id}` route pair, same
    as `impact`/`relation`/`finding` (no second dispatch mechanism).
  - Zero provider calls (`external = False`): every corpus/candidate input
    here is caller-supplied, deterministic, already-pinned job payload --
    same posture as ImpactAnalysisExecutor.
  - The FIXED, repo-authored before/after harness programs (never requester-
    derived code) run in a separate child process via
    `infra.worker.sandbox_runner.run_isolated`, a resource-limited
    subprocess that is advisory only and NOT a security boundary. This module
    only ever reads the SANDBOXED OUTPUT back and, for the "hd2" adapter,
    hands per-case outcomes to `core.change_intelligence.simulation`'s pure
    comparison helpers (`CaseResult`) to classify them -- `simulation.py`
    itself never executes anything.
  - Deterministic "config-only" policy expressions (`adapter ==
    "deterministic_dsl"`) are parsed and evaluated by
    `core.change_intelligence.simulation_dsl` -- an AST allowlist, never
    `eval`/`exec`/`compile`, rejecting an out-of-allowlist expression at
    `prepare()` time with a `NonRetryableExecutionError` (-> job FAILED,
    real provenance, never a silently-ignored policy).
  - Denominator accounting: every corpus record is
    classified into exactly one of known/excluded/unknown/malformed/
    duplicate, per-currency, before any execution -- never silently
    coerced or dropped. An empty corpus is EXECUTED with an explicit
    `total == 0` denominator, never a fabricated "0 affected, all clear"
    without evidence it examined anything.
  - `adapter` values other than the ones this executor actually runs
    (`"hd2"`, `"deterministic_dsl"`) yield `status == "NOT_EXECUTABLE"`
    with an explicit reason -- never a fabricated EXECUTED/zero-impact
    result.
  - `payload.candidate_kind` in the malicious-probe set below routes
    straight to the real sandboxed runner with a fixed probe program, so
    the isolation guarantees can be exercised through the real mechanism
    (network/host-fs/OOM/timeout/output-flood/input-write), never a mocked
    boundary.
  - A candidate that crashes inside the sandbox (uncaught exception) or is
    killed (timeout/OOM/output-flood/denied) is reported with a real
    terminal `status` and sanitized `reason` -- never silently retried into
    a fabricated success, never swallowed. "CANCELLED" is not
    produced by this executor itself (no in-band cancel signal reaches
    `execute()` once it has started); a caller-issued job cancel is handled
    by the generic job-cancel route/worker claim path, unchanged by this
    module, and is out of this file's scope to re-implement.
  - A second real run against the current revision must not reuse a
    cached baseline: this executor is STATELESS across jobs -- it
    reads only `ctx.job["payload"]` and `ctx.job["pinned_*"]` for THIS job
    id, never a prior job's result, and denormalizes its own
    `pinned_source_revision_id`/`pinned_decision_epoch` into the result
    exactly like ImpactAnalysisExecutor, so a second submission against a
    moved revision is provably a fresh run, never a cache hit; no caching
    mechanism exists here to smuggle a shortcut in later.

HD2 reference candidate. No real HD2/emergency-purchase module is
checked into this repo; this executor therefore ships a small, REAL,
EXECUTED-IN-THE-SANDBOX two-handler reference model representing an
"emergency purchase approval threshold" scenario, so the boundary/config-
only/currency/denominator behaviours are proven through actual subprocess output,
not a static description:

  - `standard_handler(request, config)`: approves iff amount <= config
    threshold.
  - `emergency_handler(request, config)`: for channel == "emergency", the
    CURRENT (candidate_kind == "config_only") build has a HARDCODED bypass
    (approves any amount, ignoring config) -- the defect this
    scenario is built around. The CODE_CHANGE candidate fixes
    the emergency handler to also honor `config["threshold"]`. A config-only
    candidate changes only `config["threshold"]`, never the emergency
    handler's hardcoded bypass, so the mismatch structurally survives
    -- proven by actually running both variants, not asserted by
    name (the golden-finding discipline `simulation.py`'s own docstring
    requires: never name the suspect handler in an assertion; compare `CaseResult.changed`/`disagreeing_handlers` structurally).

This is an explicit scope decision: real, executed-in-sandbox business
logic, not a fabricated reference to a module that does not exist in this
repo.
"""

from __future__ import annotations

import json
import os

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.sandbox_config import load_sandbox_config
from infra.worker import sandbox_runner
from core.change_intelligence import simulation as pure_simulation
from core.change_intelligence import simulation_dsl

#: adapters this executor actually runs candidate code for.
_EXECUTABLE_ADAPTERS = ("hd2", "deterministic_dsl", "harbor_cli")

#: candidate_kind -> fixed probe program, run for real inside the sandbox.
#: Each program is a `run_probe()`-shaped module (see sandbox_runner's
#: bootstrap contract) attempting exactly the action its name describes.
_PROBE_SOURCES = {
    "malicious_network_probe": (
        "def run_probe():\n"
        "    import socket\n"
        "    s = socket.create_connection(('example.com', 80), timeout=2)\n"
        "    s.close()\n"
    ),
    "malicious_host_read_probe": (
        "def run_probe():\n"
        "    import os\n"
        "    target = 'C:/Windows/win.ini' if os.name == 'nt' else '/etc/passwd'\n"
        "    with open(target, 'r') as fh:\n"
        "        fh.read()\n"
    ),
    "infinite_loop_probe": (
        "def run_probe():\n"
        "    while True:\n"
        "        pass\n"
    ),
    "oom_probe": (
        "def run_probe():\n"
        "    blocks = []\n"
        "    while True:\n"
        "        blocks.append(bytearray(10 * 1024 * 1024))\n"
    ),
    "output_flood_probe": (
        "def run_probe():\n"
        "    while True:\n"
        "        print('x' * 1024)\n"
    ),
    "malicious_input_write_probe": (
        "def run_probe():\n"
        "    import os\n"
        "    target = os.path.join(INPUTS_DIR, 'corpus.json')\n"
        "    with open(target, 'w') as fh:\n"
        "        fh.write('tampered')\n"
    ),
    "crash_probe": (
        "def run_probe():\n"
        "    raise RuntimeError('simulated unrecoverable candidate failure')\n"
    ),
}

_HD2_HANDLER_SOURCE = """
def standard_handler(request, config):
    return "approved" if request.get("amount", 0) <= config.get("threshold", 500000) else "denied"


def emergency_handler(request, config):
    if request.get("channel") == "emergency":
        return "approved"  # real, reviewed defect: ignores config["threshold"] entirely
    return "approved" if request.get("amount", 0) <= config.get("threshold", 500000) else "denied"
"""

_HD2_HANDLER_SOURCE_FIXED = """
def standard_handler(request, config):
    return "approved" if request.get("amount", 0) <= config.get("threshold", 500000) else "denied"


def emergency_handler(request, config):
    return "approved" if request.get("amount", 0) <= config.get("threshold", 500000) else "denied"
"""

_HD2_HARNESS_TEMPLATE = """
{handlers}

def handle(request, config):
    handler = emergency_handler if request.get("channel") == "emergency" else standard_handler
    return handler(request, config)
"""


def runner_adapters() -> frozenset:
    """Adapters allowed to reach `sandbox_runner` in THIS deployment. The runner is a resource-limited ADVISORY boundary for fixed programs, never isolation for untrusted code, so the Pitch
    deployment runs only the supported pinned Harbor simulation through it (config as validated data, a fixed repo-authored program). Anything else that needs container isolation (the
    generic `hd2` candidate harness, the hostile-behaviour probes) is disabled until a real OS-level sandbox exists. Default: harbor_cli only; the env var can only be set by whoever
    deploys, never by a requester, a payload or an old job."""
    raw = os.environ.get("RIPPLE_SIM_RUNNER_ADAPTERS")
    return frozenset(x.strip() for x in (raw if raw is not None else "harbor_cli").split(",") if x.strip())


def _refuse_if_runner_disabled(payload: dict) -> None:
    """Checked at prepare AND at execute (a job created before this deployment, or a payload that skipped prepare, meets the same refusal before anything runs)."""
    adapter = payload.get("adapter") or "hd2"
    if payload.get("candidate_kind") in _PROBE_SOURCES and os.environ.get("RIPPLE_SANDBOX_PROBES") != "1":
        raise NonRetryableExecutionError("sandbox_probes_disabled", "the sandbox probe programs are disabled in this deployment")
    if adapter == "hd2" and payload.get("corpus") and "hd2" not in runner_adapters():     # an empty corpus never reaches the runner (honest no-op EXECUTED), so the empty `{}` request is unchanged
        raise NonRetryableExecutionError("runner_adapter_disabled", "the generic candidate harness needs container isolation and is disabled in this deployment")


class SimulationExecutor:
    """See module docstring. Zero provider calls: deterministic, caller-
    supplied corpus/candidate, no live model account/budget wiring needed."""

    external = False
    executor_id = "simulation.run.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        corpus = payload.get("corpus")
        if corpus is not None and not isinstance(corpus, list):
            raise NonRetryableExecutionError(
                "corpus_must_be_list", "payload.corpus must be a list when present")
        _refuse_if_runner_disabled(payload)
        if payload.get("adapter") == "harbor_cli":
            from infra.worker import harbor_sim
            err = harbor_sim.validate_payload(payload)
            if err:
                raise NonRetryableExecutionError("harbor_payload_rejected", err)
        if payload.get("adapter") == "deterministic_dsl":
            expr = payload.get("policy_expression")
            try:
                simulation_dsl.parse(expr if isinstance(expr, str) else "")
            except simulation_dsl.DslSyntaxError as exc:
                raise NonRetryableExecutionError("dsl_expression_rejected", str(exc)) from exc
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    # ------------------------------------------------------------------ denominators

    @staticmethod
    def _classify_corpus(corpus: list) -> dict:
        seen_ids: set[str] = set()
        known, excluded, unknown, malformed, duplicate = [], [], [], [], []
        by_currency: dict[str, list] = {}
        for index, record in enumerate(corpus):
            if not isinstance(record, dict):
                malformed.append({"index": index, "reason": "record_not_an_object"})
                continue
            case_id = record.get("historical_case_id")
            amount = record.get("amount")
            if case_id is None or amount is None:
                malformed.append({
                    "index": index, "historical_case_id": case_id,
                    "reason": "missing_required_field (historical_case_id/amount)"})
                continue
            if case_id in seen_ids:
                duplicate.append({
                    "historical_case_id": case_id, "reason": "duplicate_historical_case_id"})
                continue
            seen_ids.add(case_id)
            if record.get("exception"):
                excluded.append({
                    "historical_case_id": case_id,
                    "reason": f"exception_listed:{record['exception']}"})
                continue
            channel = record.get("channel")
            if channel not in (None, "emergency"):
                excluded.append({
                    "historical_case_id": case_id, "reason": f"out_of_scope_channel:{channel}"})
                continue
            currency = record.get("currency") or "VND"
            by_currency.setdefault(currency, []).append(record)
            known.append(record)
        return {
            "submitted": len(corpus),
            "known_count": len(known), "excluded_count": len(excluded),
            "unknown_count": len(unknown), "malformed_count": len(malformed),
            "duplicate_count": len(duplicate),
            "by_currency": {currency: len(records) for currency, records in by_currency.items()},
            "known": known, "excluded": excluded, "unknown": unknown,
            "malformed": malformed, "duplicate": duplicate,
        }

    # ------------------------------------------------------------------ execute

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        payload = job["payload"]
        cfg = load_sandbox_config()
        pinned_input = {
            "pinned_source_revision_id": job.get("pinned_source_revision_id"),
            "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
            "pinned_decision_epoch": job.get("pinned_decision_epoch"),
        }

        corpus = payload.get("corpus") or []
        classification = self._classify_corpus(corpus)
        coverage = {
            "submitted": classification["submitted"],
            "known": classification["known_count"],
            "excluded": classification["excluded_count"],
            "unknown": classification["unknown_count"],
            "malformed": classification["malformed_count"],
            "duplicate": classification["duplicate_count"],
            "by_currency": classification["by_currency"],
        }
        denominator_detail = {
            "excluded": classification["excluded"], "malformed": classification["malformed"],
            "duplicate": classification["duplicate"], "unknown": classification["unknown"],
        }

        _refuse_if_runner_disabled(payload)
        candidate_kind = payload.get("candidate_kind")
        adapter = payload.get("adapter") or "hd2"

        # --- Harbor CLI adapter: pinned public Harbor copy, config as validated DATA (see infra/worker/harbor_sim.py).
        if adapter == "harbor_cli":
            from infra.worker import harbor_sim
            return ExecutionOutcome(result=harbor_sim.execute(ctx, payload, pinned_input), model_id=None)

        # --- malicious probes: real sandboxed execution, no diff needed.
        if candidate_kind in _PROBE_SOURCES:
            sandbox_result = sandbox_runner.run_isolated(
                candidate_source=_PROBE_SOURCES[candidate_kind],
                input_files={"corpus.json": json.dumps(corpus).encode("utf-8")},
                config=cfg)
            status = {
                sandbox_runner.STATUS_DENIED: "DENIED",
                sandbox_runner.STATUS_TIMEOUT: "TIMEOUT",
                sandbox_runner.STATUS_OOM_KILLED: "OOM_KILLED",
                sandbox_runner.STATUS_OUTPUT_LIMIT_EXCEEDED: "OUTPUT_LIMIT_EXCEEDED",
                sandbox_runner.STATUS_CRASHED: "FAILED",
                sandbox_runner.STATUS_EXECUTED: "EXECUTED",
            }[sandbox_result.status]
            result = {
                "executor_id": self.executor_id, "status": status,
                "reason": sandbox_result.reason, "coverage": coverage,
                "denominator_detail": denominator_detail, "outcomes": [],
                "sandbox": {
                    "input_hash_before": sandbox_result.input_hashes_before,
                    "input_hash_after": sandbox_result.input_hashes_after,
                    "input_hash_unchanged": (sandbox_result.input_hashes_before
                                             == sandbox_result.input_hashes_after),
                    "memory_limit_enforced": sandbox_result.memory_limit_enforced,
                    "duration_seconds": sandbox_result.duration_seconds,
                },
                "pinned_input": pinned_input,
            }
            return ExecutionOutcome(result=result, model_id=None)

        # --- unsupported adapter: honest NOT_EXECUTABLE, never a fake zero-impact EXECUTED.
        if adapter not in _EXECUTABLE_ADAPTERS:
            result = {
                "executor_id": self.executor_id, "status": "NOT_EXECUTABLE",
                "reason": f"unsupported_adapter:{adapter}", "coverage": coverage,
                "denominator_detail": denominator_detail, "outcomes": [],
                "pinned_input": pinned_input,
            }
            return ExecutionOutcome(result=result, model_id=None)

        # --- empty corpus: honest, explicit zero-sized EXECUTED, never a fabricated "clean" claim.
        if not corpus:
            result = {
                "executor_id": self.executor_id, "status": "EXECUTED",
                "reason": "empty_corpus", "coverage": coverage,
                "denominator_detail": denominator_detail, "outcomes": [],
                "pinned_input": pinned_input,
            }
            return ExecutionOutcome(result=result, model_id=None)

        if adapter == "deterministic_dsl":
            return self._execute_dsl(ctx, payload, classification, coverage,
                                     denominator_detail, pinned_input)

        return self._execute_hd2(ctx, payload, candidate_kind, classification, coverage,
                                 denominator_detail, pinned_input, cfg)

    # ------------------------------------------------------------------ deterministic_dsl adapter

    def _execute_dsl(self, ctx, payload, classification, coverage, denominator_detail,
                     pinned_input) -> ExecutionOutcome:
        tree = simulation_dsl.parse(payload.get("policy_expression"))
        outcomes = []
        for record in classification["known"]:
            matched = simulation_dsl.evaluate(tree, record)
            outcomes.append({
                "historical_case_id": record.get("historical_case_id"),
                "matched": matched})
        result = {
            "executor_id": self.executor_id, "status": "EXECUTED", "reason": None,
            "coverage": coverage, "denominator_detail": denominator_detail,
            "outcomes": outcomes, "pinned_input": pinned_input,
        }
        return ExecutionOutcome(result=result, model_id=None)

    # ------------------------------------------------------------------ hd2 adapter

    def _execute_hd2(self, ctx, payload, candidate_kind, classification, coverage,
                     denominator_detail, pinned_input, cfg) -> ExecutionOutcome:
        by_currency = {}
        for record in classification["known"]:
            by_currency.setdefault(record.get("currency") or "VND", []).append(record)

        config_only = candidate_kind == "config_only"
        current_source = _HD2_HARNESS_TEMPLATE.format(handlers=_HD2_HANDLER_SOURCE)
        # code_change candidate fixes the handler; config_only never touches
        # the handler source, only the shadow config value below.
        shadow_source = (current_source if config_only
                         else _HD2_HARNESS_TEMPLATE.format(handlers=_HD2_HANDLER_SOURCE_FIXED))
        current_config = {"threshold": 500000}
        shadow_config = {"threshold": 600000} if config_only else {"threshold": 500000}

        cases_by_currency: dict[str, list] = {}
        any_denied = None
        for currency, records in by_currency.items():
            current_result = sandbox_runner.run_isolated(
                candidate_source=current_source,
                input_files={
                    "corpus.json": json.dumps(records).encode("utf-8"),
                    "config.json": json.dumps(current_config).encode("utf-8")},
                config=cfg)
            shadow_result = sandbox_runner.run_isolated(
                candidate_source=shadow_source,
                input_files={
                    "corpus.json": json.dumps(records).encode("utf-8"),
                    "config.json": json.dumps(shadow_config).encode("utf-8")},
                config=cfg)
            if current_result.status != sandbox_runner.STATUS_EXECUTED:
                any_denied = any_denied or (currency, current_result)
                continue
            if shadow_result.status != sandbox_runner.STATUS_EXECUTED:
                any_denied = any_denied or (currency, shadow_result)
                continue
            current_outcomes = tuple(
                pure_simulation.Outcome(case_id=str(o["historical_case_id"]),
                                        handler="candidate.handle", outcome=o["outcome"])
                for o in current_result.stdout_result.get("outcomes", []))
            shadow_outcomes = tuple(
                pure_simulation.Outcome(case_id=str(o["historical_case_id"]),
                                        handler="candidate.handle", outcome=o["outcome"])
                for o in shadow_result.stdout_result.get("outcomes", []))
            cases = []
            by_case_id = {o.case_id: o for o in current_outcomes}
            for shadow_outcome in shadow_outcomes:
                current_o = by_case_id.get(shadow_outcome.case_id)
                if current_o is None:
                    continue
                cases.append(pure_simulation.CaseResult(
                    case_id=shadow_outcome.case_id,
                    current=(current_o,), shadow=(shadow_outcome,),
                    effective_handler="handle"))
            cases_by_currency[currency] = cases

        if any_denied is not None:
            currency, bad = any_denied
            result = {
                "executor_id": self.executor_id, "status": "FAILED",
                "reason": f"sandbox_run_not_executed:{currency}:{bad.status}:{bad.reason}",
                "coverage": coverage, "denominator_detail": denominator_detail,
                "outcomes": [], "pinned_input": pinned_input,
            }
            return ExecutionOutcome(result=result, model_id=None)

        outcomes = []
        for currency, cases in cases_by_currency.items():
            for case in cases:
                outcomes.append({
                    "historical_case_id": case.case_id,
                    "currency": currency,
                    "changed": case.changed,
                    "current_outcome": case.current[0].outcome if case.current else None,
                    "shadow_outcome": case.shadow[0].outcome if case.shadow else None,
                })

        result = {
            "executor_id": self.executor_id, "status": "EXECUTED", "reason": None,
            "candidate_kind": candidate_kind, "coverage": coverage,
            "denominator_detail": denominator_detail, "outcomes": outcomes,
            "changed_count": sum(1 for o in outcomes if o["changed"]),
            "pinned_input": pinned_input,
        }
        return ExecutionOutcome(result=result, model_id=None)


__all__ = ["SimulationExecutor"]
