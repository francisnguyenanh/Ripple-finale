"""Fixtures for independent verification and knowledge rebuild from the new
source.

This docstring lists the interface the tests bind to. Every name below
extends an existing pattern.

Verification and rebuild contract
---------------------------------
* Trigger (existing durable link, contracts C13): ApplyExecutor's publish
  already writes an outbox row `kind="SOURCE_PUBLISHED"` whose payload carries
  `job_id`, `kind="apply"`, `approval_job_id`, `approver_actor_id`,
  `target_artifact_id`, `source_revision_id`, `version`
  (`tests/ai8t02/test_publish_round2_attacks.py::test_e1_...`).
  `job_worker.claim_and_admit_one` handles a claimed
  `SOURCE_PUBLISHED` event with `payload.kind == "apply"` by creating (through
  the existing `store.create_job`, idempotency key `job-create-<id>`) TWO
  deterministic jobs and then acking the event (create-then-ack: a crash
  between them redelivers and the deterministic ids make it idempotent):
    `verify-<apply_job_id>`  kind `"verification"`  (immutable machine report)
    `rebuild-<apply_job_id>` kind `"km_rebuild"`    (re-ingest + index + AI4
                                                     reanalysis scheduling)
  The two are independent: a failed/false report never blocks the rebuild and
  a failed rebuild never destroys the report (report immutability for
  result acceptance). Any other `SOURCE_PUBLISHED` event (no `kind`, i.e. every
  non-apply publish) creates nothing.
* Job kinds `"verification"` and `"km_rebuild"` (`infra/worker/verification.py`,
  registered in `job_worker.BUILTIN_EXECUTORS`): zero provider calls
  (`external=False`). NOT creatable through `POST /v2/jobs` (absent from
  `JOB_CREATE_PERMISSION` -> 400 "unsupported job kind"); retry only via the
  existing `POST /v2/jobs/{id}/retry` gated by `JOB_APPROVE_PERMISSION[kind] =
  "change.accept_result"` (matches `core/change_intelligence/v2/api_contract.py`
  `verification.retry`).
* VerificationReport = the dataclass
  `core.change_intelligence.v2.schema.VerificationReport` (contracts 2.2),
  serialized as the verification job's `execution_result`
  (`GET /v2/jobs/verify-<apply_job_id>`), immutable, plus additive keys. Binding:
    id (= job id), schema_version "1.0.0", expected_edits / actual_edits /
    unexpected_edits / missing_edits (sequences of mappings with `artifact_id`),
    parsing_result / tests_result / simulation_result / evidence_result
    (mappings with `status`), open_required_work (sequence of str), scope
    (carries the approved `verification_plan`), coverage,
    after_source_revision_id (= published revision), after_knowledge_revision_id
    (None: a KM is only a candidate at machine time)
    status             PASSED | FAILED | NOT_RUN | PARTIAL (reviewed enum;
                       priority FAILED > NOT_RUN > PARTIAL > PASSED; C15: never
                       PASSED with open_required_work / missing_edits)
    reasons            list[str] of stable codes: `missing_write:<artifact>`,
                       `unexpected_write:<artifact>`, `unreadable_artifact:
                       <artifact>`, `required_check_failed:<check>`,
                       `required_check_not_run:<check>`, `manual_action_unproven`
    additive keys      report_digest (canonical hash of the body excluding
                       itself), apply_job_id, approval_job_id, parent_revision_id
    checks             {name: {"status": PASS|FAIL|NOT_RUN|SKIPPED_NOT_APPLICABLE,
                       "required": bool, "detail": str}}; names
                       published_bytes_readback, expected_edits, parse_after,
                       acceptance_tests, simulation_exact_after
    manual_work        [{"index", "action", "reason", "proof": None}]
    actual_values      values READ from the actual after bytes, e.g.
                       {"<artifact>": {"refund_window_days": 30}}
    verification_scope exact scope carried from the approved plan
* Independent acceptance oracle (config, tighten-only):
  env `RIPPLE_VERIFICATION_ORACLE_FILE` -> JSON file
  `{"oracles": [{"oracle_id","artifact_id","kind":"yaml_value_equals","path",
  "expected","requirement_ref"}]}`. Without it the `acceptance_tests` check is
  `NOT_RUN` (never PASS): no independent harness = no PASS (C15/C20).
* Rebuild stage (`km_rebuild` job result, "rebuild" below): re-ingests the
  artifacts of the published revision (NEW observations under the NEW revision
  id; the old revision's observations are never touched), builds the retrieval
  index for it (`store.build_retrieval_index`), and schedules AI4 reanalysis as
  existing `extraction` jobs (one per artifact in scope, payload
  `caused_by_apply_job_id`) via `store.create_job`; scope FULL when dependency
  coverage is missing. If the head has moved past the published revision the
  rebuild result is `SUPERSEDED` and schedules nothing. A KM CANDIDATE
  (`km-cand-...`) is only a candidate: the workspace head (`version`,
  `source_revision_id`, `knowledge_revision_id`, `decision_epoch`) is NEVER
  moved by verification/rebuild/reanalysis. Human acceptance of the candidate
  reuses the reviewed `POST /v2/knowledge/accept`.
  Affected claims/edges: for every candidate extracted (job kind `extraction`)
  from a changed artifact, the rebuild creates one existing-kind `invalidation`
  job (`decision_id="apply-<apply_job_id>"`, `target=<candidate id>`)
  so dependents (finding/relation jobs) are marked stale + reanalysed with the
  recorded causation chain; result keys `invalidation` (what was routed) and
  `counterevidence` (list of S1-index hits, each with `blob_hash`).
  Decision carry-forward: rebuild result `carry_forward = {"carried": [...],
  "reopened": [{"decision_id","reason"}]}`; a decision is carried only when its
  evidence binding still holds on the new source.
* Read surface (also used by the result-acceptance tests): `GET /v2/changes/{apply_job_id}?
  workspace_id=` (project.read). Binding subset: `state` (lifecycle names of
  `core/change_intelligence/v2/lifecycle.py` "change" machine), `verification
  {job_id, status, report_digest}`, `reanalysis {status, scope, job_ids,
  reason}`, `knowledge {candidate_revision_id, status, accepted,
  retrieval_index_status, km_index_status}`, `completion {status, eligible,
  blocked_reasons, decision}`.

Reuses tests.ai8t02/ai8t01/ai7t0x/ai4t01 fixtures verbatim.
"""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.adapters import v2_postgres
from infra.worker import job_worker  # noqa: F401  (import first: circular import guard)
from infra.worker import extraction

from tests.ai4t01.conftest import DSN, drain_outbox, login, setup  # noqa: F401
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai7t01.conftest import get_job_status  # noqa: F401
from tests.ai8t01.conftest import (  # noqa: F401
    submit_apply_job,
    submit_claim_and_get_apply,
)
from tests.ai8t02.conftest import (  # noqa: F401
    artifact_text,
    build_ready_bundles,
    publish_footprint,
    reason_of,
)

VERIFICATION_KIND = "verification"
REBUILD_KIND = "km_rebuild"
ORACLE_ENV = "RIPPLE_VERIFICATION_ORACLE_FILE"


def verification_job_id(apply_job_id: str) -> str:
    """PROPOSED deterministic id (duplicate outbox delivery => same id =>
    idempotent create; discoverable without a lookup route)."""
    return f"verify-{apply_job_id}"


def rebuild_job_id(apply_job_id: str) -> str:
    return f"rebuild-{apply_job_id}"


# ---------------------------------------------------------------------------
# fixtures: an applied (published) change
# ---------------------------------------------------------------------------

def head_tuple(store, tenant, workspace):
    h = store.head(tenant, workspace)
    return (h.version, h.source_revision_id, h.knowledge_revision_id, h.decision_epoch)


def mutate_job_result(store, tenant, workspace, job_id, fn):
    """Same in-test SQL mutation of an upstream job's `execution_result` that
    the publish tests use (`_mutate_result`) to shape the approved plan."""
    from tests import _row_gate
    _row_gate.disable()  # W2-AI10-09: forged row after approval, see tests/_row_gate.py
    import copy

    from psycopg2.extras import Json
    with store._tx() as cur:
        cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND "
                    "workspace_id=%s AND job_id=%s", (tenant, workspace, job_id))
        result = copy.deepcopy(cur.fetchone()[0])
        fn(result)
        cur.execute("UPDATE v2_jobs SET execution_result=%s WHERE tenant_id=%s AND "
                    "workspace_id=%s AND job_id=%s", (Json(result), tenant, workspace, job_id))


def review_without_contract_defect(monkeypatch):
    """W1: with a real review check every approvable chain names an operator contract, so a verification
    with NEITHER a contract NOR an oracle is only reachable through a (simulated) review defect that
    lets a contract-less review PASS. Verification must still refuse to mint acceptance (NOT_RUN):
    its own second line of defence, which the pre-W1 "no oracle -> NOT_RUN" tests exist to prove."""
    from infra.worker import review as review_mod
    real = review_mod._check_acceptance_tests

    def lenient(*args, **kwargs):
        _status, detail, _info = real(*args, **kwargs)
        return "PASS", detail, None  # PASS, and no contract carried into the review result

    monkeypatch.setattr(review_mod, "_check_acceptance_tests", lenient)


def applied_change(setup, monkeypatch, owner, *, siblings=None, expected_current=14,
                   proposed=30, target=None, apply_job_id=None, drain=True,
                   publish_tamper=None, before_apply=None, base_text=None,
                   edit_path="refund_window_days"):
    """Real chain through a PUBLISHED apply: base head published ONCE (with
    optional multi-artifact `siblings`), proposal -> review -> approval
    -> recorded human approve (shared-head builder), then the real
    `POST /v2/jobs {kind: apply}` -> worker -> `publish_source`. Returns a dict
    with the requester `client`, ids, and the S0/S1 heads. The SOURCE_PUBLISHED
    outbox row is deliberately left PENDING (the post-publish worker owns it).
    """
    workspace, store, tenant = setup["workspace"], setup["store"], setup["tenant"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_bundles(
        setup, client, workspace, monkeypatch, owner, target_artifact_id=target,
        expected_current=expected_current, proposed_values=(proposed,),
        siblings=siblings, base_text=base_text,
        edit_path=edit_path)[0]
    if drain:
        drain_outbox(store, tenant, workspace)
    head_before = store.head(tenant, workspace)
    apply_job_id = apply_job_id or f"job-apply-{uuid4().hex}"
    if before_apply is not None:
        before_apply(ready)
    with monkeypatch.context() as mp:
        if publish_tamper is not None:
            # In-test failure injection at the WRITE boundary: the apply gate
            # believes it published what it staged, but the durable revision
            # differs (a buggy / compromised publisher). Scoped to the
            # `apply.publish` operation only, so fixture publishes are
            # unaffected.
            original = v2_postgres.PostgresV2Store.publish_source

            def _tampered(self, tenant_id, workspace_id, revision_id, content, *a, **k):
                if k.get("operation") == "apply.publish":
                    content = publish_tamper(self, dict(content))
                return original(self, tenant_id, workspace_id, revision_id, content, *a, **k)

            mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _tampered)
        body = submit_claim_and_get_apply(
            setup, client, workspace, approval_job_id=ready["approval_job_id"],
            job_id=apply_job_id, expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    head_after = store.head(tenant, workspace)
    assert head_after.source_revision_id != head_before.source_revision_id, body
    return {
        "client": client, "ready": ready, "apply_job_id": apply_job_id,
        "verify_job_id": verification_job_id(apply_job_id),
        "rebuild_job_id": rebuild_job_id(apply_job_id), "apply_body": body,
        "target": ready["target_artifact_id"], "siblings": dict(siblings or {}),
        "s0": head_before.source_revision_id, "s1": head_after.source_revision_id,
        "head_before": head_before, "head_after": head_after,
        "workspace": workspace, "expected_current": expected_current,
        "proposed": proposed,
    }


# ---------------------------------------------------------------------------
# reanalysis provider (existing AI4 seam: `extraction._client_for_model`)
# ---------------------------------------------------------------------------

def _client(response=None, error=None, calls=None):
    calls = calls if calls is not None else []

    def generate_content(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return response

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content),
                           calls=calls)


def _response(candidates_json, model="test-model"):
    return SimpleNamespace(
        text=json.dumps({"candidates": candidates_json}), model_version=model,
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=30,
                                       thoughts_token_count=None, total_token_count=70))


def install_provider(monkeypatch, mode="ok"):
    """Patch the AI4 extraction provider seam (the same seam
    `tests/ai4t02/conftest.py::seed_extraction_candidates` uses). `mode` is read
    on every call, so a test can flip `holder.mode` from "outage" to "ok" to
    model 'provider back'. Returns a holder with `.calls`."""
    holder = SimpleNamespace(mode=mode, calls=[])

    def factory(model):
        if holder.mode == "outage":
            return _client(error=TimeoutError("provider outage"), calls=holder.calls)
        return _client(_response([]), calls=holder.calls)

    monkeypatch.setattr(extraction, "_client_for_model", factory)
    return holder


# ---------------------------------------------------------------------------
# oracle config
# ---------------------------------------------------------------------------

def write_oracle(tmp_path, monkeypatch, *, artifact_id, expected, path="refund_window_days",
                 kind="yaml_value_equals"):
    f = tmp_path / f"oracle-{uuid4().hex[:6]}.json"
    f.write_text(json.dumps({"oracles": [{
        "oracle_id": "human-confirmed-1", "artifact_id": artifact_id, "kind": kind,
        "path": path, "expected": expected,
        "requirement_ref": "human-confirmed requirement R1"}]}), encoding="utf-8")
    monkeypatch.setenv(ORACLE_ENV, str(f))
    # A1: the operator SHA256 pin is mandatory (infra/worker/verification.py); a fixture oracle is pinned like a real one
    import hashlib
    monkeypatch.setenv("RIPPLE_VERIFICATION_ORACLE_SHA256", hashlib.sha256(f.read_bytes()).hexdigest())
    return f


# ---------------------------------------------------------------------------
# worker pump (drives the REAL dispatcher; never calls executors directly)
# ---------------------------------------------------------------------------

def _leased_job_events(store, tenant, workspace) -> int:
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s "
                    "AND kind='JOB_QUEUED' AND status='CLAIMED' AND attempts < 5",
                    (tenant, workspace))
        return cur.fetchone()[0]


def _pending_events(store, tenant, workspace) -> int:
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s "
                    "AND status='PENDING'", (tenant, workspace))
        return cur.fetchone()[0]


def pump(setup, *, max_steps=40, lease_seconds=2, wait_retry=True, prefix="pump",
         max_claims=None):
    """Repeatedly call `job_worker.claim_and_admit_one` until the queue is
    empty. Returns the claim log `[(kind, id, payload.kind)]` (an exception raised
    out of the dispatcher is logged as `("EXC", type, message)` so a test can
    assert none escaped -- an uncaught deterministic failure leaves a job RUNNING
    and its event redelivered forever). Unrelated
    non-job events are acked here (like `drain_outbox`) so they never
    starve later JOB_QUEUED claims. A `SOURCE_PUBLISHED` event of an apply is
    NEVER acked by the pump: only the production dispatcher may consume it
    (that is the T03 requirement). If it does not, the event simply stays
    CLAIMED until its lease expires and `max_steps` bounds the loop."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    log = []
    for _ in range(max_steps):
        if max_claims is not None and len(log) >= max_claims:
            break
        worker = f"{prefix}-{uuid4().hex[:6]}"
        try:
            claim = job_worker.claim_and_admit_one(store, tenant, workspace, worker,
                                                   lease_seconds=lease_seconds)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the test, not swallowed
            log.append(("EXC", type(exc).__name__, str(exc)))
            if sum(1 for e in log if e[0] == "EXC") > 3:
                break
            continue
        if claim is None:
            # `None` also covers a dispatcher branch that handled + acked an
            # event and returned nothing: keep going while work is PENDING.
            if _pending_events(store, tenant, workspace) > 0:
                continue
            if wait_retry and _leased_job_events(store, tenant, workspace) > 0:
                time.sleep(lease_seconds + 0.4)
                continue
            break
        payload = claim.payload if isinstance(claim.payload, dict) else {}
        log.append((claim.kind, claim.id, payload.get("kind")))
        if claim.kind == "JOB_QUEUED":
            continue
        if claim.kind == "SOURCE_PUBLISHED" and payload.get("kind") == "apply":
            continue  # only the production dispatcher may consume it (see docstring)
        if store.outbox_status(tenant, workspace, claim.id) == "CLAIMED":
            store.ack_outbox(tenant, workspace, claim.id, worker, claim.fencing_token)
    return log


def outbox_rows(store, tenant, workspace, kind=None):
    with store._tx() as cur:
        q = ("SELECT id, kind, status, payload FROM v2_outbox WHERE tenant_id=%s "
             "AND workspace_id=%s")
        args = [tenant, workspace]
        if kind:
            q += " AND kind=%s"
            args.append(kind)
        cur.execute(q + " ORDER BY id", args)
        return cur.fetchall()


def source_published_event(store, tenant, workspace, apply_job_id):
    rows = [r for r in outbox_rows(store, tenant, workspace, "SOURCE_PUBLISHED")
            if isinstance(r[3], dict) and r[3].get("job_id") == apply_job_id]
    assert len(rows) == 1, rows
    return rows[0]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def job_row(store, tenant, workspace, job_id):
    return store.job_record(tenant, workspace, job_id)


def report_of(change, store, tenant):
    """The verification job's persisted `execution_result` (asserting the job
    exists and COMPLETED first, so a missing job is a crisp failure)."""
    rec = job_row(store, tenant, change["workspace"], change["verify_job_id"])
    assert rec["execution_status"] == "COMPLETED", rec
    return rec["execution_result"]


def get_change(client, workspace, apply_job_id):
    return client.get(f"/v2/changes/{apply_job_id}", params={"workspace_id": workspace})


def change_dto(client, workspace, apply_job_id):
    resp = get_change(client, workspace, apply_job_id)
    assert resp.status_code == 200, resp.text
    return resp.json()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_bytes(store, tenant, workspace, revision, artifact_id) -> bytes:
    return store.read_artifact_blob(tenant, workspace, revision, artifact_id)["content"]


def run_to_machine_stage(setup, monkeypatch, owner, tmp_path, *, oracle_expected=30,
                         provider="ok", **applied_kw):
    """applied change + oracle + provider + pump. Returns the change dict with
    `provider` (the mutable holder)."""
    change = applied_change(setup, monkeypatch, owner, **applied_kw)
    if oracle_expected is not None:
        write_oracle(tmp_path, monkeypatch, artifact_id=change["target"],
                     expected=oracle_expected)
    change["provider"] = install_provider(monkeypatch, provider)
    change["pump_log"] = pump(setup)
    return change


def count_jobs(store, tenant, workspace, kind, *, like=None):
    with store._tx() as cur:
        q = "SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s"
        args = [tenant, workspace, kind]
        if like:
            q += " AND job_id LIKE %s"
            args.append(like)
        cur.execute(q, args)
        return cur.fetchone()[0]


def restarted(setup):
    """A fresh store object over the same DB and the same blob port -- a
    process restart as far as every durable claim is concerned (no in-memory
    state survives). Returns a shallow copy of `setup` using it."""
    new_store = v2_postgres.PostgresV2Store(DSN, setup["store"].blobs)
    return {**setup, "store": new_store}


def assert_head_unmoved(setup, change):
    """Verification / rebuild / reanalysis / retries never move the head:
    no version bump, no KM promotion, no epoch change."""
    h = change["head_after"]
    assert head_tuple(setup["store"], setup["tenant"], setup["workspace"]) == (
        h.version, h.source_revision_id, h.knowledge_revision_id, h.decision_epoch)


def reasons_of(report) -> str:
    return " ".join(str(r) for r in (report.get("reasons") or []))


def exceptions_of(log):
    return [e for e in log if e[0] == "EXC"]


def ids(items):
    """artifact ids of a list of edit mappings (reviewed VerificationReport
    `*_edits` fields are sequences of mappings)."""
    return [e["artifact_id"] for e in items]


def as_reviewed_report(report: dict):
    """Rebuild the persisted report as the schema dataclass
    (`core.change_intelligence.v2.schema.VerificationReport`): enforces the enum
    and the C15 invariant (never PASSED with open required work / missing
    edits) on whatever the executor persisted."""
    from core.change_intelligence.v2.schema import VerificationReport
    names = ("id", "expected_edits", "actual_edits", "unexpected_edits", "missing_edits",
             "parsing_result", "tests_result", "simulation_result", "evidence_result",
             "open_required_work", "scope", "coverage", "after_source_revision_id",
             "after_knowledge_revision_id", "status", "reasons", "schema_version")
    return VerificationReport(**{n: (tuple(report[n]) if isinstance(report[n], list) else report[n])
                                 for n in names})
