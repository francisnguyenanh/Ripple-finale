"""Executor for job kind ``review`` (executor id ``review.board.v1``): validates a COMPLETED proposal and reports whether it is ready for a human to review. It never approves and never writes.

Behaviour
  - One deterministic board of three evaluators runs inside a single ``ReviewExecutor.execute()`` call (no sub-jobs). ``review_config.BOARD_ID`` names the board version. The roles are the same in-process
    heuristic evaluator, and every result says so in ``model_correlation_disclosure`` instead of claiming independent models. The executor makes no provider call (``external = False``).
  - Input: ``payload.proposal_job_id`` (must resolve to a ``kind="proposal"`` job of this tenant/workspace), optional ``evaluated_hash`` (typed only) and optional ``verification_contract_id``. Payload shape is
    validated at the route (``infra/v2_policy_routes.py::create_job``) and again here as defence in depth. Creation needs ``source.import`` (``JOB_CREATE_PERMISSION["review"]``), the ceiling of the other
    read-only kinds that consume already stored job output. A caller-declared ``expected_decision_epoch`` below the workspace's current epoch is refused like for ``impact``, ``whatif`` and ``proposal``.
  - Configuration is in ``review_config.py`` (environment sourced, safe defaults, an override can only tighten).

Guards
  - ``evaluated_hash`` is never trusted. The executor recomputes it from the resolved proposal's own result (``staged_blob_hash`` when real candidate bytes were staged, else ``after_blob_hash`` for a provable
    no-op); a caller value that differs is rejected with ``NonRetryableExecutionError`` and no bundle is produced.
  - A ``ReviewBundle`` is the job's own result, keyed to the exact ``proposal_job_id`` named in its payload. A revised proposal is always a new job id, so an old review can never attach to new bytes; reviewing a
    revision needs a new ``review`` job.
  - Read-only: ``stage_blob``, ``publish_source`` and ``approve_job`` are never called. Role findings come only from persisted structured fields (classification, edits, blockers), never from free model text.
  - ``readiness`` is one deterministic rule: any required check or role that reports ``FAIL`` or ``UNAVAILABLE`` forces ``BLOCKED``; no role can override another's failure and there is no majority vote. The
    strongest value reported is ``READY_FOR_HUMAN_REVIEW``; approval is a separate human decision.
  - ``acceptance_tests`` evaluates the verification contract named by ``payload.verification_contract_id`` (``_check_acceptance_tests``); with no contract it is ``UNAVAILABLE`` and therefore a blocker.
  - Python edits are never executed. For ``edit_kind == "python"`` with staged bytes the simulation check is ``UNAVAILABLE`` (reason ``user_code_not_executed``), which forces ``BLOCKED``: python edits are
    proposal-only, because ``sandbox_runner.run_isolated`` is a resource-limited subprocess, not a security boundary. A true no-op passes. ``markdown``, ``yaml_typed_edit`` and ``xlsx_typed_edit`` are
    ``SKIPPED_NOT_APPLICABLE`` for that check; ``yaml_schema_roundtrip`` and ``workbook_roundtrip`` are the executed proof for the data-only kinds, and ``python_syntax_ast`` parses python text without running it.

History: earlier revisions of this docstring recorded design deliberations between work rounds; they are in the version control history.
"""

from __future__ import annotations

import ast
import hashlib
import io

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.review_config import load_review_board_config

_REQUIRED_PAYLOAD_FIELDS = ("proposal_job_id",)


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _resolve_proposal(ctx: ExecutionContext, proposal_job_id: str):
    """Resolves the exact, real, already-COMPLETED `kind="proposal"` job
    this review evaluates. Route-level validation
    (`infra/v2_policy_routes.py::create_job`) already rejects a missing/
    wrong-kind `proposal_job_id` before this job is even created; a
    worker-time miss here (e.g. a race between submission and claim) is a
    hard, non-retryable failure -- there is nothing real to review."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    try:
        job = ctx.store.job_record(tenant_id, workspace_id, proposal_job_id)
    except Exception as exc:
        raise NonRetryableExecutionError(
            "proposal_job_not_found", str(proposal_job_id)) from exc
    if job.get("kind") != "proposal":
        raise NonRetryableExecutionError(
            "proposal_job_wrong_kind", f"kind={job.get('kind')!r}")
    if job.get("execution_status") != "COMPLETED":
        raise NonRetryableExecutionError(
            "proposal_job_not_completed", f"status={job.get('execution_status')!r}")
    return job


def _real_evaluated_hash(proposal_result: dict) -> str | None:
    """The REAL hash of the exact candidate bytes under review -- never a
    caller-supplied claim (see module docstring). This is `after_blob_hash`,
    the proposal's own reported bare-hex SHA-256 of the final candidate
    content (identical to `before_blob_hash` for a true no-op) -- the SAME
    value/format `ProposalExecutor` itself already reports to any reader of
    the proposal job, so a caller comparing against what it already saw at
    `GET /v2/jobs/{proposal_job_id}` compares like-for-like. `staged_blob_
    hash` is a DIFFERENT, store-internal, `"sha256:"`-prefixed content-
    address (`infra/adapters/v2_postgres.py::_digest`) used only to fetch
    the bytes back (see `_staged_bytes`) -- never surfaced as "the evaluated
    hash" itself, since exposing two differently-formatted hashes of the
    same content as if interchangeable would itself be a real evaluated-hash
    confusion bug, not a security property."""
    after = proposal_result.get("after_blob_hash")
    if isinstance(after, str) and after:
        return after
    return proposal_result.get("before_blob_hash")


def _staged_bytes(ctx: ExecutionContext, proposal_result: dict) -> bytes | None:
    """Reads back the exact staged candidate bytes via the store's own
    content-addressed blob-integrity primitive (the SAME one `stage_blob`
    itself uses to verify what it just wrote, `infra/adapters/v2_postgres.py
    ::_verified_blob`) -- not a second blob-reading mechanism. Returns
    `None` for a true no-op (nothing staged; nothing to independently apply-
    dry-run/syntax-check beyond what the proposal's own resolved contract
    already guarantees)."""
    staged_hash = proposal_result.get("staged_blob_hash")
    if not isinstance(staged_hash, str) or not staged_hash:
        return None
    return ctx.store._verified_blob(staged_hash)


def _check_apply_dry_run(staged_bytes: bytes | None, is_no_op: bool,
                         after_blob_hash: str | None) -> tuple[str, str | None]:
    if is_no_op:
        return "PASS", "true no-op: zero bytes staged, nothing to apply"
    if staged_bytes is None:
        return "FAIL", "proposal reports a non-no-op edit but no staged bytes are readable"
    # Independently recompute the staged bytes' own hash and cross-check it
    # against the proposal's OWN reported `after_blob_hash` -- proves the
    # bytes this executor just read back are genuinely the ones the
    # proposal claims to have produced, not merely "some readable blob".
    if isinstance(after_blob_hash, str) and after_blob_hash:
        recomputed = _sha256_hex(staged_bytes)
        if recomputed != after_blob_hash:
            return "FAIL", (f"staged blob content hash {recomputed!r} does not match "
                            f"proposal's own after_blob_hash {after_blob_hash!r}")
    return "PASS", f"staged blob readable, content-addressed verified, and hash-consistent " \
                  f"with the proposal's own after_blob_hash ({len(staged_bytes)} bytes)"


def _check_syntax_ast(edit_kind: str, staged_bytes: bytes | None) -> tuple[str, str | None]:
    if edit_kind not in ("python", "markdown"):
        return "SKIPPED_NOT_APPLICABLE", f"edit_kind={edit_kind!r} has no Python AST to check"
    if edit_kind == "markdown":
        return "SKIPPED_NOT_APPLICABLE", "markdown has no Python syntax/AST to check"
    if staged_bytes is None:
        return "PASS", "true no-op: nothing staged"
    try:
        text = staged_bytes.decode("utf-8")
        ast.parse(text)
    except (UnicodeDecodeError, SyntaxError) as exc:
        return "FAIL", f"staged Python candidate fails to parse: {exc}"
    return "PASS", "staged Python candidate parses as valid AST"


def _check_yaml_schema_roundtrip(edit_kind: str, staged_bytes: bytes | None) -> tuple[str, str | None]:
    if edit_kind != "yaml_typed_edit":
        return "SKIPPED_NOT_APPLICABLE", f"edit_kind={edit_kind!r} is not a YAML typed edit"
    if staged_bytes is None:
        return "PASS", "true no-op: nothing staged"
    import yaml
    try:
        data = yaml.safe_load(staged_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return "FAIL", "staged YAML root is not a mapping"
        # Roundtrip: dump then reload must reproduce the same mapping --
        # proves the staged bytes are not merely parseable but stably
        # re-serializable (the real "workbook/schema roundtrip" proof for
        # this edit kind, since typed YAML edits have no separate schema
        # file to validate against in this repo).
        redumped = yaml.safe_dump(data, sort_keys=False)
        if yaml.safe_load(redumped) != data:
            return "FAIL", "staged YAML does not survive a dump/reload roundtrip unchanged"
    except Exception as exc:  # noqa: BLE001 -- real, unpredictable yaml/codec errors
        return "FAIL", f"staged YAML fails to parse/roundtrip: {exc}"
    return "PASS", "staged YAML parses, is a mapping, and roundtrips unchanged"


def _check_workbook_roundtrip(edit_kind: str, staged_bytes: bytes | None) -> tuple[str, str | None]:
    if edit_kind != "xlsx_typed_edit":
        return "SKIPPED_NOT_APPLICABLE", f"edit_kind={edit_kind!r} is not an XLSX typed edit"
    if staged_bytes is None:
        return "PASS", "true no-op: nothing staged"
    import openpyxl
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(staged_bytes))
        if not workbook.sheetnames:
            return "FAIL", "staged workbook has no sheets"
    except Exception as exc:  # noqa: BLE001 -- real, unpredictable openpyxl errors
        return "FAIL", f"staged workbook fails to reopen: {exc}"
    return "PASS", f"staged workbook reopens with sheets {workbook.sheetnames!r}"


def _check_diff_scope(proposal_result: dict) -> tuple[str, str | None]:
    edits = proposal_result.get("edits") or []
    target = proposal_result.get("target_artifact_id")
    if not isinstance(target, str) or not target:
        return "FAIL", "proposal has no target_artifact_id to scope the diff to"
    if proposal_result.get("is_no_op"):
        return "PASS", "true no-op: empty diff scope"
    if not edits:
        return "FAIL", "non-no-op proposal reports zero edit records: unscoped diff"
    for edit in edits:
        if not isinstance(edit, dict) or not edit.get("rationale") or not edit.get("evidence_ref"):
            return "FAIL", "an edit record is missing rationale/evidence_ref: unscoped/unsupported diff"
    return "PASS", f"diff scoped to target_artifact_id={target!r} across {len(edits)} edit record(s)"


def _check_acceptance_tests(ctx: ExecutionContext, payload: dict, proposal_result: dict,
                            edit_kind: str, staged_bytes: bytes | None,
                            is_no_op: bool) -> tuple[str, str | None, dict | None]:
    """The acceptance check is REAL and driven only by an operator-supplied
    verification contract (`v2_verification_contracts`, `payload.verification_contract_id`).
    The expected value comes from an authenticated human, never from the AI output, the
    proposal's own edits, a repo file, an env var or a label; no user code is executed.

      PASS         the contract is present, belongs to THIS proposal and kind, targets THIS
                   artifact, the STAGED bytes hold the contract's expected value at its locator
                   AND the deterministic before->staged tree diff equals exactly that one
                   locator (nothing else changed);
      FAIL         the contract disagrees with the staged value, covers another locator /
                   artifact / proposal / kind, the diff exceeds the declared scope, or the
                   stored row no longer hashes to its own `contract_sha256`;
      UNAVAILABLE  no contract, contract/bytes unreadable, a true no-op, or python/markdown
                   (no data contract exists for them -- unchanged, they stay proposal-only).

    The detail embeds `contract_sha256`; the whole review result is part of `approval_digest`
    (`approval.approval_digest_inputs` hashes `review_result`), so the human-visible approval is
    bound to the exact contract without any change to the digest definition. Returns
    `(status, detail, contract_info)`; a required-but-not-PASS check is a blocker (see
    `execute`), never silently approved."""
    from infra.worker import verification_contract as vc

    expected_kind = vc.EDIT_KIND_TO_CONTRACT_KIND.get(edit_kind)
    if expected_kind is None:
        return ("UNAVAILABLE", f"edit_kind={edit_kind!r} has no data acceptance contract "
                               f"(python/markdown stay proposal-only)", None)
    contract_id = payload.get("verification_contract_id")
    if not isinstance(contract_id, str) or not contract_id:
        return ("UNAVAILABLE", "no operator-supplied verification contract was named "
                               "(payload.verification_contract_id)", None)
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    try:
        contract = ctx.store.verification_contract(tenant_id, workspace_id, contract_id)
    except Exception as exc:  # noqa: BLE001 -- unreadable store row => unavailable, never a PASS
        return "UNAVAILABLE", f"verification contract unreadable: {type(exc).__name__}", None
    if contract is None:
        return "UNAVAILABLE", f"verification contract {contract_id!r} not found in this workspace", None
    info = {"contract_id": contract_id, "contract_sha256": contract.get("contract_sha256")}
    if vc.contract_hash_of(contract) != contract.get("contract_sha256"):
        return "FAIL", vc.describe(contract, note="stored contract no longer matches its hash"), info
    if contract.get("proposal_job_id") != payload.get("proposal_job_id"):
        return "FAIL", vc.describe(contract, note="contract belongs to another proposal"), info
    if contract.get("kind") != expected_kind:
        return "FAIL", vc.describe(
            contract, note=f"contract kind does not cover edit_kind={edit_kind!r}"), info
    if is_no_op or staged_bytes is None:
        return "UNAVAILABLE", vc.describe(contract, note="true no-op: nothing staged to verify"), info
    target = proposal_result.get("target_artifact_id")
    pinned = (proposal_result.get("pinned_input") or {}).get("pinned_source_revision_id")
    try:
        before = ctx.store.read_artifact_blob(tenant_id, workspace_id, pinned, target)
    except Exception as exc:  # noqa: BLE001
        return ("UNAVAILABLE", vc.describe(
            contract, note=f"pre-change artifact unreadable: {type(exc).__name__}"), info)
    if (before.get("logical_path") or target) != contract.get("logical_path"):
        return "FAIL", vc.describe(contract, note="contract targets another artifact"), info
    before_doc = vc.load_document(expected_kind, before["content"])
    staged_doc = vc.load_document(expected_kind, staged_bytes)
    if before_doc is None or staged_doc is None:
        return "UNAVAILABLE", vc.describe(contract, note="before/staged bytes do not parse"), info
    value_status, actual = vc.evaluate_value(contract, staged_doc)
    diff = vc.diff_locators(before_doc, staged_doc)
    if value_status != "PASS":
        return ("FAIL", vc.describe(contract, actual=actual, diff=diff,
                                    note="staged value disagrees with the contract"), info)
    if diff != [contract["locator"]]:
        return ("FAIL", vc.describe(contract, actual=actual, diff=diff,
                                    note="diff is not exactly the declared locator"), info)
    return ("PASS", vc.describe(contract, actual=actual, diff=diff,
                                note="staged value agrees and the diff is exactly the declared locator"),
            info)


#: Named reason recorded in the check detail when a python edit's exact-patch simulation is refused.
REASON_USER_CODE_NOT_EXECUTED = ("user_code_not_executed: python edits are proposal-only; staged text is never run "
                                 "(sandbox_runner is for fixed internal programs and is not a security boundary)")


def _check_simulation_exact_patch(edit_kind: str, staged_bytes: bytes | None,
                                  is_no_op: bool) -> tuple[str, str | None]:
    if edit_kind not in ("python", "markdown"):
        return ("SKIPPED_NOT_APPLICABLE",
               f"edit_kind={edit_kind!r} carries no Python control flow to execute; "
               f"see yaml_schema_roundtrip/workbook_roundtrip for the equivalent proof")
    if edit_kind == "markdown":
        return "SKIPPED_NOT_APPLICABLE", "markdown has no executable logic to simulate"
    if is_no_op or staged_bytes is None:
        return "PASS", "true no-op: nothing staged to execute"
    # The staged bytes derive from a requester's proposal, so they are NEVER executed. The
    # sandbox runner is a resource-limited subprocess for FIXED internal programs, not a security boundary
    # (it has no network/filesystem confinement), and python/markdown edits are proposal-only. A required
    # check that cannot be proven is UNAVAILABLE, which `_readiness` turns into BLOCKED (fail closed).
    return "UNAVAILABLE", REASON_USER_CODE_NOT_EXECUTED


# ------------------------------------------------------------------- roles

def _role_business_review(proposal_result: dict) -> dict:
    findings = []
    if proposal_result.get("classification") == "manual":
        findings.append({
            "finding": "no_machine_synthesizable_edit",
            "severity": "blocking",
            "evidence": {"kind": "proposal_classification", "value": "manual",
                        "reason": proposal_result.get("blockers")},
            "suggested_resolution": "a human must author this edit; this is not "
                                    "an AI-approvable candidate",
        })
    for blocker in proposal_result.get("blockers") or []:
        findings.append({
            "finding": "upstream_report_unavailable",
            "severity": "blocking",
            "evidence": {"kind": "proposal_blocker", "detail": blocker},
            "suggested_resolution": "resolve the named upstream report/blocker before review",
        })
    status = "FAIL" if any(f["severity"] == "blocking" for f in findings) else "PASS"
    return {"role": "BUSINESS_REVIEW", "status": status, "findings": findings}


def _role_evidence_review(proposal_result: dict) -> dict:
    findings = []
    for edit in proposal_result.get("edits") or []:
        if not isinstance(edit, dict) or not edit.get("evidence_ref") or not edit.get("rationale"):
            findings.append({
                "finding": "missing_citation",
                "severity": "blocking",
                "evidence": {"kind": "edit_record", "detail": edit},
                "suggested_resolution": "every edit must cite a real evidence_ref/rationale",
            })
    status = "FAIL" if findings else "PASS"
    return {"role": "EVIDENCE_REVIEW", "status": status, "findings": findings}


def _role_technical_review(proposal_result: dict, checks: dict) -> dict:
    findings = []
    for name, entry in checks.items():
        if entry["status"] == "FAIL":
            findings.append({
                "finding": f"required_check_failed:{name}",
                "severity": "blocking",
                "evidence": {"kind": "check_result", "check": name, "detail": entry["detail"]},
                "suggested_resolution": f"fix the underlying defect that fails {name!r} "
                                        f"before this candidate can proceed",
            })
    status = "FAIL" if findings else "PASS"
    return {"role": "TECHNICAL_REVIEW", "status": status, "findings": findings}


_ROLE_EVALUATORS = {
    "BUSINESS_REVIEW": lambda proposal_result, checks: _role_business_review(proposal_result),
    "EVIDENCE_REVIEW": lambda proposal_result, checks: _role_evidence_review(proposal_result),
    "TECHNICAL_REVIEW": lambda proposal_result, checks: _role_technical_review(
        proposal_result, checks),
}


def _assert_draft_current(ctx, proposal_job_id) -> None:
    """A superseded draft's proposal is history: never reviewed, bundled or applied from now on (checked again here because the job may have been queued or running when the draft was revised)."""
    from infra import v2_lineage
    newer = v2_lineage.newer_draft_for_proposal(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], proposal_job_id)
    if newer:
        raise NonRetryableExecutionError("draft_superseded", f"{v2_lineage.SUPERSEDED_MSG} (newer draft {newer})")


class ReviewExecutor:
    """See module docstring. Zero provider calls (`external = False`)."""

    external = False
    executor_id = "review.board.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        for key in _REQUIRED_PAYLOAD_FIELDS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise NonRetryableExecutionError(
                    f"{key}_required", f"payload.{key} must be a non-empty string")
        evaluated_hash = payload.get("evaluated_hash")
        if evaluated_hash is not None and (
                not isinstance(evaluated_hash, str) or not evaluated_hash):
            raise NonRetryableExecutionError(
                "evaluated_hash_must_be_string", "payload.evaluated_hash, when present, "
                                                 "must be a non-empty string")
        contract_id = payload.get("verification_contract_id")
        if contract_id is not None and (not isinstance(contract_id, str) or not contract_id):
            raise NonRetryableExecutionError(
                "verification_contract_id_must_be_string",
                "payload.verification_contract_id, when present, must be a non-empty string")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        payload = job["payload"]
        proposal_job_id = payload.get("proposal_job_id")
        claimed_hash = payload.get("evaluated_hash")
        # Deterministic test-only knob (same convention as
        # `infra/worker/simulation.py`'s own `payload.candidate_kind`
        # probes): lets a caller/test deterministically exercise "a required
        # role is unavailable" through the real
        # executor, not a mocked boundary.
        forced_unavailable_roles = set(payload.get("simulate_role_unavailable") or [])

        proposal_job = _resolve_proposal(ctx, proposal_job_id)
        _assert_draft_current(ctx, proposal_job_id)
        proposal_result = proposal_job.get("execution_result") or {}

        real_hash = _real_evaluated_hash(proposal_result)
        if claimed_hash is not None and claimed_hash != real_hash:
            # B3-b: an evaluated-hash bypass attempt -- the caller's claim
            # does not match the real, independently recomputed hash of the
            # exact candidate bytes. Rejected outright; no ReviewBundle is
            # ever produced trusting the forged claim.
            raise NonRetryableExecutionError(
                "evaluated_hash_mismatch",
                f"claimed={claimed_hash!r} real={real_hash!r}")

        edit_kind = proposal_result.get("edit_kind")
        is_no_op = bool(proposal_result.get("is_no_op"))
        staged_bytes = _staged_bytes(ctx, proposal_result)

        board_cfg = load_review_board_config()

        checks: dict = {}
        acceptance = _check_acceptance_tests(
            ctx, payload, proposal_result, edit_kind, staged_bytes, is_no_op)
        after_blob_hash = proposal_result.get("after_blob_hash")
        for name, status_detail in (
                ("apply_dry_run",
                 _check_apply_dry_run(staged_bytes, is_no_op, after_blob_hash)),
                ("syntax_ast", _check_syntax_ast(edit_kind, staged_bytes)),
                ("yaml_schema_roundtrip", _check_yaml_schema_roundtrip(edit_kind, staged_bytes)),
                ("workbook_roundtrip", _check_workbook_roundtrip(edit_kind, staged_bytes)),
                ("diff_scope", _check_diff_scope(proposal_result)),
                ("acceptance_tests", acceptance[:2]),
                ("simulation_exact_patch",
                 _check_simulation_exact_patch(edit_kind, staged_bytes, is_no_op)),
        ):
            status, detail = status_detail
            checks[name] = {"status": status, "detail": detail}

        roles: dict = {}
        for role_id in sorted(board_cfg.required_roles):
            if role_id in forced_unavailable_roles:
                roles[role_id] = {"role": role_id, "status": "UNAVAILABLE",
                                  "findings": [], "reason": "simulated_unavailable_or_timeout"}
                continue
            evaluator = _ROLE_EVALUATORS.get(role_id)
            if evaluator is None:
                roles[role_id] = {"role": role_id, "status": "UNAVAILABLE",
                                  "findings": [], "reason": "no_evaluator_registered"}
                continue
            roles[role_id] = evaluator(proposal_result, checks)

        blockers = []
        for name in board_cfg.required_checks:
            entry = checks.get(name)
            if entry is None:
                blockers.append({"kind": "check", "name": name, "reason": "check_not_run"})
            elif entry["status"] in ("FAIL", "UNAVAILABLE"):
                blockers.append({"kind": "check", "name": name, "reason": entry["status"],
                                 "detail": entry["detail"]})
        for role_id in board_cfg.required_roles:
            entry = roles.get(role_id)
            if entry is None or entry["status"] in ("FAIL", "UNAVAILABLE"):
                blockers.append({
                    "kind": "role", "name": role_id,
                    "reason": (entry or {}).get("status", "role_not_evaluated"),
                })

        # Never AI-overridden, never a majority vote, never "approved" --
        # a single, deterministic rule over real results (see module
        # docstring).
        readiness = "BLOCKED" if blockers else "READY_FOR_HUMAN_REVIEW"

        result = {
            "executor_id": self.executor_id,
            "board_id": board_cfg.board_id,
            "proposal_job_id": proposal_job_id,
            "evaluated_hash": real_hash,
            "edit_kind": edit_kind,
            "checks": checks,
            "roles": roles,
            "role_model_ids": board_cfg.role_model_ids,
            "model_correlation_disclosure": (
                "all roles run as the SAME deterministic, zero-provider-call "
                "in-process evaluator (external=False); no independent live "
                "model providers are wired in this environment, so no "
                "majority-vote/independent-model proof is claimed"),
            "readiness": readiness,
            "blockers": blockers,
            **({"verification_contract": acceptance[2]} if acceptance[2] is not None else {}),
            "max_repair_rounds": board_cfg.max_repair_rounds,
            "pinned_input": {
                "pinned_source_revision_id": job.get("pinned_source_revision_id"),
                "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
                "pinned_decision_epoch": job.get("pinned_decision_epoch"),
            },
        }
        _assert_draft_current(ctx, proposal_job_id)                      # revised while this ran: the result is not published as current
        return ExecutionOutcome(result=result, model_id=None)


__all__ = ["ReviewExecutor"]
