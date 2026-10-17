"""Verification negatives: a missing write, unexpected
write, unreadable artifact or failed test is never PASS, and a required code
manual action is never masked by an edited workflow; plus the early-return
branches of the apply gate (no-op, manual-only) that never publish.

Every scenario reaches the verifier through the real chain (apply job ->
publish -> SOURCE_PUBLISHED outbox row -> dispatcher -> verification job). The
failure is injected at the WRITE / STORAGE boundary only (a tampered
`publish_source` content, a corrupted blob in the blob port, a mutated approved
plan), never by editing the verifier; the report must carry the exact reason and
must never be PASS, and the change must not become complete / eligible.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.ai8t03.conftest import (
    applied_change, artifact_bytes, as_reviewed_report, assert_head_unmoved, ids, change_dto, count_jobs,
    install_provider, mutate_job_result, outbox_rows, pump, reasons_of, report_of,
    sha256_hex, verification_job_id, write_oracle,
)

NOT_DONE = ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE", "COMPLETED")


def _target():
    return f"config-t03-{uuid4().hex[:6]}.yaml"


def _base_hash(setup, target):
    s, t, w = setup["store"], setup["tenant"], setup["workspace"]
    return s.read_artifact_blob(t, w, s.head(t, w).source_revision_id, target)["blob_hash"]


def _finish(setup, monkeypatch, tmp_path, change, *, oracle=30):
    if oracle is not None:
        write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=oracle)
    install_provider(monkeypatch, "ok")
    pump(setup)
    return report_of(change, setup["store"], setup["tenant"])


def _assert_blocked(setup, change, report):
    assert report["status"] != "PASSED"
    dto = change_dto(change["client"], setup["workspace"], change["apply_job_id"])
    assert dto["state"] not in NOT_DONE, dto
    assert dto["completion"]["eligible"] is False
    assert dto["completion"]["blocked_reasons"]
    assert_head_unmoved(setup, change)
    return dto


# ------------------------------------------------------------------ missing write

def test_z1_missing_write_is_fail_with_exact_reason(setup, monkeypatch, owner, tmp_path):
    """The apply gate reports COMPLETED/published, but the durable revision still
    carries the OLD target bytes (the write never landed). The verifier reads the
    published bytes, so the expected edit is missing."""
    target = _target()
    holder = {}

    def tamper(store, content):
        # replace the new bytes with the pre-apply blob: a revision exists, the edit does not
        holder["before"] = _base_hash(setup, target)
        return {k: holder["before"] for k in content}

    change = applied_change(setup, monkeypatch, owner, target=target, publish_tamper=tamper)
    report = _finish(setup, monkeypatch, tmp_path, change)
    assert report["status"] == "FAILED", report
    assert ids(report["missing_edits"]) == [target]
    assert f"missing_write:{target}" in reasons_of(report)
    assert report["checks"]["expected_edits"]["status"] == "FAIL"
    # exact actual bytes/hash are still shown: the OLD bytes
    entry = report["artifacts"][target]
    assert entry["blob_hash"] == holder["before"]
    assert entry["sha256"] == sha256_hex(
        artifact_bytes(setup["store"], setup["tenant"], setup["workspace"], change["s1"], target))
    assert report["actual_values"][target]["refund_window_days"] == change["expected_current"]
    _assert_blocked(setup, change, report)


# ------------------------------------------------------------------ unexpected write

@pytest.mark.parametrize("variant", ["new-artifact", "modified-sibling"])
def test_z1_unexpected_write_is_fail_with_exact_reason(
        setup, monkeypatch, owner, tmp_path, variant):
    target = _target()
    siblings = {"sibling-keep.yaml": "keep: 1\n"} if variant == "modified-sibling" else None
    rogue = "rogue-added.yaml" if variant == "new-artifact" else "sibling-keep.yaml"

    def tamper(store, content):
        return {**content, rogue: store.stage_blob(b"rogue: 99\n")}

    change = applied_change(setup, monkeypatch, owner, target=target, siblings=siblings,
                            publish_tamper=tamper)
    report = _finish(setup, monkeypatch, tmp_path, change)
    assert report["status"] == "FAILED", report
    assert rogue in ids(report["unexpected_edits"]) and target not in ids(report["unexpected_edits"])
    assert f"unexpected_write:{rogue}" in reasons_of(report)
    assert report["missing_edits"] == []
    assert report["artifacts"][rogue]["sha256"] == sha256_hex(b"rogue: 99\n")
    _assert_blocked(setup, change, report)


# ------------------------------------------------------------------ unreadable artifact

@pytest.mark.parametrize("mode", ["missing", "corrupt"])
@pytest.mark.parametrize("which", ["target", "sibling"])
def test_z1_unreadable_published_blob_is_fail_and_names_the_artifact(
        setup, monkeypatch, owner, tmp_path, mode, which):
    """Snapshot the WHOLE blob dict and restore it. A verifier
    that trusts the apply job's recorded hashes instead of reading the published
    bytes would report PASS here."""
    target = _target()
    siblings = {"sibling-read.yaml": "read_me: 1\n"}
    change = applied_change(setup, monkeypatch, owner, target=target, siblings=siblings)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    victim = target if which == "target" else "sibling-read.yaml"
    view = {a["artifact_id"]: a for a in
            store.source_revision_view(tenant, workspace, change["s1"])["artifacts"]}
    digest = view[victim]["blob_hash"]
    snapshot = dict(store.blobs.data)
    corrupt_bytes = b"tampered-not-yaml: [\n"
    try:
        if mode == "missing":
            store.blobs.data.pop(digest)
        else:
            store.blobs.data[digest] = corrupt_bytes
        write_oracle(tmp_path, monkeypatch, artifact_id=target, expected=30)
        install_provider(monkeypatch, "ok")
        pump(setup)
        report = report_of(change, store, tenant)
    finally:
        store.blobs.data.clear()
        store.blobs.data.update(snapshot)
    assert report["status"] == "FAILED", report
    assert f"unreadable_artifact:{victim}" in reasons_of(report)
    entry = report["artifacts"][victim]
    assert entry["blob_hash"] == digest
    if mode == "missing":
        assert entry["readable"] is False and entry["sha256"] is None
    else:
        assert entry["checksum_ok"] is False
        assert entry["sha256"] == sha256_hex(corrupt_bytes), "report must show the exact actual bytes' hash"
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] not in NOT_DONE and dto["completion"]["eligible"] is False


# ------------------------------------------------------------------ failed required check

def test_z1_unparseable_published_bytes_fail_the_required_parse_check(
        setup, monkeypatch, owner, tmp_path):
    target = _target()

    def tamper(store, content):
        return {k: store.stage_blob(b"refund_window_days: [unclosed\n") for k in content}

    change = applied_change(setup, monkeypatch, owner, target=target, publish_tamper=tamper)
    report = _finish(setup, monkeypatch, tmp_path, change)
    assert report["status"] == "FAILED", report
    assert report["checks"]["parse_after"]["status"] == "FAIL"
    assert "required_check_failed:parse_after" in reasons_of(report)
    _assert_blocked(setup, change, report)


def test_z1_wrong_value_written_is_read_from_actual_bytes_not_from_the_plan(
        setup, monkeypatch, owner, tmp_path):
    """Z3: the proposal/apply RECORD says 30; the durable bytes say 31. The report
    quotes 31 (read from the published bytes) and fails the diff + oracle."""
    target = _target()

    def tamper(store, content):
        return {k: store.stage_blob(b"refund_window_days: 31\n") for k in content}

    change = applied_change(setup, monkeypatch, owner, target=target, publish_tamper=tamper)
    report = _finish(setup, monkeypatch, tmp_path, change)
    assert report["actual_values"][target]["refund_window_days"] == 31
    assert report["status"] == "FAILED", report
    assert report["checks"]["acceptance_tests"]["status"] == "FAIL"
    assert report["checks"]["expected_edits"]["status"] == "FAIL"
    _assert_blocked(setup, change, report)


# ------------------------------------------------------------------ manual work

def _add_manual_code_action(setup):
    def before_apply(ready):
        def add(result):
            result.setdefault("verification_plan", []).append({
                "action": "human_authoring_required",
                "reason": "refund_service.py still hard-codes 14 days; code change required"})
        mutate_job_result(setup["store"], setup["tenant"], setup["workspace"],
                          ready["proposal_job_id"], add)
    return before_apply


def test_z1_required_manual_code_action_without_proof_is_partial_not_pass(
        setup, monkeypatch, owner, tmp_path):
    """The (fixed) workflow YAML verifies perfectly, oracle included. The approved
    plan still requires a manual code change with no proof: the report must be
    PARTIAL and keep the manual item; the change must not become complete."""
    change = applied_change(setup, monkeypatch, owner, before_apply=_add_manual_code_action(setup))
    assert change["apply_body"]["execution_result"]["pending_human_work"], "fixture must carry the manual item"
    report = _finish(setup, monkeypatch, tmp_path, change)
    assert report["checks"]["acceptance_tests"]["status"] == "PASS"
    assert report["checks"]["expected_edits"]["status"] == "PASS"
    assert report["status"] == "PARTIAL", report
    assert len(report["manual_work"]) == 1 and len(report["open_required_work"]) == 1
    as_reviewed_report(report)  # C15 invariant of the reviewed schema holds (not PASSED)
    item = report["manual_work"][0]
    assert item["action"] == "human_authoring_required" and item["proof"] is None
    assert "refund_service.py" in item["reason"]
    assert "manual_action_unproven" in reasons_of(report)
    dto = _assert_blocked(setup, change, report)
    assert dto["manual_work"] and dto["manual_work"][0]["proof"] is None


def test_z1_fail_outranks_partial_when_both_apply(setup, monkeypatch, owner, tmp_path):
    change = applied_change(setup, monkeypatch, owner, before_apply=_add_manual_code_action(setup))
    report = _finish(setup, monkeypatch, tmp_path, change, oracle=45)
    assert report["status"] == "FAILED", report
    assert "manual_action_unproven" in reasons_of(report)
    assert "required_check_failed:acceptance_tests" in reasons_of(report)


# ------------------------------------------------------------------ early-return branches never publish

def test_z1_no_op_apply_creates_no_verification_or_rebuild_work(setup, monkeypatch, owner, tmp_path):
    from tests.ai8t01.conftest import submit_claim_and_get_apply
    from tests.ai8t02.conftest import build_ready_bundles, drain_outbox, login

    workspace, store, tenant = setup["workspace"], setup["store"], setup["tenant"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_bundles(setup, client, workspace, monkeypatch, owner,
                                expected_current=14, proposed_values=(14,), record_approval=False)[0]
    drain_outbox(store, tenant, workspace)
    head = store.head(tenant, workspace)
    # W1: a no-op bundle is BLOCKED -> it can no longer be approved or applied at all
    from tests.ai8t02.conftest import assert_noop_bundle_is_unapprovable_and_unapplyable
    assert_noop_bundle_is_unapprovable_and_unapplyable(setup, client, workspace, ready)
    job_id = "never-applied"
    install_provider(monkeypatch, "ok")
    pump(setup)
    from tests.ai8t02.conftest import head_core
    assert head_core(store.head(tenant, workspace)) == head_core(head)
    assert count_jobs(store, tenant, workspace, "verification") == 0
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 0
    assert [r for r in outbox_rows(store, tenant, workspace, "SOURCE_PUBLISHED")
            if isinstance(r[3], dict) and r[3].get("job_id") == job_id] == []


def test_z1_manual_work_only_apply_never_becomes_eligible(setup, monkeypatch, owner):
    from tests.ai8t02.test_publish_f5_manual_work import _apply, _bundle, _client

    workspace, store, tenant = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    job_id = f"job-apply-{uuid4().hex}"
    body = _apply(setup, client, b, job_id=job_id)
    assert body["execution_status"] == "COMPLETED" and body["execution_result"]["published"] is False
    install_provider(monkeypatch, "ok")
    pump(setup)
    assert count_jobs(store, tenant, workspace, "verification") == 0
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 0
    resp = client.get(f"/v2/changes/{job_id}", params={"workspace_id": workspace})
    assert resp.status_code == 200, resp.text
    dto = resp.json()
    assert dto["state"] not in NOT_DONE
    assert dto["completion"]["eligible"] is False
    assert "manual_action_unproven" in " ".join(dto["completion"]["blocked_reasons"])
    assert dto["manual_work"] and dto["manual_work"][0]["proof"] is None
