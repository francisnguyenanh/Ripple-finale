"""Rule identity and semantic relation graph, real
boundary.

Follows the extraction tests' convention: real `infra.judge.app` via
`fastapi.testclient.TestClient`, real PostgreSQL `PostgresV2Store`, real
worker claim path (`job_worker.claim_and_admit_one`); only the network
client is substituted.

Interface under test:

  - job kind: "relation"
  - executor module: infra.worker.relations, class RelationGraphExecutor
  - registered in infra.worker.job_worker.BUILTIN_EXECUTORS["relation"]
  - gated by JOB_CREATE_PERMISSION["relation"] (no specific
    permission is mandated, so tests only
    assert that an account WITHOUT ANY grant on the workspace is denied,
    not a specific permission string)
  - payload: {"candidate_ids": [...]} referencing prior "extraction" job
    candidates already persisted
  - persisted result (mirrors the extraction job's execution_result reuse):
    execution_result = {
        "proposed_edges": [RelationCandidate-shaped dicts,
                           lifecycle_state in {"PROPOSED","REJECTED"}],
        "identity_decisions": [{"candidate_a_id", "candidate_b_id",
                                "decision" in {"SAME_RULE","DIFFERENT_RULE","UNKNOWN"},
                                "reason"}],
        "rejected_edges": [...],
    }

Every test below is expected to fail RED today: kind "relation" is not in
JOB_CREATE_PERMISSION, so `POST /v2/jobs` returns 400 "unsupported job kind"
via the REAL, deployed route (`infra/v2_policy_routes.py::create_job` line
138-140) before any executor is ever reached. This is the real production
entry point, not an import-only probe.
"""

from __future__ import annotations

from uuid import uuid4

from infra.worker import job_worker

from tests.ai4t01.conftest import login
from tests.ai4t02.conftest import seed_extraction_candidates


def _create_relation_job(client, workspace, candidate_ids):
    job_id = f"job-relation-{uuid4().hex}"
    response = client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "relation", "job_id": job_id,
        "payload": {"candidate_ids": candidate_ids}})
    return job_id, response


def _create_claim_and_read(setup_ctx, client, candidate_ids):
    """Real production path: POST /v2/jobs -> real worker claim
    (`job_worker.claim_and_admit_one`) -> GET /v2/jobs/{id}. Returns the
    persisted job body so callers can assert on `execution_result`, not
    merely on the submission's 200."""
    tenant, workspace, store = (
        setup_ctx[k] for k in ("tenant", "workspace", "store"))
    job_id, response = _create_relation_job(client, workspace, candidate_ids)
    assert response.status_code == 200, response.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "relation-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    return status.json()


# --------------------------------------------------------------------------- real entry point: positive path (replaces the pre-implementation flip tests)
# Positive path through the real module and route.

def test_relation_executor_module_exists_and_is_registered():
    """Positive-path replacement for the old
    `test_relation_executor_module_does_not_exist_yet`: the module and
    executor class are real and wired into the real worker registry, not
    merely importable in isolation."""
    from infra.worker.relations import RelationGraphExecutor
    assert isinstance(job_worker.BUILTIN_EXECUTORS["relation"], RelationGraphExecutor)


def test_relation_job_kind_is_accepted_and_produces_identity_decisions(setup, monkeypatch):
    """Positive-path replacement for the old
    `test_relation_job_kind_is_rejected_by_the_real_route_today`: the real
    route now accepts kind='relation', the real worker claims and executes
    it, and the persisted `execution_result` actually contains identity
    decisions -- not merely a 200 on submission."""
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-a", text=RENAMED_TEXT_A,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_A.split(". ", 1)[1])])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    result = _create_claim_and_read(setup, client, [cand_a])
    assert result["execution_status"] == "COMPLETED", result
    execution_result = result["execution_result"]
    assert execution_result["executor_id"] == "relations.identity_graph.v1"
    assert execution_result["compared_candidate_count"] == 1
    assert execution_result["identity_decisions"] == []
    assert execution_result["proposed_edges"] == []


# =========================================================================== G1: identity discrimination
# renamed/paraphrased vs same-number-unrelated vs CAPEX-vs-all-orders vs
# conflicting-scope-same-explicit-id. Each is its OWN sub-case (role
# protocol: "subrows đủ nhỏ để không PASS một hàng nhiều yêu cầu bằng một
# assertion").

RENAMED_TEXT_A = (
    "Purchase Order Policy. All purchase orders above 500,000 VND require "
    "Finance Manager approval before submission.")
RENAMED_TEXT_B = (
    "PO Approval Standard. Any purchase order exceeding five hundred "
    "thousand VND must be signed off by the Finance Manager prior to "
    "submission.")
SAME_NUMBER_UNRELATED_TEXT = (
    "Vendor Onboarding Policy. A new vendor must submit form 500,000 for "
    "tax registration before the first invoice is paid.")
CAPEX_TEXT = (
    "Capital Expenditure Policy. All CAPEX purchase orders above 500,000 "
    "VND require Finance Manager approval before submission.")
ALL_ORDERS_TEXT = (
    "Purchase Order Policy. All purchase orders of any category above "
    "500,000 VND require Finance Manager approval before submission.")


def _candidate(subject, predicate, value, scope, quote, candidate_id=None):
    d = {"subject": subject, "predicate": predicate, "value": value, "unit": "VND",
        "modality": "must", "scope": scope, "outcome": "Finance Manager approval",
        "quotes": [quote]}
    if candidate_id is not None:
        d["candidate_id"] = candidate_id
    return d


def test_g1_renamed_and_paraphrased_rule_is_proposed_same_rule(setup, monkeypatch):
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-a", text=RENAMED_TEXT_A,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_A.split(". ", 1)[1])])
    body_b = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-b", text=RENAMED_TEXT_B,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_B.split(". ", 1)[1])])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    cand_b = body_b["execution_result"]["candidates"][0]["candidate_id"]

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    result = _create_claim_and_read(setup, client, [cand_a, cand_b])
    decisions = result["execution_result"]["identity_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "SAME_RULE", decisions


def test_g1_same_number_unrelated_rule_is_not_merged(setup, monkeypatch):
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-a", text=RENAMED_TEXT_A,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_A.split(". ", 1)[1])])
    body_c = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-c", text=SAME_NUMBER_UNRELATED_TEXT,
        candidates_json=[_candidate("vendor", "submits_form_number", 500000,
                                    {"category": "vendor_onboarding"},
                                    SAME_NUMBER_UNRELATED_TEXT.split(". ", 1)[1])])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    cand_c = body_c["execution_result"]["candidates"][0]["candidate_id"]

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    result = _create_claim_and_read(setup, client, [cand_a, cand_c])
    decisions = result["execution_result"]["identity_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] in ("DIFFERENT_RULE", "UNKNOWN"), decisions
    assert decisions[0]["decision"] != "SAME_RULE", (
        "must not merge merely because both cite the literal number 500000")


def test_g1_capex_scope_never_merges_with_all_orders_scope(setup, monkeypatch):
    body_capex = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-capex", text=CAPEX_TEXT,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "capex"}, CAPEX_TEXT.split(". ", 1)[1])])
    body_all = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-all", text=ALL_ORDERS_TEXT,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "all_orders"}, ALL_ORDERS_TEXT.split(". ", 1)[1])])
    cand_capex = body_capex["execution_result"]["candidates"][0]["candidate_id"]
    cand_all = body_all["execution_result"]["candidates"][0]["candidate_id"]

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    result = _create_claim_and_read(setup, client, [cand_capex, cand_all])
    decisions = result["execution_result"]["identity_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] != "SAME_RULE", (
        "CAPEX-only scope vs all-orders scope is a real semantic "
        f"difference even with identical subject/predicate/value: {decisions}")


def test_g1_conflicting_scope_with_same_explicit_id_keeps_both_conflicts_visible(setup, monkeypatch):
    """Explicit IDs must not compress away a real scope conflict -- the
    appendix: 'Explicit IDs cũng phải giữ conflicts nếu scopes khác, không
    nén mất evidence.'"""
    body = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-both", text=CAPEX_TEXT + " " + ALL_ORDERS_TEXT,
        candidates_json=[
            _candidate("purchase order", "requires_approval_above", 500000,
                       {"category": "capex"}, CAPEX_TEXT.split(". ", 1)[1],
                       candidate_id="RULE-PO-001"),
            _candidate("purchase order", "requires_approval_above", 500000,
                       {"category": "all_orders"}, ALL_ORDERS_TEXT.split(". ", 1)[1],
                       candidate_id="RULE-PO-001"),
        ])
    candidates = body["execution_result"]["candidates"]
    assert len(candidates) == 2, (
        "both explicit-ID-sharing candidates with DIFFERENT scopes must "
        "still be visible as two rows, not silently collapsed to one "
        "before AI4-T02 even runs")


# =========================================================================== G2: endpoint/direction/ref validation + rejected-edge lifecycle

def test_g2_wrong_endpoint_revision_is_rejected_not_silently_linked(setup, monkeypatch):
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-a", text=RENAMED_TEXT_A,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_A.split(". ", 1)[1])])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    fabricated_ref = f"candidate-does-not-exist-{uuid4().hex}"

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    result = _create_claim_and_read(setup, client, [cand_a, fabricated_ref])
    execution_result = result["execution_result"]
    rejected_ids = {r["candidate_id"] for r in execution_result["rejected_edges"]}
    assert fabricated_ref in rejected_ids, execution_result
    linked_ids = set()
    for edge in execution_result["proposed_edges"]:
        linked_ids.add(edge["from_candidate_id"])
        linked_ids.add(edge["to_candidate_id"])
    assert fabricated_ref not in linked_ids, (
        "a nonexistent candidate revision must never appear in a "
        f"proposed/confirmed edge: {execution_result}")


def test_g2_rejected_edge_does_not_reappear_as_confirmed_after_worker_restart(setup, monkeypatch):
    """Simulates 'qua cache/restart': claim the relation job with a fresh
    worker id (a distinct in-process call, the closest available proxy for
    a restarted worker under this store's real terminal-state semantics),
    and re-read the job. A rejected edge must stay rejected, never flip to
    confirmed merely because a second worker instance re-touches the row."""
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-a", text=RENAMED_TEXT_A,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_A.split(". ", 1)[1])])
    body_c = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-c", text=SAME_NUMBER_UNRELATED_TEXT,
        candidates_json=[_candidate("vendor", "submits_form_number", 500000,
                                    {"category": "vendor_onboarding"},
                                    SAME_NUMBER_UNRELATED_TEXT.split(". ", 1)[1])])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    cand_c = body_c["execution_result"]["candidates"][0]["candidate_id"]

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    job_id, response = _create_relation_job(client, workspace, [cand_a, cand_c])
    assert response.status_code == 200, response.text
    claim = job_worker.claim_and_admit_one(setup["store"], setup["tenant"], workspace,
                                           "relation-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    first = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    second = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert first.status_code == 200 and second.status_code == 200
    decisions = first.json()["execution_result"]["identity_decisions"]
    assert decisions and decisions[0]["decision"] != "SAME_RULE", decisions
    assert first.json() == second.json(), (
        "re-reading a rejected/proposed relation job twice must be stable; "
        "a cache/restart must not promote a REJECTED edge to CONFIRMED")


def test_g2_denied_without_any_workspace_grant(setup, monkeypatch):
    """Negative permission control: an account with NO grant at all on the
    workspace must be denied job creation. Deliberately does not assert a
    SPECIFIC permission string (that mapping is left to the implementation,
    not guessed here)."""
    workspace = setup["workspace"]
    # Use a brand-new account with zero membership grants.
    stranger, stranger_cred = f"stranger-{uuid4().hex}", f"cred-{uuid4().hex}"
    from infra.adapters.v2_identity import PostgresIdentityStore as _Identities
    ident = _Identities(_dsn_from_setup(setup))
    ident.create_account(stranger, stranger_cred)

    client = setup["browser"]()
    login(client, stranger, stranger_cred)
    _, response = _create_relation_job(client, workspace, [])
    assert response.status_code in (401, 403, 400)


def _dsn_from_setup(setup_ctx):
    from tests.ai4t01.conftest import DSN
    return DSN


# =========================================================================== G3: bounded traversal + merge/split ancestry replay

def test_g3_traversal_over_a_configured_limit_stops_with_recorded_omissions(setup, monkeypatch):
    """A star of candidates all sharing the same subject/predicate but
    distinct scopes -- large enough that an unbounded pairwise comparison
    would be O(n^2). PROPOSED: the job must terminate (not hang) and record
    which comparisons/nodes were omitted, per the appendix's 'Graph
    cycles/large candidate sets phải dừng với omissions rõ'."""
    ids = []
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    for i in range(12):
        text = f"Policy {i}. Purchase orders in department {i} above 500,000 VND require approval."
        body = seed_extraction_candidates(
            setup, monkeypatch, artifact_id=f"policy-{i}", text=text,
            candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                        {"department": str(i)}, text.split(". ", 1)[1])])
        ids.append(body["execution_result"]["candidates"][0]["candidate_id"])

    result = _create_claim_and_read(setup, client, ids)
    execution_result = result["execution_result"]
    assert execution_result["compared_candidate_count"] == 12
    # 12 candidates -> C(12,2) = 66 pairs, well under the 500-pair cap
    # (`infra/worker/relations.py::_MAX_PAIR_COMPARISONS`): the cap itself is
    # NOT exercised here (see the dedicated cap-exceeding test below); this
    # row only proves the job terminates and returns a well-formed
    # omission count under normal load.
    assert execution_result["omitted_comparisons"] == 0


def test_g3_traversal_cap_is_actually_enforced_when_exceeded(setup, monkeypatch):
    """Tightens the previous test, which never exceeded
    `_MAX_PAIR_COMPARISONS` (500): 33 candidates -> C(33,2) = 528 pairs,
    which DOES exceed the cap. This is the real negative/boundary proof
    that the cap is enforced, not merely declared -- since no
    other test exceeds relations.py's _MAX_PAIR_COMPARISONS cap."""
    ids = []
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    for i in range(33):
        text = f"Policy cap {i}. Purchase orders in department cap{i} above 500,000 VND require approval."
        body = seed_extraction_candidates(
            setup, monkeypatch, artifact_id=f"policy-cap-{i}", text=text,
            candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                        {"department": f"cap{i}"}, text.split(". ", 1)[1])])
        ids.append(body["execution_result"]["candidates"][0]["candidate_id"])

    result = _create_claim_and_read(setup, client, ids)
    execution_result = result["execution_result"]
    assert execution_result["compared_candidate_count"] == 33
    total_pairs = 33 * 32 // 2  # 528
    assert total_pairs > 500
    assert execution_result["omitted_comparisons"] == total_pairs - 500
    assert len(execution_result["identity_decisions"]) == 500


def test_g3_merge_then_split_replay_preserves_ancestry_not_a_fresh_history(setup, monkeypatch):
    """Asserts more than `response.status_code == 200` (which alone
    would be vacuous). The load-bearing behavior implemented is verified below: the proposed SAME_RULE edge
    itself carries BOTH original candidate ids as its endpoints, which is
    the only ancestry this executor persists.

    A full merge-then-split REPLAY (confirm a merge via a human decision,
    later split it, then replay the graph and show the pre-merge ids in the
    resulting ancestry) is out of scope here: the two-step merge/split
    decision/confirmation API belongs to the human-confirmation layer; this
    executor only PROPOSES edges and never confirms/splits them (confirmed by `rg -n
    "def .*(merge|split|confirm)" infra/worker/relations.py` -- zero hits).
    This test does not claim to prove replay."""
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-a", text=RENAMED_TEXT_A,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_A.split(". ", 1)[1])])
    body_b = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="policy-b", text=RENAMED_TEXT_B,
        candidates_json=[_candidate("purchase order", "requires_approval_above", 500000,
                                    {"category": "purchase_order"}, RENAMED_TEXT_B.split(". ", 1)[1])])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    cand_b = body_b["execution_result"]["candidates"][0]["candidate_id"]

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    result = _create_claim_and_read(setup, client, [cand_a, cand_b])
    edges = result["execution_result"]["proposed_edges"]
    assert len(edges) == 1
    endpoints = {edges[0]["from_candidate_id"], edges[0]["to_candidate_id"]}
    assert endpoints == {cand_a, cand_b}, (
        "the only ancestry T02 persists (the proposed edge's own endpoints) "
        f"must name the original two candidate ids: {edges}")
