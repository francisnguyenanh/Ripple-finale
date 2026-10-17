"""SYN-03: after a committed business change, a person chooses the automatic file edit or a manual edit, through the real routes, store, worker and publish gate.

What this proves: the approved YAML scalar edit is written as ONE new source revision (only that scalar on its own line changes; comments, order, every other
artifact untouched), the next analysis reads it back and the TARGET rule becomes `verified`; the manual choice writes nothing; every refusal (role, no approval,
not committed, stale binding, moved sources, stale current value, revoked approver) publishes nothing; code is never edited. No model: the understanding comes
from the CHG-01 STUB. Not human acceptance, not behaviour."""
from __future__ import annotations

import psycopg2
import pytest

from infra.adapters.v2_business import business_view
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store
from infra.worker import business_source_apply as bsa
from infra.worker.execution_types import NonRetryableExecutionError
from tests.final_loop.test_chg04_review import decide, login, review
from tests.final_loop.test_chg05_commit import TARGET, approved, commit
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn02_reconcile import YAML_PATH, analyse, refresh_yaml
from tests.pitch_brief.conftest import drain

OLD, NEW = b"standard_manager_threshold_jpy: 1000000", b"standard_manager_threshold_jpy: 1500000"


def source(q, client, cid):
    return client.get(f"/v2/business/changes/{cid}/source", params={"workspace_id": q.env.ws})


def choose(q, client, cid, mode, binding):
    return client.post(f"/v2/business/changes/{cid}/source/apply", json={"workspace_id": q.env.ws, "mode": mode, "binding": binding})


def committed_and_file_writes_approved(q):
    """Both approvals are decisions on the draft and are recorded BEFORE the commit (a decision after the knowledge moved is refused by CHG-04); the commit follows."""
    cid, approver, binding = approved(q)
    b = review(q, approver, cid).json()["binding"]
    r = decide(q, approver, cid, b, kind="file_writes", reason="The YAML edit is exactly the decided value.")
    assert b["edits_hash"] and r.status_code == 200, r.text
    assert commit(q, q.admin, cid, binding).status_code == 200
    drain(q.env)
    return cid, approver, b


def artifacts(q, revision_id):
    view = q.env.store.source_revision_view(q.env.tenant, q.env.ws, revision_id)
    return {a["logical_path"]: a for a in view["artifacts"]}


def revisions(q):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        return cur.fetchone()[0]


def test_the_ai_choice_writes_one_source_revision_with_only_the_approved_scalar_changed_and_the_target_is_read_back_as_verified(qa_env):
    q = qa_env
    cid, approver, b = committed_and_file_writes_approved(q)
    head0 = q.env.store.head(q.env.tenant, q.env.ws)
    before = artifacts(q, head0.source_revision_id)
    rv = review(q, q.admin, cid).json()
    assert rv["can"]["apply_source_edits"] is True and rv["source"]["file_writes_approved"] and rv["source"]["knowledge_committed"] and rv["source"]["job"] is None
    assert review(q, approver, cid).json()["can"]["apply_source_edits"] is False                                                              # change.approve is not change.apply
    s_req, s_admin = source(q, q.req, cid).json(), source(q, q.admin, cid).json()
    assert s_req["automatic_edit_available"] and "path" not in s_req["manual"][0] and "path" in s_admin["manual"][0]                            # paths need evidence.read
    assert all(m["action"] in ("MANUAL", "UNRESOLVED") for m in s_admin["manual"]) and any(m["kind"] == "document" for m in s_admin["manual"])        # the policy wording stays a person's work
    viewer = login(q, f"viewer-{q.env.nonce}", ("reviewer",))
    assert choose(q, viewer, cid, "ai", b).status_code == 404 and choose(q, viewer, cid, "manual", b).status_code == 404                        # not a party to the change: not found
    assert choose(q, approver, cid, "ai", b).status_code == 403                                                                                 # may decide, may not apply (no change.apply)
    r = choose(q, q.admin, cid, "ai", b)
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["job_id"]
    assert r.json()["created"] is True and r.json()["job"]["status"] in ("PENDING", "QUEUED", None) or r.json()["job"]["status"] != "COMPLETED"
    again = choose(q, q.admin, cid, "ai", b).json()
    assert again["created"] is False and again["job"]["job_id"] == job_id                                                                       # one job per edits_hash
    assert review(q, q.admin, cid).json()["can"]["apply_source_edits"] is False
    n = revisions(q)
    drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, job_id)
    assert rec["execution_status"] == "COMPLETED", rec["execution_reason"]
    res = rec["execution_result"]
    assert res["provider_calls"] == 0 and res["source_revision_id"] == f"src-apply-{job_id}" and res["previous_source_revision_id"] == head0.source_revision_id
    assert [e["key_path"] for e in res["edits"]] == ["routing_rules.standard_manager_threshold_jpy"] and res["edits"][0]["before"] == 1000000 and res["edits"][0]["after"] == 1500000
    assert all(l["action"] != "AI_EDIT_SUPPORTED" for l in res["left_for_humans"]) and any(l["path"].endswith(".md") for l in res["left_for_humans"])
    head1 = q.env.store.head(q.env.tenant, q.env.ws)
    assert head1.source_revision_id == res["source_revision_id"] and head1.version > head0.version and revisions(q) == n + 1
    after = artifacts(q, head1.source_revision_id)
    assert set(after) == set(before)
    for path, a in after.items():
        if path != YAML_PATH:
            assert a["blob_hash"] == before[path]["blob_hash"], path                                                                            # every other file byte-identical
    old = q.env.store.read_artifact_blob(q.env.tenant, q.env.ws, head0.source_revision_id, before[YAML_PATH]["artifact_id"])["content"]
    new = q.env.store.read_artifact_blob(q.env.tenant, q.env.ws, head1.source_revision_id, after[YAML_PATH]["artifact_id"])["content"]
    assert new == old.replace(OLD, NEW) and new != old and old.count(OLD) == 1                                                                 # one scalar; comments and order kept
    s = source(q, q.admin, cid).json()
    assert s["job"]["status"] == "COMPLETED" and s["job"]["files"] and "files" not in source(q, q.req, cid).json()["job"]
    comp = review(q, q.admin, cid).json()["completion"]
    assert comp["source_sync"] == "applied_pending_readback" and comp["behavioral_verification"] == "not_tested" and comp["knowledge_result"] == "committed"
    out = analyse(q)                                                                                                                             # SYN-02 carry-over reads the new bytes
    assert out["derived"]
    pending = business_view(q.env.store, q.env.tenant, q.env.ws)["pending"]
    rules = {r["rule_id"]: r for r in pending["rules"]}
    assert rules[TARGET]["implementation_status"] == "verified" and rules["rule-standard-manager-review"]["condition"]["value"] == 1500000
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND payload->>'kind'=%s", (q.env.tenant, q.env.ws, bsa.KIND))
        assert cur.fetchone()[0] >= 1


def test_the_manual_choice_writes_nothing_and_the_ai_choice_is_refused_before_approval_commit_or_with_a_stale_binding(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)
    b = review(q, approver, cid).json()["binding"]
    head = q.env.store.head(q.env.tenant, q.env.ws)
    n = revisions(q)
    m = choose(q, q.req, cid, "manual", b)
    assert m.status_code == 200 and m.json()["applies_nothing"] is True and m.json()["manual"] and "path" not in m.json()["manual"][0]
    assert choose(q, q.admin, cid, "ai", b).json()["code"] == "not_approved"                                                                    # file_writes not decided
    assert decide(q, approver, cid, b, kind="file_writes").status_code == 200
    assert choose(q, q.admin, cid, "ai", b).json()["code"] == "knowledge_not_committed"                                                         # sources follow the knowledge
    assert review(q, q.admin, cid).json()["can"]["apply_source_edits"] is False
    assert commit(q, q.admin, cid, binding).status_code == 200
    drain(q.env)
    assert choose(q, q.admin, cid, "ai", dict(b, edits_hash="0" * 64)).json()["code"] == "edits_changed"
    assert choose(q, q.admin, cid, "ai", dict(b, draft_hash="0" * 64)).json()["code"] == "draft_revised"
    assert choose(q, q.admin, cid, "nope", b).status_code == 400 and choose(q, q.admin, cid, "ai", {}).status_code == 409
    assert q.env.store.head(q.env.tenant, q.env.ws).source_revision_id == head.source_revision_id and revisions(q) == n
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s", (q.env.tenant, q.env.ws, bsa.KIND))
        assert cur.fetchone()[0] == 0


def test_a_revoked_approver_or_moved_sources_fail_the_job_with_zero_publication(qa_env):
    q = qa_env
    cid, approver, b = committed_and_file_writes_approved(q)
    r = choose(q, q.admin, cid, "ai", b)
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["job_id"]
    PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, f"approver-{q.env.nonce}")                     # approval no longer backed by a right
    head, n = q.env.store.head(q.env.tenant, q.env.ws), revisions(q)
    drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, job_id)
    assert rec["execution_status"] == "FAILED" and "approver_authority_revoked" in str(rec["execution_reason"])
    assert q.env.store.head(q.env.tenant, q.env.ws) == head and revisions(q) == n
    assert review(q, q.admin, cid).json()["completion"]["source_sync"] == "pending"                                                             # the divergence stays visible
    rules = {r["rule_id"]: r for r in business_view(q.env.store, q.env.tenant, q.env.ws)["accepted"]["rules"]}
    assert rules[TARGET]["implementation_status"] == "pending"


def test_a_file_that_changed_since_the_review_is_refused_at_apply_time_with_zero_publication(qa_env, monkeypatch):
    q = qa_env
    cid, approver, b = committed_and_file_writes_approved(q)
    r = choose(q, q.admin, cid, "ai", b)
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["job_id"]
    real = PostgresV2Store.read_artifact_blob

    def drifted(self, *a, **k):                                                                                                                  # the bytes at the head differ from what the review saw
        out = real(self, *a, **k)
        out["content"] = out["content"].replace(OLD, b"standard_manager_threshold_jpy: 1200000")
        return out
    head, n = q.env.store.head(q.env.tenant, q.env.ws), revisions(q)
    with monkeypatch.context() as m:                                                                                                                 # the env fixture shares this monkeypatch: never undo it wholesale
        m.setattr(PostgresV2Store, "read_artifact_blob", drifted)
        drain(q.env)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, job_id)
    assert rec["execution_status"] == "FAILED" and "stale_current_value" in str(rec["execution_reason"])
    assert q.env.store.head(q.env.tenant, q.env.ws) == head and revisions(q) == n
    refresh_yaml(q, 1000000, extra=b"\n# touched\n")                                                                                               # sources moved after the review
    assert choose(q, q.admin, cid, "ai", review(q, q.admin, cid).json()["binding"]).status_code in (409,)                                       # stale pin or existing job: never a second publish


TEXT = "# routing\nrouting_rules:\n  standard_manager_threshold_jpy: 1000000   # JPY\n  capex_finance_threshold_jpy: 2000000\n  high_risk_route: \"NEEDS_DIRECTOR\"\nflags:\n  strict: true\n"


def test_scalar_edits_keep_comments_and_refuse_stale_ambiguous_or_non_isolated_edits():
    new, applied = bsa.apply_scalar_edits(TEXT, [{"key_path": "routing_rules.standard_manager_threshold_jpy", "expected_current": 1000000, "proposed": 1500000}])
    assert new == TEXT.replace("1000000   # JPY", "1500000   # JPY") and applied[0]["line"] == 3
    new, _ = bsa.apply_scalar_edits(TEXT, [{"key_path": "flags.strict", "expected_current": True, "proposed": False}, {"key_path": "routing_rules.high_risk_route", "expected_current": "NEEDS_DIRECTOR", "proposed": "NEEDS VP"}])
    assert "strict: false" in new and 'high_risk_route: "NEEDS VP"' in new
    with pytest.raises(NonRetryableExecutionError, match="stale_current_value"):
        bsa.apply_scalar_edits(TEXT, [{"key_path": "routing_rules.standard_manager_threshold_jpy", "expected_current": 900000, "proposed": 1500000}])
    with pytest.raises(NonRetryableExecutionError, match="locator_not_found"):
        bsa.apply_scalar_edits(TEXT, [{"key_path": "routing_rules.missing", "expected_current": 1, "proposed": 2}])
    dup = TEXT + "other:\n  standard_manager_threshold_jpy: 1000000\n"
    with pytest.raises(NonRetryableExecutionError, match="ambiguous_key"):
        bsa.apply_scalar_edits(dup, [{"key_path": "routing_rules.standard_manager_threshold_jpy", "expected_current": 1000000, "proposed": 1500000}])
    with pytest.raises(NonRetryableExecutionError, match="malformed_file"):
        bsa.apply_scalar_edits("- a\n- b\n", [{"key_path": "a", "expected_current": 1, "proposed": 2}])
    with pytest.raises(NonRetryableExecutionError, match="unsupported_value"):
        bsa.apply_scalar_edits(TEXT, [{"key_path": "routing_rules.standard_manager_threshold_jpy", "expected_current": 1000000, "proposed": [1, 2]}])
