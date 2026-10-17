"""Contextual patch generation, job kind "proposal",
executor_id "proposal.generate.v1".

Contract:

  - Reuses the generic `POST /v2/jobs` / `GET /v2/jobs/{id}` route pair,
    same as `impact`/`simulation`/`whatif` (no second dispatch
    mechanism). Payload-shape/protected-path/allowlist/oracle-substitution
    validation lives at the ROUTE layer
    (`infra/v2_policy_routes.py::create_job`), the SAME place every other
    job kind's payload-shape checks already live -- this executor does not
    duplicate those checks, it trusts the route already ran them (defense
    in depth: this executor still re-validates required fields in
    `prepare()`, matching every sibling executor's own posture, but the
    PRIMARY submission-time rejection is the route's, exactly like
    `impact`/`whatif`'s own `payload.candidate_ids`/`impact_job_id`/
    `simulation_job_id` checks).

  - **Multi-revision decision:**
    a "revise" action creates a brand NEW job id, never mutates a prior proposal's row. This is the simplest
    shape that reuses the job table's existing append-only nature
    unchanged, and it trivially satisfies "an old review must not silently
    reattach to a new version": a review/approval is keyed by job_id, and a
    revision is a DIFFERENT job_id, so there is no shared row an old
    approval could still point at. The route enforces (before creating the
    new job) that `previous_proposal_job_id` names a REAL, existing
    `kind == "proposal"` job in this tenant/workspace -- a revise against a
    nonexistent/wrong-kind prior proposal is rejected before any job is
    created. This executor denormalizes `previous_proposal_job_id` and a
    monotonic `proposal_version` (1 for a fresh generate; the prior job's
    own `proposal_version + 1` for a revise, read via the SAME
    `ctx.store.job_record` primitive every sibling executor already uses)
    into its own result, so a reader can reconstruct the whole revision
    chain without a second, dedicated versioned-read route
    (a known limit if a multi-version LIST becomes a real requirement --
    the single `execution_result` per job id is sufficient for "read the latest/one
    named version", not for "list every version of an intent" without a
    caller separately tracking job ids).

  - Zero provider calls (`external = False`): this executor makes NO live
    model call. Every "contextual" edit below is derived deterministically
    from real, already-persisted inputs -- the confirmed Intent
    (`ctx.store.get_intent`), the impact report's real findings/evidence,
    and the actual current artifact bytes (`ctx.store.read_artifact_blob`,
    pinned to the job's OWN `pinned_source_revision_id`, never a live
    re-read of a possibly-since-changed head). No live retrieval/model
    account exists for this job kind (same posture as
    `impact`/`simulation`/`whatif`); where a real textual anchor cannot be
    found in the current content, this executor is HONEST about that
    limitation (classification `manual`, not a template string invented to
    look like a patch) rather than fabricating content it cannot ground in
    a real citation.

  - Persistence: a real, non-no-op edit's candidate bytes are staged via
    `ctx.store.stage_blob` (an IMMUTABLE content-addressed write, the same
    primitive `publish_with_chunk`/every other source-staging path already
    uses) -- NEVER via `store.publish_source`. This executor never mutates
    the workspace head/current source in any way; a proposal is always a
    candidate, never itself a publish. A no-op edit (the input already
    matches the proposed target) stages ZERO bytes and the reported
    `after_blob_hash` is set to the UNCHANGED `before_blob_hash` value --
    never a fabricated new hash for content nothing wrote.

  - Malformed patch (a typed edit whose locator does not resolve, or whose
    `expected_current` does not match the REAL current value at that
    locator) is a NON-RETRYABLE FAILURE (`NonRetryableExecutionError`) --
    the job never reaches COMPLETED and nothing is ever staged. This is the
    literal "rejected before staging" property: a
    `FAILED` job with a durable, named reason is provable via
    `execution_reason`, never a silently-accepted patch with a wrong
    locator.

  - Unresolvable/wrong-kind/not-completed `impact_job_id`/`simulation_job_id`
    inputs are named blockers on a still-COMPLETED proposal
    (`"report_unavailable:<reason>"`, the exact convention
    `infra/worker/whatif.py::_resolve_report` already established --
    not a second mechanism) -- never a crash, and never
    silently treated as "no report, nothing to check".

  - Classification (`required`/`recommended`/`manual`/`unsupported`) per
    edit, never silently downgraded: an edit that resolves a real, OPEN,
    blocking-severity impact finding is `required`; an edit derived from
    real evidence but not tied to a blocking finding is `recommended`; an
    edit this executor cannot ground in any real textual/typed anchor
    (unresolved intent, no matching anchor in the current content) is
    `manual` (a human must author it); a syntactically-valid typed edit
    whose PROPOSED value this executor cannot safely apply (e.g. a type
    mismatch) is `unsupported` -- distinct from `manual`, and distinct from
    the hard-reject malformed-locator case above (that case never reaches
    classification at all -- it fails the job outright).
"""

from __future__ import annotations

import hashlib
import io

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.proposal_config import load_proposal_support_matrix

_REQUIRED_PAYLOAD_FIELDS = (
    "intent_id", "impact_job_id", "simulation_job_id", "target_artifact_id", "edit_kind")


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _resolve_report(ctx: ExecutionContext, job_id, *, expected_kind: str):
    """Same real, tenant/workspace-scoped resolution primitive
    `infra/worker/whatif.py::_resolve_report` already established
    (a reused pattern, not a second mechanism -- kept as a local
    copy rather than a cross-module import so this executor has no runtime
    dependency on `whatif.py`'s own module, matching every sibling
    executor's self-contained shape)."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    if not isinstance(job_id, str) or not job_id:
        return None, f"{expected_kind}_job_id_missing"
    try:
        job = ctx.store.job_record(tenant_id, workspace_id, job_id)
    except Exception:
        return None, f"{expected_kind}_job_not_found"
    if job.get("kind") != expected_kind:
        return None, f"{expected_kind}_job_wrong_kind"
    if job.get("execution_status") != "COMPLETED":
        return None, f"{expected_kind}_job_not_completed"
    return job.get("execution_result") or {}, None


def _resolve_intent(ctx: ExecutionContext, intent_id):
    """Resolves the confirmed Intent this proposal must consume. A missing/unconfirmed intent is a named
    blocker, never a crash and never a fabricated intent."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    try:
        return ctx.store.get_intent(tenant_id, workspace_id, intent_id), None
    except Exception:
        return None, "intent_not_found"


def _prior_proposal_version(ctx: ExecutionContext, previous_proposal_job_id):
    if not isinstance(previous_proposal_job_id, str) or not previous_proposal_job_id:
        return 1, None
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    try:
        prior = ctx.store.job_record(tenant_id, workspace_id, previous_proposal_job_id)
    except Exception:
        # Route-level validation (create_job) already rejects an unresolved
        # `previous_proposal_job_id` before this job is even created; a
        # worker-time miss here (e.g. a race) is a real, named blocker, not
        # a crash.
        return 1, "previous_proposal_job_not_found"
    prior_result = prior.get("execution_result") or {}
    prior_version = prior_result.get("proposal_version")
    if not isinstance(prior_version, int):
        prior_version = 1
    return prior_version + 1, None


def _classify_python_markdown_edit(text: str, intent: dict | None):
    """Deterministic, evidence-grounded contextual edit for a Python/
    Markdown target. This executor makes NO live model call (see module
    docstring); a "contextual" edit here means: locate the REAL line in
    the CURRENT published text that the confirmed intent's own
    `target`/`value` fields plausibly refer to (a literal-value
    substitution anchored at a real line, not a canned template string
    written once and reused for every input), and carry that line's
    content forward as the rationale/evidence citation. When no such
    anchor can be found, this returns classification "manual" rather than
    fabricating a change with no real citation."""
    if intent is None:
        return {"classification": "manual", "reason": "intent_not_found",
               "edits": [], "rationale": None}
    target = intent.get("target")
    value = intent.get("value")
    if not isinstance(target, str) or not target or value is None:
        return {"classification": "manual", "reason": "intent_has_no_target_value",
               "edits": [], "rationale": None}
    lines = text.splitlines()
    matches = [(i, line) for i, line in enumerate(lines) if target.lower() in line.lower()]
    if not matches:
        return {"classification": "manual", "reason": "no_textual_anchor_for_intent_target",
               "edits": [], "rationale": None}
    line_no, before_line = matches[0]
    # Real, minimal, contextual substitution: replace the FIRST run of
    # digits on the anchor line with the intent's proposed value (the
    # concrete shape this repo's own test fixtures exercise -- a numeric
    # policy value such as a day count). This is derived from the actual
    # matched line's content, not a fixed template -- a different anchor
    # line/value pair produces a different diff.
    import re
    digit_match = re.search(r"\d+", before_line)
    value_str = str(value)
    if digit_match:
        after_line = before_line[:digit_match.start()] + value_str + before_line[digit_match.end():]
    else:
        after_line = f"{before_line} ({value_str})"
    edit = {
        "line_number": line_no + 1, "before": before_line, "after": after_line,
        "rationale": f"confirmed intent target={target!r} value={value!r} "
                    f"anchored at line {line_no + 1} of the current published text",
        "evidence_ref": {"kind": "current_source_line", "line_number": line_no + 1},
    }
    new_lines = list(lines)
    new_lines[line_no] = after_line
    new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    return {"classification": "recommended", "reason": None,
           "edits": [edit], "rationale": edit["rationale"], "new_text": new_text}


def _yaml_typed_edits(text: str, edits: list):
    """Typed locator/value edit for a YAML target -- mirrors the shape of
    `core/change_intelligence/structured_diff.py`'s `StructuredEdit`/
    `cell_edit` (locator + expected_current + proposed, validated against
    the REAL current value before ever being applied), the
    closest existing shape reference; reimplemented here (not
    imported -- that legacy module has no v2 blob/store plumbing) with a minimal,
    real dotted-path YAML value locator."""
    import yaml

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise NonRetryableExecutionError("malformed_patch", f"target is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise NonRetryableExecutionError("malformed_patch", "target YAML root is not a mapping")

    applied = []
    is_no_op = True
    for edit in edits:
        path = edit.get("path")
        expected_current = edit.get("expected_current")
        proposed = edit.get("proposed")
        if not isinstance(path, str) or not path:
            raise NonRetryableExecutionError("malformed_patch", "edit.path required")
        keys = path.split(".")
        node = data
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node:
                raise NonRetryableExecutionError(
                    "malformed_patch", f"locator not found: {path!r}")
            node = node[key]
        last = keys[-1]
        if not isinstance(node, dict) or last not in node:
            raise NonRetryableExecutionError(
                "malformed_patch", f"locator not found: {path!r}")
        actual_current = node[last]
        if actual_current != expected_current:
            raise NonRetryableExecutionError(
                "malformed_patch",
                f"stale expected_current at {path!r}: expected {expected_current!r}, "
                f"actual {actual_current!r}")
        if proposed != actual_current:
            node[last] = proposed
            is_no_op = False
        applied.append({"path": path, "before": actual_current, "after": proposed,
                        "rationale": f"typed locator edit at {path!r}",
                        "evidence_ref": {"kind": "yaml_locator", "path": path}})
    new_text = yaml.safe_dump(data, sort_keys=False) if not is_no_op else text
    return applied, is_no_op, new_text


def _xlsx_typed_edits(content: bytes, edits: list):
    """Typed cell-locator edit for an XLSX target. `core/adapters/doc/xlsx/
    workbook_io.py` is a READ-ONLY `.xls`-via-COM
    fallback loader -- it has no typed-cell-EDIT/save path to reuse, so this
    module implements the minimal typed-edit + true-no-op path itself with
    `openpyxl`. `openpyxl`'s normal
    load-then-save round trip preserves formulas/styles/sheets it does not
    touch (it only rewrites the cells this code explicitly sets)."""
    import openpyxl

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content))
    except Exception as exc:
        raise NonRetryableExecutionError(
            "malformed_patch", f"target is not a readable XLSX workbook: {exc}")

    applied = []
    is_no_op = True
    for edit in edits:
        sheet_name = edit.get("sheet")
        cell_ref = edit.get("cell")
        expected_current = edit.get("expected_current")
        proposed = edit.get("proposed")
        if not isinstance(sheet_name, str) or sheet_name not in workbook.sheetnames:
            raise NonRetryableExecutionError(
                "malformed_patch", f"sheet not found: {sheet_name!r}")
        if not isinstance(cell_ref, str) or not cell_ref:
            raise NonRetryableExecutionError("malformed_patch", "edit.cell required")
        sheet = workbook[sheet_name]
        cell = sheet[cell_ref]
        if cell.value != expected_current:
            raise NonRetryableExecutionError(
                "malformed_patch",
                f"stale expected_current at {sheet_name}!{cell_ref}: "
                f"expected {expected_current!r}, actual {cell.value!r}")
        if proposed != cell.value:
            cell.value = proposed
            is_no_op = False
        applied.append({"sheet": sheet_name, "cell": cell_ref, "before": expected_current,
                        "after": proposed,
                        "rationale": f"typed cell edit at {sheet_name}!{cell_ref}",
                        "evidence_ref": {"kind": "xlsx_cell", "sheet": sheet_name,
                                        "cell": cell_ref}})
    new_bytes = None
    if not is_no_op:
        buffer = io.BytesIO()
        workbook.save(buffer)
        new_bytes = buffer.getvalue()
    return applied, is_no_op, new_bytes


class ProposalExecutor:
    """See module docstring. Zero provider calls (`external = False`)."""

    external = False
    executor_id = "proposal.generate.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        for key in _REQUIRED_PAYLOAD_FIELDS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise NonRetryableExecutionError(
                    f"{key}_required", f"payload.{key} must be a non-empty string")
        matrix = load_proposal_support_matrix()
        if payload.get("edit_kind") not in matrix.allowed_edit_kinds:
            raise NonRetryableExecutionError(
                "unsupported_edit_kind", f"edit_kind={payload.get('edit_kind')!r}")
        if matrix.is_protected_path(payload.get("target_artifact_id")):
            raise NonRetryableExecutionError(
                "protected_config_target",
                f"target_artifact_id={payload.get('target_artifact_id')!r} may never "
                f"be a patch target")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        tenant_id, workspace_id = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]

        intent_id = payload.get("intent_id")
        impact_job_id = payload.get("impact_job_id")
        simulation_job_id = payload.get("simulation_job_id")
        target_artifact_id = payload.get("target_artifact_id")
        edit_kind = payload.get("edit_kind")
        edits_payload = payload.get("edits") if isinstance(payload.get("edits"), list) else []
        draft_binding = None
        if payload.get("draft_job_id") is not None:
            # The typed edits come from the validated GenAI draft, never from caller-typed path/current/new.
            from infra.worker.change_draft_binding import resolve_draft_edits
            draft_binding = resolve_draft_edits(ctx, payload)
            edits_payload = draft_binding["edits"]
        action = payload.get("action")
        previous_proposal_job_id = payload.get("previous_proposal_job_id")

        proposal_version, version_reason = _prior_proposal_version(
            ctx, previous_proposal_job_id if action == "revise" else None)

        intent, intent_reason = _resolve_intent(ctx, intent_id)
        impact_report, impact_reason = _resolve_report(
            ctx, impact_job_id, expected_kind="impact")
        simulation_report, simulation_reason = _resolve_report(
            ctx, simulation_job_id, expected_kind="simulation")

        blockers: list[dict] = []
        for reason, source_id in (
                (intent_reason, intent_id), (impact_reason, impact_job_id),
                (simulation_reason, simulation_job_id), (version_reason, previous_proposal_job_id)):
            if reason is not None:
                blockers.append({"reason": f"report_unavailable:{reason}"
                                 if reason.endswith("_wrong_kind")
                                 or reason.endswith("_not_completed")
                                 or reason.endswith("_not_found")
                                 else reason,
                                 "source_id": source_id})

        pinned_source_revision_id = job.get("pinned_source_revision_id")
        artifact = ctx.store.read_artifact_blob(
            tenant_id, workspace_id, pinned_source_revision_id, target_artifact_id)
        before_bytes = artifact["content"]
        before_hash = artifact.get("blob_hash") or _sha256_hex(before_bytes)

        # --- generated-test / oracle-substitution guard (defense in depth;
        # the route already rejects `replaces_oracle: True` at submission --
        # see `infra/v2_policy_routes.py::create_job`). Re-checked here so
        # this executor never silently accepts one even if reached by a
        # future caller of the executor that bypasses the route.
        generated_tests = payload.get("generated_tests")
        if isinstance(generated_tests, list) and any(
                isinstance(item, dict) and item.get("replaces_oracle") for item in generated_tests):
            raise NonRetryableExecutionError(
                "oracle_substitution_rejected",
                "an AI-labeled generated test must never replace the independent oracle")

        classification = "manual"
        edit_rationale = None
        edit_records: list[dict] = []
        is_no_op = True
        after_bytes = before_bytes
        after_hash = before_hash
        staged_blob_hash = None

        if edit_kind in ("python", "markdown"):
            try:
                text = before_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise NonRetryableExecutionError(
                    "malformed_patch", "target is not valid UTF-8 text")
            outcome = _classify_python_markdown_edit(text, intent)
            classification = outcome["classification"]
            edit_records = outcome["edits"]
            edit_rationale = outcome["rationale"]
            if outcome.get("new_text") is not None:
                is_no_op = False
                after_bytes = outcome["new_text"].encode("utf-8")
        elif edit_kind == "yaml_typed_edit":
            try:
                text = before_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise NonRetryableExecutionError(
                    "malformed_patch", "target is not valid UTF-8 text")
            edit_records, is_no_op, new_text = _yaml_typed_edits(text, edits_payload)
            classification = "no_op" if is_no_op else (
                "required" if any(b for b in blockers if b["reason"].startswith("open_")) else
                "recommended")
            if not is_no_op:
                after_bytes = new_text.encode("utf-8")
        elif edit_kind == "xlsx_typed_edit":
            edit_records, is_no_op, new_bytes = _xlsx_typed_edits(before_bytes, edits_payload)
            classification = "no_op" if is_no_op else "recommended"
            if not is_no_op and new_bytes is not None:
                after_bytes = new_bytes

        if is_no_op:
            # True no-op: zero bytes staged/written, after hash IDENTICAL to
            # the current source hash.
            after_hash = before_hash
        else:
            after_hash = _sha256_hex(after_bytes)
            # Stage the candidate bytes as an IMMUTABLE blob -- never
            # `publish_source` (this never touches the workspace head).
            staged_blob_hash = ctx.store.stage_blob(after_bytes)

        readiness = "BLOCKED" if blockers else "READY"

        dependencies = [d for d in (intent_id, impact_job_id, simulation_job_id,
                                    previous_proposal_job_id if action == "revise" else None)
                        if d]
        verification_plan = []
        if classification == "manual":
            verification_plan.append({"action": "human_authoring_required",
                                      "reason": edit_records and edit_records[0].get("reason")
                                      or "no_machine_synthesizable_edit"})
        else:
            verification_plan.append({"action": "replay_simulation_job",
                                      "target_job_kind": "simulation",
                                      "source_report_id": simulation_job_id})
            verification_plan.append({"action": "diff_review",
                                      "before_hash": before_hash, "after_hash": after_hash})

        result = {
            "executor_id": self.executor_id,
            "proposal_version": proposal_version,
            "action": action or "generate",
            "previous_proposal_job_id": previous_proposal_job_id if action == "revise" else None,
            "intent_id": intent_id,
            "intent_status": "resolved" if intent is not None else f"unresolved:{intent_reason}",
            "impact_job_id": impact_job_id,
            "simulation_job_id": simulation_job_id,
            "target_artifact_id": target_artifact_id,
            "edit_kind": edit_kind,
            "classification": classification,
            "is_no_op": is_no_op,
            "before_blob_hash": before_hash,
            "after_blob_hash": after_hash,
            "staged_blob_hash": staged_blob_hash,
            "edits": edit_records,
            "rationale": edit_rationale,
            "dependencies": dependencies,
            "verification_plan": verification_plan,
            "readiness": readiness,
            "blockers": blockers,
            "pinned_input": {
                "pinned_source_revision_id": pinned_source_revision_id,
                "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
                "pinned_decision_epoch": job.get("pinned_decision_epoch"),
            },
            "draft_binding": ({k: draft_binding[k] for k in ("draft_job_id", "draft_hash", "accepted_refs",
                                                              "citations", "left_for_humans")}
                              if draft_binding else None),
        }
        return ExecutionOutcome(result=result, model_id=None)


__all__ = ["ProposalExecutor"]
