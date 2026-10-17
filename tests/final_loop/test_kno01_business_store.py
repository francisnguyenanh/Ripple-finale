"""KNO-01: business knowledge snapshots through the real store on a throwaway PostgreSQL.

Real path: import -> source revision -> store commit/read (and, for the legacy replay, the stub-provider extraction through the worker).
Provider: counting stub, no model call. Needs the DB fixture of tests/pitch_brief (skipped otherwise, which is NOT a pass).
"""
from __future__ import annotations

import copy
import json
import pathlib
import threading

import psycopg2
import pytest

from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters.v2_business import BusinessKnowledgeRefused, backfill_observed_snapshot, business_view, commit_snapshot
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store, StaleAuthorityError, StaleVersion, StoreConflict, TenantScopeError
from tests.pitch_brief.chain import APPROVAL, build, cand_id
from tests.pitch_brief.conftest import import_harbor

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "final_knowledge_loop"
KNOWLEDGE = json.loads((FIX / "expected_knowledge.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((FIX / "contract_examples.json").read_text(encoding="utf-8"))


@pytest.fixture
def ws(env):
    """A workspace with one real source revision (the Harbor import)."""
    admin = env.admin_client()
    import_harbor(env, admin)
    return env.store.head(env.tenant, env.ws)


def bind(rules, revision_id):
    """The fixture rules cite the synthetic revision `rev-1`; point them at the real source revision."""
    out = copy.deepcopy(rules)
    for rule in out:
        for b in rule["source_bindings"]:
            b["revision_id"] = revision_id
        for p in rule["provenance"]:
            if p["kind"] == "source_evidence":
                p["revision_id"] = revision_id
    return out


def observed(head):
    return bind(KNOWLEDGE["observed_rules"], head.source_revision_id)


def target(head):
    rule = bind([EXAMPLES["target_rule"]], head.source_revision_id)[0]
    return rule


DECISION = {"decision_id": "decision-s1-capex", "kind": "choose_target", "quote": "Finance reviews CAPEX from 2,000,000 JPY.",
            "body": {"chosen_rule_id": "rule-capex-finance-config"}}


def commit(env, head, *, key="k1", snapshot_id="snap-1", rules=None, decisions=None, relations=(), gaps=(), lifecycle="ACCEPTED", actor=None,
           store=None, expected_business_revision=0, version=None, **over):
    args = dict(snapshot_id=snapshot_id, actor_id=actor or env.admin, lifecycle_state=lifecycle,
                rules=rules if rules is not None else observed(head) + [target(head)],
                relations=relations, gaps=gaps, decisions=[DECISION] if decisions is None else decisions,
                expected_version=head.version if version is None else version, expected_business_revision=expected_business_revision,
                source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                idempotency_key=key)
    args.update(over)
    return commit_snapshot(store or env.store, env.tenant, env.ws, **args)


def counts(env, tenant=None):
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        out = {}
        for table in ("v2_business_snapshots", "v2_business_rules", "v2_business_relations", "v2_business_gaps", "v2_business_decisions"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND workspace_id=%s", (tenant or env.tenant, env.ws))
            out[table] = cur.fetchone()[0]
        return out


def workspace_row(env):
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version, source_revision_id, knowledge_revision_id, decision_epoch FROM v2_workspaces WHERE tenant_id=%s AND workspace_id=%s",
                    (env.tenant, env.ws))
        return cur.fetchone()


NOTHING = {"v2_business_snapshots": 0, "v2_business_rules": 0, "v2_business_relations": 0, "v2_business_gaps": 0, "v2_business_decisions": 0}


# ---- success paths -----------------------------------------------------------------------------------------------------------

def test_accepted_snapshot_survives_restart_and_reads_back_exactly(env, ws):
    result = commit(env, ws, relations=[EXAMPLES["relation"]], gaps=[EXAMPLES["gap"] | {"gap_id": bk.gap_id_for("missing_actor", ["rule-emergency-routing"], "x")}])
    assert result.version == ws.version + 1 and not result.replayed
    fresh = PostgresV2Store(env.dsn, env.blobs)                      # a second store instance = a restarted process
    view = business_view(fresh, env.tenant, env.ws)
    assert view["business_head"] == {"business_revision": 1, "accepted_snapshot_id": "snap-1"}
    assert view["accepted"]["snapshot"]["lifecycle_state"] == "ACCEPTED" and view["accepted"]["snapshot"]["business_revision"] == 1
    stored = {r["rule_id"]: r for r in view["accepted"]["rules"]}
    for rule in observed(ws) + [target(ws)]:
        assert stored[rule["rule_id"]] == rule                          # nothing lost or normalised away, incl. null condition/actor and unknowns
    assert view["accepted"]["decisions"][0]["decision_id"] == "decision-s1-capex" and view["accepted"]["decisions"][0]["actor_id"] == env.admin
    assert view["accepted"]["relations"][0]["edge_id"] == "edge-5" and len(view["accepted"]["gaps"]) == 1
    assert view["pending"] is None and view["observed_stale"] is False
    assert view["implementation"]["TARGET:partial"] == 1 and view["implementation"]["OBSERVED:divergent"] == 1


def test_target_commit_does_not_write_the_observed_state(env, ws):
    before = workspace_row(env)
    commit(env, ws)
    after = workspace_row(env)
    assert after[0] == before[0] + 1                                   # the workspace version moves like any commit
    assert after[1:] == before[1:]                                     # source head, knowledge head and decision epoch are untouched
    stored = {r["rule_id"]: r for r in business_view(env.store, env.tenant, env.ws)["accepted"]["rules"]}
    assert stored["rule-capex-finance-target"]["authority"] == "TARGET"
    assert stored["rule-capex-finance-config"]["authority"] == "OBSERVED" and stored["rule-capex-finance-config"]["implementation_status"] == "verified"


def test_second_snapshot_chains_to_the_first_and_authority_cannot_flip(env, ws):
    commit(env, ws)
    head = env.store.head(env.tenant, env.ws)
    flipped = observed(ws)
    flipped[0]["authority"], flipped[0]["status"] = "TARGET", "CONFIRMED"
    flipped[0]["provenance"] = [{"kind": "human_decision", "decision_id": "decision-s1-capex"}]
    with pytest.raises(BusinessKnowledgeRefused) as exc:
        commit(env, head, key="k2", snapshot_id="snap-2", rules=flipped, decisions=[], expected_business_revision=1)
    assert exc.value.code == "authority_change"
    commit(env, head, key="k3", snapshot_id="snap-2", expected_business_revision=1, decisions=[])
    view = business_view(env.store, env.tenant, env.ws)
    assert view["business_head"]["business_revision"] == 2 and view["accepted"]["snapshot"]["parent_snapshot_id"] == "snap-1"


def test_source_change_marks_observed_stale_not_the_accepted_target(env, ws):
    commit(env, ws)
    head = env.store.head(env.tenant, env.ws)
    digest = env.store.stage_blob(b"changed")
    env.store.publish_source(env.tenant, env.ws, "src-next", {"artifact-new": digest}, expected_version=head.version,
                             operation="source.import.commit", idempotency_key="next-src")
    view = business_view(env.store, env.tenant, env.ws)
    assert view["observed_stale"] is True and view["business_head"]["business_revision"] == 1 and view["accepted"]["snapshot"]["snapshot_id"] == "snap-1"


# ---- atomicity -----------------------------------------------------------------------------------------------------------------

def test_failure_inside_the_commit_leaves_no_partial_snapshot(env, ws, monkeypatch):
    before = workspace_row(env)

    def boom(*a, **k):
        raise RuntimeError("injected failure after the inserts")
    monkeypatch.setattr(PostgresV2Store, "_record_commit", boom)
    with pytest.raises(RuntimeError, match="injected"):
        commit(env, ws)
    monkeypatch.undo()
    assert counts(env) == NOTHING and workspace_row(env) == before
    assert business_view(env.store, env.tenant, env.ws)["business_head"] == {"business_revision": 0, "accepted_snapshot_id": None}
    commit(env, env.store.head(env.tenant, env.ws))                     # the same request succeeds afterwards: nothing half-done blocked it
    assert counts(env)["v2_business_snapshots"] == 1


def test_stored_rows_are_immutable(env, ws):
    commit(env, ws)
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        for sql in ("UPDATE v2_business_rules SET status='REJECTED' WHERE tenant_id=%s",
                    "DELETE FROM v2_business_snapshots WHERE tenant_id=%s"):
            with pytest.raises(psycopg2.Error, match="immutable"):
                cur.execute(sql, (env.tenant,))
            conn.rollback()


# ---- rejections: nothing written -------------------------------------------------------------------------------------------------

def test_unknown_references_refuse_the_whole_commit(env, ws):
    bad_binding = observed(ws)
    bad_binding[0]["source_bindings"][0]["revision_id"] = "no-such-revision"
    cases = [
        dict(rules=bad_binding, decisions=[]),                                                                        # unknown source revision
        dict(rules=observed(ws) + [target(ws)], decisions=[]),                                                        # decision never recorded
        dict(rules=observed(ws), decisions=[], relations=[dict(EXAMPLES["relation"], status="PROPOSED", to="rule-missing")]),  # relation to unknown rule
        dict(rules=observed(ws), decisions=[], gaps=[dict(EXAMPLES["gap"], affected_rule_ids=["rule-missing"])]),     # gap about unknown rule
        dict(rules=observed(ws), decisions=[], relations=[EXAMPLES["relation"] | {"to": "rule-emergency-routing", "provenance_refs": ["decision-ghost"]}]),
    ]
    for case in cases:
        with pytest.raises(BusinessKnowledgeRefused) as exc:
            commit(env, ws, **case)
        assert exc.value.code == "unknown_reference"
    assert counts(env) == NOTHING


def test_duplicate_ids_and_contract_violations_are_refused(env, ws):
    twice = observed(ws) + [observed(ws)[0]]
    with pytest.raises(BusinessKnowledgeRefused) as exc:
        commit(env, ws, rules=twice, decisions=[])
    assert exc.value.code == "duplicate_id"
    confirmed_by_ai = observed(ws)
    confirmed_by_ai[0]["status"] = "CONFIRMED"
    with pytest.raises(bk.ContractError) as exc2:
        commit(env, ws, rules=confirmed_by_ai, decisions=[])
    assert exc2.value.code == "forbidden_provenance"
    assert counts(env) == NOTHING


def test_stale_pins_and_versions_are_refused(env, ws):
    with pytest.raises(StaleVersion):
        commit(env, ws, version=ws.version + 5)
    with pytest.raises(StaleVersion):
        commit(env, ws, decision_epoch=ws.decision_epoch + 1)
    with pytest.raises(StaleVersion):
        commit(env, ws, source_revision_id="other")
    with pytest.raises(StaleVersion):
        commit(env, ws, km_revision_id="km-ghost")
    with pytest.raises(StaleVersion, match="business_revision"):
        commit(env, ws, expected_business_revision=3)
    assert counts(env) == NOTHING


def test_duplicate_confirm_is_idempotent_and_a_second_writer_is_stale(env, ws):
    first = commit(env, ws)
    again = commit(env, ws)                                              # same key, same payload: the first result, no new version
    assert again.replayed and again.version == first.version and counts(env)["v2_business_snapshots"] == 1
    with pytest.raises(StoreConflict, match="idempotency_conflict"):
        commit(env, ws, snapshot_id="snap-other")                         # same key, different payload
    with pytest.raises(StaleVersion):
        commit(env, env.store.head(env.tenant, env.ws), key="k-second", snapshot_id="snap-2")   # a second confirm against revision 0
    assert counts(env)["v2_business_snapshots"] == 1 and business_view(env.store, env.tenant, env.ws)["business_head"]["business_revision"] == 1


def test_revoked_or_unauthorised_actor_cannot_accept(env, ws):
    with pytest.raises(StaleAuthorityError):
        commit(env, ws, actor=env.req)                                    # a requester has no knowledge.confirm
    ids = PostgresIdentityStore(env.dsn)
    ids.revoke_membership(env.tenant, f"p-{env.nonce}", env.ws, env.admin)
    head = env.store.head(env.tenant, env.ws)
    with pytest.raises(StaleAuthorityError):
        commit(env, head)                                                 # revoked since the page was opened
    assert counts(env) == NOTHING


def test_cross_tenant_scope(env, ws):
    commit(env, ws)
    other = env.store.create_workspace("t-other-" + env.nonce, f"p-{env.nonce}", env.ws)
    assert business_view(env.store, "t-other-" + env.nonce, env.ws)["accepted"] is None       # same workspace id, other tenant: nothing visible
    assert counts(env, "t-other-" + env.nonce) == NOTHING
    with pytest.raises(TenantScopeError):
        business_view(env.store, "t-unknown", env.ws)
    with pytest.raises(TenantScopeError):
        commit_snapshot(env.store, "t-unknown", env.ws, snapshot_id="s", actor_id=env.admin, lifecycle_state="PENDING_HUMAN", rules=[], expected_version=0,
                        expected_business_revision=0, source_revision_id="x", km_revision_id=None, decision_epoch=0, idempotency_key="k")
    assert other.version == 0


# ---- concurrency -----------------------------------------------------------------------------------------------------------------

def test_concurrent_accepts_from_separate_connections_have_one_winner(env, ws):
    barrier, outcome = threading.Barrier(2), {}

    def worker(name):
        store = PostgresV2Store(env.dsn, env.blobs)                       # its own connections
        barrier.wait()
        try:
            commit(env, ws, key=f"k-{name}", snapshot_id=f"snap-{name}", store=store)
            outcome[name] = "ok"
        except StaleVersion:
            outcome[name] = "stale"
    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(outcome.values()) == ["ok", "stale"], outcome
    assert counts(env)["v2_business_snapshots"] == 1
    assert business_view(env.store, env.tenant, env.ws)["business_head"]["business_revision"] == 1


# ---- legacy candidates -> observed proposal -------------------------------------------------------------------------------------------

def test_legacy_candidates_replay_into_a_pending_observed_snapshot(env, monkeypatch):
    ch = build(env, monkeypatch)                                          # real import + worker-run stub extraction (no model)
    head = env.store.head(env.tenant, env.ws)
    out = backfill_observed_snapshot(env.store, env.tenant, env.ws, snapshot_id="snap-legacy", actor_id="system", idempotency_key="bf-1")
    view = business_view(env.store, env.tenant, env.ws)
    assert view["business_head"] == {"business_revision": 0, "accepted_snapshot_id": None} and view["accepted"] is None   # a proposal never becomes the head
    pending = view["pending"]
    assert pending["snapshot"]["lifecycle_state"] == "PENDING_HUMAN" and pending["snapshot"]["business_revision"] is None
    candidates = {c["candidate_id"]: c for cs in ch.cands.values() for c in cs}
    assert out["rules"] == len(pending["rules"]) == len(candidates) and out["skipped"] == []
    for rule in pending["rules"]:
        bk.validate_business_rule(rule)
        cand = candidates[rule["candidate_ids"][0]]
        assert rule["authority"] == "OBSERVED" and rule["status"] == "VALIDATED"                   # model confidence is never raised to CONFIRMED
        assert {b["evidence_ref"] for b in rule["source_bindings"]} == {e["locator"] for e in cand["evidence"]}
        assert rule["source_bindings"][0]["revision_id"] == head.source_revision_id
        assert "condition" in rule["unknown_fields"] and "actor_ref" in rule["unknown_fields"] and rule["condition"] is None   # legacy rows carry no operator or actor
    assert {g["kind"] for g in pending["gaps"]} == {"missing_condition", "missing_actor"} and len(pending["gaps"]) == 2 * len(candidates)
    again = backfill_observed_snapshot(env.store, env.tenant, env.ws, snapshot_id="snap-legacy", actor_id="system", idempotency_key="bf-1")
    assert again["result"].replayed and counts(env)["v2_business_snapshots"] == 1                      # re-run: same snapshot, no duplicate questions
    assert env.store.head(env.tenant, env.ws).source_revision_id == head.source_revision_id             # observed state untouched


def test_legacy_confirmation_needs_the_recorded_human_decision(env, monkeypatch):
    ch = build(env, monkeypatch)
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    r = ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": tid, "rationale": "KNO-01 legacy replay"})
    assert r.status_code == 200, r.text
    decision_id = r.json()["decision_id"]
    out = backfill_observed_snapshot(env.store, env.tenant, env.ws, snapshot_id="snap-legacy", actor_id="system", idempotency_key="bf-2",
                                     confirmations={tid: decision_id})
    rules = {r["candidate_ids"][0]: r for r in business_view(env.store, env.tenant, env.ws)["pending"]["rules"]}
    assert rules[tid]["status"] == "CONFIRMED" and {"kind": "human_decision", "decision_id": decision_id} in rules[tid]["provenance"]
    assert sum(1 for r in rules.values() if r["status"] == "CONFIRMED") == 1 and out["skipped"] == []
    with pytest.raises(BusinessKnowledgeRefused) as exc:                  # a decision id that was never recorded cannot confirm anything
        backfill_observed_snapshot(env.store, env.tenant, env.ws, snapshot_id="snap-ghost", actor_id="system", idempotency_key="bf-3",
                                   confirmations={tid: "decision-never-recorded"})
    assert exc.value.code == "unknown_reference"
