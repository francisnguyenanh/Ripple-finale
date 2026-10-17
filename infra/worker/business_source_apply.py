"""Job kind "business.source_apply" (SYN-03): write the APPROVED automatic file edits of a committed business change into a new source revision.

What it does, deterministically and with zero provider calls: re-read the current draft of the change, check that the `file_writes` approval is still the approval
of exactly these edits (`edits_hash`), that the knowledge change is committed, that the sources are still the pinned revision and that the approver still holds
`change.approve`; then, for every AI_EDIT_SUPPORTED item, re-read the artifact bytes at the head, verify the stored scalar is the confirmed current value, replace
that one scalar on its own line (comments, order and every other line untouched), re-parse and prove that only that key changed, stage the bytes and publish ONE
source revision through the existing CAS/authority/fence gate (`publish_source`, carry_forward: every other artifact of the head is kept). The publish queues the
usual `source_ingest`, so the next `analyse` re-observes the file and the carry-over (SYN-02) recomputes `implementation_status` from the new bytes.

What it never does: touch a MANUAL, NO_OP or UNRESOLVED item, a code file, a spreadsheet, a protected path, a file outside the write scope of the request, or
rewrite a YAML file through a dumper. A failure anywhere publishes nothing (`applies_nothing` stays true in the failure) and the knowledge keeps its TARGET rule
with `implementation_status = pending` (the honest divergence between decided meaning and sources)."""
from __future__ import annotations

import re
from typing import Mapping

import yaml

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2.source_ingest_adapters import PARSER_VERSION, parser_for
from infra.adapters import v2_business_review
from infra.adapters.v2_postgres import LeaseLost, StaleAuthorityError, StaleVersion, TenantScopeError
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget
from infra.worker.proposal_config import load_proposal_support_matrix

EXECUTOR_ID = "business.source_apply.v1"
KIND = "business.source_apply"
OPERATION = "business.source_apply.publish"
APPROVER_PERMISSION = "change.approve"
_SCALAR = r"(?P<value>-?\d+(?:\.\d+)?|true|false|True|False|\"[^\"\n]*\"|'[^'\n]*'|[^#\s][^#\n]*?)"


def _lookup(data, key_path: str):
    node = data
    for key in key_path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            raise NonRetryableExecutionError("locator_not_found", f"{key_path!r} is not a key of this file")
        node = node[key]
    return node


def _same_except(a, b, key_path: str) -> bool:
    """True when mappings a and b are equal everywhere except at key_path."""
    def strip(node, keys):
        if not keys or not isinstance(node, Mapping):
            return node
        out = {k: v for k, v in node.items() if k != keys[0]}
        if keys[0] in node and len(keys) > 1:
            out[keys[0]] = strip(node[keys[0]], keys[1:])
        return out
    keys = key_path.split(".")
    return strip(a, keys) == strip(b, keys)


def _same_scalar(a, b) -> bool:
    """Equal and of the same kind: a bool is never a number here, a number never a string."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return isinstance(a, str) and isinstance(b, str) and a == b


def _render(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return value if re.fullmatch(r"[A-Za-z0-9_./-]+", value) and value.lower() not in ("true", "false", "null", "yes", "no", "on", "off") else '"' + value.replace('"', '\\"') + '"'
    raise NonRetryableExecutionError("unsupported_value", "only a number, boolean or string scalar can be written")


def apply_scalar_edits(text: str, edits: list) -> tuple[str, list]:
    """Replace the scalar of each `{key_path, expected_current, proposed}` on its own line of a YAML mapping file. Refuses (nothing returned) when the file is not a mapping,
    a key is missing, the stored value is not the expected current value, the key line is ambiguous (duplicate last key with the same value) or the edit would change
    anything but that key. Comments, ordering and other lines are kept byte for byte."""
    try:
        before = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise NonRetryableExecutionError("malformed_file", f"not valid YAML: {exc}") from exc
    if not isinstance(before, Mapping):
        raise NonRetryableExecutionError("malformed_file", "the YAML root is not a mapping")
    lines = text.splitlines(keepends=True)
    applied = []
    for edit in edits:
        key_path, expected, proposed = edit["key_path"], edit["expected_current"], edit["proposed"]
        actual = _lookup(before, key_path)
        if not isinstance(actual, (bool, int, float, str)) or not _same_scalar(actual, expected):
            raise NonRetryableExecutionError("stale_current_value", f"{key_path!r} is {actual!r} in the current file, not the confirmed {expected!r}; the file changed since the review")
        last = key_path.split(".")[-1]
        pat = re.compile(r"^(?P<lead>\s*" + re.escape(last) + r"\s*:\s*)" + _SCALAR + r"(?P<tail>\s*(?:#.*)?)$")
        hits = []
        for n, line in enumerate(lines):
            m = pat.match(line.rstrip("\r\n"))
            if m:
                try:
                    v = yaml.safe_load(m.group("value"))
                except yaml.YAMLError:
                    continue
                if _same_scalar(v, actual):
                    hits.append((n, m))
        if len(hits) != 1:
            raise NonRetryableExecutionError("ambiguous_key", f"{key_path!r}: {len(hits)} candidate lines; a person edits this file")
        n, m = hits[0]
        eol = lines[n][len(lines[n].rstrip("\r\n")):]
        lines[n] = m.group("lead") + _render(proposed) + m.group("tail") + eol
        after = yaml.safe_load("".join(lines))
        if not isinstance(after, Mapping) or _lookup(after, key_path) != proposed or not _same_except(before, after, key_path):
            raise NonRetryableExecutionError("edit_not_isolated", f"writing {key_path!r} would change more than that key; a person edits this file")
        before = after
        applied.append({"key_path": key_path, "line": n + 1, "before": actual, "after": proposed})
    return "".join(lines), applied


def read_back(store, tenant_id, workspace_id, revision_id) -> dict:
    """Observations and retrieval index for the NEW revision, the same way the post-publish stage of the Pitch apply records them (idempotent per revision; old rows
    untouched). Without this the carry-over could only say `unverifiable`; with it the next analysis reads the edited scalar back."""
    view = store.source_revision_view(tenant_id, workspace_id, revision_id)
    counts, complete = {}, True
    for art in view["artifacts"]:
        name = art.get("logical_path") or art["artifact_id"]
        parser = parser_for(name)
        if parser is None:
            complete, counts[art["artifact_id"]] = False, 0
            continue
        content = store.read_blob(tenant_id, workspace_id, revision_id, art["artifact_id"])
        parsed = parser(name, content)
        if parsed.parse_status != "parsed":
            complete = False
        observations = list(parsed.observations)
        if not any(o.get("kind") == "text_chunk" for o in observations):
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = None
            if text and text.strip():
                observations.append({"kind": "text_chunk", "locator": {"type": "line_span", "line_start": 1, "line_end": len(text.splitlines())}, "raw_value": text, "fields": {},
                                     "parser_version": PARSER_VERSION})
        if observations:
            store.record_source_observations(tenant_id, workspace_id, revision_id, art["artifact_id"], observations)
        counts[art["artifact_id"]] = len(observations)
    store.build_retrieval_index(tenant_id, workspace_id, revision_id, source_coverage_complete=complete,
                                source_coverage_reason="all artifacts of the published revision parsed" if complete else "at least one artifact has no parser or did not fully parse")
    return {"observations": counts, "coverage_complete": complete}


def approved_file_writes(store, tenant_id, workspace_id, change_id, draft):
    """-> the current `file_writes` approve decision of this draft's edits, or None."""
    states = v2_business_review.decision_states(v2_business_review.list_decisions(store, tenant_id, workspace_id, change_id), draft)
    cur = v2_business_review._latest_current(states, "file_writes")
    return cur if cur is not None and cur["action"] == "approve" else None


class BusinessSourceApplyExecutor:
    external = False
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        for key in ("change_id", "edits_hash", "decision_id", "source_revision_id"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise NonRetryableExecutionError("payload_invalid", f"missing {key}")
        return ExecutionRequest(prompt=None, schema={"type": "object", "executor": EXECUTOR_ID}, provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        store, tenant_id, workspace_id, payload = ctx.store, job["tenant_id"], job["workspace_id"], job["payload"]
        change_id = payload["change_id"]
        from infra.adapters import v2_business_commit
        got = v2_business_review.current_draft(store, tenant_id, workspace_id, change_id)
        if got is None:
            raise NonRetryableExecutionError("not_confirmed", "the request is not confirmed")
        draft, _w, parts = got
        if bd.edits_hash(draft) != payload["edits_hash"]:
            raise NonRetryableExecutionError("edits_changed", "the proposed edits are not the approved ones any more")
        decision = approved_file_writes(store, tenant_id, workspace_id, change_id, draft)
        if decision is None or decision["decision_id"] != payload["decision_id"]:
            raise NonRetryableExecutionError("not_approved", "the file edits have no current approval")
        if v2_business_commit.committed_state(store, tenant_id, workspace_id, change_id) is None:
            raise NonRetryableExecutionError("knowledge_not_committed", "the knowledge change is not committed; sources follow the decided knowledge, never the other way round")
        head = store.head(tenant_id, workspace_id)
        if head.source_revision_id != payload["source_revision_id"] or draft["pin"].get("source_revision_id") != head.source_revision_id:
            raise NonRetryableExecutionError("source_moved", "the sources changed since the edits were reviewed; review the change again")
        write_scope = set(parts["intent"]["write_scope"])
        matrix = load_proposal_support_matrix()
        items = [i for i in draft["file_actions"] if i["action"] == "AI_EDIT_SUPPORTED"]
        if not items:
            raise NonRetryableExecutionError("nothing_to_apply", "the draft proposes no automatic edit")
        by_artifact: dict = {}
        for it in items:
            if it["kind"] != "config" or not it["path"].lower().endswith((".yaml", ".yml")) or not isinstance(it.get("proposal_edit"), Mapping):
                raise NonRetryableExecutionError("unsupported_edit", f"{it['path']}: only a YAML scalar is written automatically")
            if not set(it["rule_ids"]) <= write_scope:
                raise NonRetryableExecutionError("outside_write_scope", f"{it['path']} belongs to a rule the request does not change")
            if matrix.is_protected_path(it["resolved_artifact_id"]) or matrix.is_protected_path(it["path"]):
                raise NonRetryableExecutionError("protected_config_target", f"{it['path']} is a protected path")
            by_artifact.setdefault(it["resolved_artifact_id"], []).append(it)
        manifest, files, edits_out = {}, {}, []
        for artifact_id, its in by_artifact.items():
            try:
                art = store.read_artifact_blob(tenant_id, workspace_id, head.source_revision_id, artifact_id)
            except TenantScopeError as exc:
                raise NonRetryableExecutionError("artifact_missing", str(exc)) from exc
            try:
                text = art["content"].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NonRetryableExecutionError("malformed_file", "the file is not UTF-8 text") from exc
            new_text, applied = apply_scalar_edits(text, [dict(i["proposal_edit"]) for i in its])
            new_bytes = new_text.encode("utf-8")
            digest = store.stage_blob(new_bytes)
            manifest[artifact_id] = digest
            files[artifact_id] = {"path": art["logical_path"], "before_hash": art["blob_hash"], "after_hash": digest, "size_bytes": len(new_bytes)}
            edits_out += [dict(a, ref=i["ref"], path=art["logical_path"], artifact_id=artifact_id) for a, i in zip(applied, its)]
        revision_id = f"src-apply-{job['job_id']}"
        approver = decision["actor_id"]

        def _authority(cur, _store=store, _t=tenant_id, _w=workspace_id, _a=approver):
            _store._recheck_actor_authority(cur, _t, _w, _a, APPROVER_PERMISSION)

        def _head_check(locked, _v=head.version, _s=head.source_revision_id):
            if locked.version != _v or locked.source_revision_id != _s:
                raise StaleVersion("publish_stale_head")
        try:
            out = store.publish_source(tenant_id, workspace_id, revision_id, manifest, expected_version=head.version, operation=OPERATION, idempotency_key=f"{OPERATION}:{job['job_id']}",
                                       authority_check=_authority, head_check=_head_check, carry_forward=True, fence=ctx.lease,
                                       audit_context={"job_id": job["job_id"], "kind": KIND, "change_id": change_id, "file_writes_decision_id": decision["decision_id"], "approver_actor_id": approver,
                                                      "edits_hash": payload["edits_hash"]})
        except LeaseLost:
            raise
        except StaleAuthorityError as exc:
            raise NonRetryableExecutionError("approver_authority_revoked", str(exc)) from exc
        except StaleVersion as exc:
            raise NonRetryableExecutionError("publish_stale_head", str(exc)) from exc
        except NonRetryableExecutionError:
            raise
        except Exception as exc:
            raise NonRetryableExecutionError("publish_transaction_failed", f"{type(exc).__name__}: {exc}") from exc
        try:
            readback = read_back(store, tenant_id, workspace_id, revision_id)
        except Exception as exc:                                                   # the revision IS published; the job says so and fails honestly (the head moved, the files are not read back)
            raise NonRetryableExecutionError("readback_failed", f"published {revision_id}; observations not recorded: {type(exc).__name__}: {exc}") from exc
        left = [{"ref": i["ref"], "path": i["path"], "action": i["action"], "reason": i["reason"]} for i in draft["file_actions"] if i["role"] == "to_change" and i["action"] != "AI_EDIT_SUPPORTED"]
        result = {"executor_id": EXECUTOR_ID, "provider_calls": 0, "applies_nothing": False, "change_id": change_id, "edits_hash": payload["edits_hash"], "file_writes_decision_id": decision["decision_id"],
                  "approver_actor_id": approver, "source_revision_id": revision_id, "previous_source_revision_id": head.source_revision_id, "workspace_version": getattr(out, "version", None),
                  "files": files, "edits": edits_out, "left_for_humans": left, "readback": readback,
                  "statement": "The approved scalar edits are published as a new source revision; nothing else in the files changed. The knowledge re-observes the files on the next analysis; behaviour is not tested."}
        return ExecutionOutcome(result=result, model_id=None)
