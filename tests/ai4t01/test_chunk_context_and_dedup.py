"""Chunk-hierarchy/bounded-context assembly and in-run duplicate
consolidation for `infra/worker/extraction.py`. Uses the same lightweight
`scoped_store`-only boundary as `test_extraction_boundaries_round3.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from infra.worker import job_worker, extraction
from infra.worker.budget_config import BudgetConfig, ModelPrice
from tests.ai2t03.conftest import scoped_store  # noqa: F401  (fixture reuse)


def _config(**overrides):
    from decimal import Decimal
    values = dict(
        per_job_cap_usd=Decimal("1"), total_cap_usd=Decimal("50"),
        max_output_tokens=2000, max_input_tokens_per_call=8000,
        max_retries=0, max_provider_calls_per_job=1,
        model_prices={"gemini-priced": ModelPrice("gemini-priced", Decimal("0.01"), Decimal("0.02"))},
        default_model_id="gemini-priced", fail_closed_on_missing_usage=True,
        latency_slo_p50_seconds=10.0, latency_slo_p95_seconds=30.0,
    )
    values.setdefault("enabled_roles", "all")  # role allowlist: explicit (absent = no AI kind may run)
    values.update(overrides)
    return BudgetConfig(**values)


def _chunked_job(scoped_store, chunks: list, *, artifact_id="policy-doc"):
    """Publish one source revision with `artifact_id`'s bytes being the
    concatenation of `chunks`, PLUS one real, persisted observation row per
    chunk (ascending `observation_id`, matching the ingest
    ordering) -- the real shape a chunked parser leaves behind."""
    store, tenant, workspace, _dsn = scoped_store
    full_text = "\n\n".join(chunks)
    digest = store.stage_blob(full_text.encode())
    store.publish_source(tenant, workspace, "src-" + uuid4().hex, {artifact_id: digest},
                        expected_version=0, operation="source.import.commit",
                        idempotency_key=uuid4().hex)
    revision_id = store.head(tenant, workspace).source_revision_id
    store.record_source_observations(
        tenant, workspace, revision_id, artifact_id,
        [{"kind": "text_chunk", "locator": {"chunk": i}, "raw_value": chunk,
         "fields": {}, "parser_version": "1.0.0"} for i, chunk in enumerate(chunks)])
    head = store.head(tenant, workspace)
    job_id = "job-" + uuid4().hex
    store.create_job(tenant, workspace, job_id, "extraction",
                     {"artifact_id": artifact_id, "source_role": "normative"},
                     expected_version=head.version, idempotency_key="create-" + job_id)
    first = store.claim_outbox(tenant, workspace, "worker")
    assert first.kind == "SOURCE_PUBLISHED"
    store.ack_outbox(tenant, workspace, first.id, "worker", first.fencing_token)
    return store, tenant, workspace, job_id


def _client(candidates_json):
    def generate_content(**kwargs):
        return SimpleNamespace(
            text=json.dumps({"candidates": candidates_json}), model_version="gemini-priced",
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=10,
                                           thoughts_token_count=None, total_token_count=30))

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


# --------------------------------------------------------------------------- (3) chunk hierarchy + bounded cross-chunk context


def test_evidence_in_a_later_chunk_still_resolves_via_chunk_assembly(scoped_store, monkeypatch):
    """The rule's operative sentence lives in chunk #2, not chunk #0 -- the
    executor must assemble context from ALL of this artifact's persisted
    chunks (chunk hierarchy), not only read chunk #0 or fail to find the
    citation."""
    chunks = [
        "Section 1. Scope: this policy applies to all procurement staff.",
        "Section 2. Definitions omitted for brevity.",
        "Section 3. Purchase orders above 500,000 VND must be approved by a "
        "Finance Manager before submission.",
    ]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must", "scope": {"category": "po"},
         "quotes": ["Purchase orders above 500,000 VND must be approved by a Finance Manager "
                   "before submission"]}]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", record
    result = record["execution_result"]
    assert len(result["candidates"]) == 1, result
    assert result["context_chunks_total"] == 3
    assert result["context_chunks_used"] == 3, "no truncation with only 3 chunks"


def test_context_bound_truncates_and_reports_coverage_not_a_silent_drop(scoped_store, monkeypatch):
    """More chunks than the configured bound: the executor must still run
    (bounded, not unbounded cost), and the result must SAY it truncated
    (`context_chunks_used < context_chunks_total`), never silently drop the
    remainder with no trace -- same "report coverage, don't silently drop"
    principle the retrieval top-k already follows."""
    chunks = [f"Section {i}. Unrelated filler paragraph number {i}." for i in range(6)]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_MAX_CONTEXT_CHUNKS", 3)
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", record
    result = record["execution_result"]
    assert result["context_chunks_total"] == 6
    assert result["context_chunks_used"] == 3


def test_citation_outside_the_bounded_context_is_not_verifiable(scoped_store, monkeypatch):
    """A quote that only appears in a chunk BEYOND the bounded window the
    model was actually shown must be rejected as unverifiable -- bounding is
    real, not merely a reported number while resolution silently searches
    the whole artifact anyway."""
    chunks = ["Filler paragraph zero.", "Filler paragraph one.",
             "Purchase orders above 750,000 VND require Director approval."]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_MAX_CONTEXT_CHUNKS", 2)  # excludes chunk #2
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 750000,
         "modality": "must", "scope": {"category": "po"},
         "quotes": ["Purchase orders above 750,000 VND require Director approval"]}]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    result = record["execution_result"]
    assert result["candidates"] == [], result
    assert result["rejected"][0]["reason"] == "no_verifiable_citation"


def test_artifact_without_chunk_observations_falls_back_to_whole_blob(scoped_store, monkeypatch):
    """An artifact that was never chunk-ingested (no `text_chunk`
    observations recorded for it) must still work -- falls back to the
    whole-blob read, exactly as for non-chunked sources, rather than
    failing closed just because chunking metadata is absent."""
    store, tenant, workspace, _dsn = scoped_store
    text = "All refunds above 1,000,000 VND must be approved by the CFO."
    digest = store.stage_blob(text.encode())
    store.publish_source(tenant, workspace, "src-" + uuid4().hex, {"policy-doc": digest},
                        expected_version=0, operation="source.import.commit",
                        idempotency_key=uuid4().hex)
    # Deliberately NO record_source_observations call here.
    head = store.head(tenant, workspace)
    job_id = "job-" + uuid4().hex
    store.create_job(tenant, workspace, job_id, "extraction",
                     {"artifact_id": "policy-doc", "source_role": "normative"},
                     expected_version=head.version, idempotency_key="create-" + job_id)
    first = store.claim_outbox(tenant, workspace, "worker")
    store.ack_outbox(tenant, workspace, first.id, "worker", first.fencing_token)

    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", record
    assert record["execution_result"]["context_chunks_total"] is None
    assert record["execution_result"]["context_chunks_used"] is None


# --------------------------------------------------------------------------- (4) duplicate consolidation with ancestry


def test_same_rule_mentioned_in_two_chunks_consolidates_with_ancestry(scoped_store, monkeypatch):
    """The identical rule (same subject/predicate/scope) is stated once in
    a summary chunk and again, verbatim-adjacent, in a detail chunk. The
    model returns it as two candidate entries citing the two different
    quotes; the executor must consolidate them into ONE persisted
    candidate whose evidence includes BOTH quotes and whose `ancestry`
    records that a duplicate was merged -- never two separate rows for the
    same rule from one run."""
    chunks = [
        "Summary: purchase orders above 500,000 VND need Finance Manager approval.",
        "Detail: any purchase order exceeding 500,000 VND must be approved by "
        "the Finance Manager prior to submission.",
    ]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must", "scope": {"category": "po"},
         "quotes": ["purchase orders above 500,000 VND need Finance Manager approval"]},
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must", "scope": {"category": "po"},
         "quotes": ["any purchase order exceeding 500,000 VND must be approved by "
                   "the Finance Manager prior to submission"]},
    ]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    result = record["execution_result"]
    assert len(result["candidates"]) == 1, (
        "two mentions of the same rule in one run must consolidate to one candidate", result)
    candidate = result["candidates"][0]
    assert len(candidate["evidence"]) == 2, "both quotes must be preserved, not discarded"
    assert candidate["ancestry"]["merged_count"] == 2


def test_same_subject_predicate_different_scope_never_force_merged(scoped_store, monkeypatch):
    """CAPEX-only vs all-orders: same subject/predicate, DIFFERENT scope --
    must stay two separate candidates, never force-merged into one just
    because subject/predicate match."""
    chunks = [
        "CAPEX purchase orders above 500,000 VND require Finance Manager approval.",
        "All other purchase orders above 500,000 VND require Department Head approval.",
    ]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must", "scope": {"category": "capex"},
         "quotes": ["CAPEX purchase orders above 500,000 VND require Finance Manager approval"]},
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must", "scope": {"category": "all_orders"},
         "quotes": ["All other purchase orders above 500,000 VND require Department Head "
                   "approval"]},
    ]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    result = record["execution_result"]
    assert len(result["candidates"]) == 2, (
        "different scopes must never be force-merged despite matching subject/predicate", result)
    scopes = {c["scope"]["category"] for c in result["candidates"]}
    assert scopes == {"capex", "all_orders"}


def test_unresolved_scope_candidates_are_never_merged(scoped_store, monkeypatch):
    """Two UNKNOWN-applicability candidates (missing scope) must never be
    silently consolidated into one -- without a real scope, identity cannot
    be safely asserted, so each stays its own row/question."""
    chunks = ["Some purchase orders must be approved by a manager.",
             "Some purchase orders must be approved by a manager, per policy."]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([
        {"subject": "purchase order", "predicate": "requires_approval", "value": True,
         "modality": "must", "quotes": ["Some purchase orders must be approved by a manager"]},
        {"subject": "purchase order", "predicate": "requires_approval", "value": True,
         "modality": "must",
         "quotes": ["Some purchase orders must be approved by a manager, per policy"]},
    ]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    result = record["execution_result"]
    assert len(result["candidates"]) == 2, result
    assert all(c["applicability"] == "UNKNOWN" for c in result["candidates"])


# --------------------------------------------------------------------------- item (6) gap found while deciding "reuse GET
# /v2/jobs/{id} vs a dedicated route": the design requires "concise
# rationale từng candidate" and a missing-scope candidate must not silently
# lose its already-resolved real evidence. Both are exposed through the
# SAME existing read API, so no new route is needed for these fields.


def test_rationale_and_evidence_are_both_readable_through_the_existing_job_api(scoped_store, monkeypatch):
    chunks = ["All purchase orders above 500,000 VND must be approved by a "
             "Finance Manager before submission."]
    store, tenant, workspace, job_id = _chunked_job(scoped_store, chunks)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client([
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must", "scope": {"category": "po"},
         "rationale": "The text states an explicit VND threshold with a named approver role.",
         "quotes": ["All purchase orders above 500,000 VND must be approved by a Finance "
                   "Manager before submission"]},
        # Missing scope -- must stay UNKNOWN, but its real citation must NOT
        # be discarded just because scope is missing.
        {"subject": "purchase order", "predicate": "requires_approval_above", "value": 500000,
         "modality": "must",
         "rationale": "Same sentence but the model omitted scope this time.",
         "quotes": ["All purchase orders above 500,000 VND must be approved by a Finance "
                   "Manager before submission"]},
    ]))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    result = record["execution_result"]
    recognized = next(c for c in result["candidates"] if c["applicability"] == "RECOGNIZED")
    unknown = next(c for c in result["candidates"] if c["applicability"] == "UNKNOWN")
    assert recognized["rationale"] == ("The text states an explicit VND threshold with a "
                                       "named approver role.")
    assert unknown["rationale"] == "Same sentence but the model omitted scope this time."
    assert unknown["evidence"], (
        "a missing-scope candidate must keep its real, already-resolved citation", unknown)
    # Provenance (model id, prompt/schema hash) is already on the SAME job
    # record the candidates/evidence come from -- no separate route needed.
    assert record["execution_model_id"] == "gemini-priced"
    assert record["execution_schema_hash"]
