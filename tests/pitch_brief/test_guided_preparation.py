"""EUI-08 guided proposal preparation through the real routes, the shared job-creation contract, the worker and the store on the throwaway PostgreSQL
(contract docs/contracts/GUIDED_PREPARATION.md). The draft comes from the synthetic draft model of the B1 tests; impact, simulation and proposal are the real
deterministic executors. Covers: off by default, the manifest (pins, 0 paid calls, no edit preselected, choices needed), explicit start with the seen hash,
children created AS the starter and no approval given, idempotent start / advance, pins that move pause the run, another person sees nothing, cancel."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import drain
from tests.pitch_brief.test_change_draft import Model, _res, run


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def man(client, env, iid, **q):
    return client.get(f"/v2/intents/{iid}/preparation/manifest", params={"workspace_id": env.ws, **q})


def start(client, env, iid, h, accepted, target=None):
    return client.post(f"/v2/intents/{iid}/preparation", json={"workspace_id": env.ws, "manifest_hash": h, "accepted_refs": accepted, "target_candidate_id": target})


def advance(client, env, rid):
    return client.post(f"/v2/preparation/runs/{rid}/advance", json={"workspace_id": env.ws})


def jobs_of(env, kinds):
    with env.store._tx() as cur:
        cur.execute("SELECT kind, count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind = ANY(%s) GROUP BY kind", (env.tenant, env.ws, list(kinds)))
        return dict(cur.fetchall())


def drafted(env, ch, monkeypatch):
    jid, body = run(env, ch, monkeypatch, Model())
    return jid, _res(body)["intent"]["intent_id"]


def test_off_by_default_and_the_manifest_shows_pins_no_paid_call_and_no_preselected_edit(env, ch, monkeypatch):
    djid, iid = drafted(env, ch, monkeypatch)
    req = env.req_client()
    off = man(req, env, iid)
    assert off.status_code == 409 and off.json()["code"] == "preparation_off"
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    m = man(req, env, iid).json()
    assert m["pins"]["draft_job_id"] == djid and m["paid_calls"] == 0 and [s["stage"] for s in m["stages"]] == ["impact", "simulation", "proposal"]
    assert m["edits_offered"] and m["selected"]["accepted_refs"] == [] and "accepted_edits" in m["choices_needed"] and m["ready_to_start"] is False
    assert "none" in m["files_changed_by_run"] and "verification contract" in m["manual_after"]
    from tests.pitch_brief.test_draft_to_proposal import _grant
    viewer = _grant(env, "viewer")                                                                      # may read workspaces, may not prepare a change
    assert man(viewer, env, iid).status_code in (403, 404)


def test_explicit_start_runs_impact_simulation_proposal_as_the_starter_and_approves_nothing(env, ch, monkeypatch):
    djid, iid = drafted(env, ch, monkeypatch)
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    req = env.req_client()
    m0 = man(req, env, iid).json()
    refs = [m0["edits_offered"][0]["ref"]]
    assert start(req, env, iid, m0["manifest_hash"], refs).json()["code"] == "manifest_changed"      # the hash seen WITHOUT the choice is not this manifest
    assert start(req, env, iid, m0["manifest_hash"], []).json()["code"] == "choices_needed"          # no edit is chosen for the person
    m = man(req, env, iid, accepted=",".join(refs)).json()
    assert m["ready_to_start"] is True and m["selected"]["accepted_refs"] == refs
    if m["selected"]["target_proposed_by_server"]:                                                   # confirming the proposed target by its id is the same manifest
        confirmed = man(req, env, iid, accepted=",".join(refs), target=m["selected"]["target_candidate_id"]).json()
        assert confirmed["manifest_hash"] == m["manifest_hash"] and confirmed["selected"]["target_proposed_by_server"] is False
    before = jobs_of(env, ("approval", "apply", "review"))
    r = start(req, env, iid, m["manifest_hash"], refs, target=m["selected"]["target_candidate_id"])   # as the page sends it
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    again = start(req, env, iid, m["manifest_hash"], refs)
    assert again.json()["run_id"] == rid                                                                # one run per starter + request + draft + manifest
    for _ in range(12):
        drain(env)
        st = advance(req, env, rid).json()
        advance(req, env, rid)                                                                           # a second, concurrent-style step creates nothing new
        if st["status"] != "RUNNING":
            break
    assert st["status"] == "PROPOSAL_READY", st
    prop = env.store.job_record(env.tenant, env.ws, st["proposal_job_id"])
    assert prop["payload"]["accepted_refs"] == refs and prop["payload"]["draft_job_id"] == djid and prop["execution_status"] == "COMPLETED"
    with env.store._tx() as cur:
        cur.execute("SELECT job_id, requested_by, approved_by, status FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)",
                    (env.tenant, env.ws, [x["job_id"] for x in st["stages"]]))
        rows = cur.fetchall()
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type ILIKE %s AND payload->>'job_id' = ANY(%s)",
                    (env.tenant, env.ws, "%APPROV%", [x["job_id"] for x in st["stages"]]))
        approvals = cur.fetchone()[0]
    assert len(rows) == 3 and all(r[1] == env.req and r[2] is None for r in rows), rows                  # AS the starter; nobody approved them
    assert approvals == 0
    assert jobs_of(env, ("impact", "simulation", "proposal")) == {"impact": 1, "simulation": 1, "proposal": 1}
    assert jobs_of(env, ("approval", "apply", "review")) == before                                       # no approval, review or apply created


def test_a_moved_pin_pauses_the_run_another_person_sees_nothing_and_cancel_stops_it(env, ch, monkeypatch):
    _djid, iid = drafted(env, ch, monkeypatch)
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    req = env.req_client()
    m0 = man(req, env, iid).json()
    refs = [m0["edits_offered"][0]["ref"]]
    m = man(req, env, iid, accepted=",".join(refs)).json()
    rid = start(req, env, iid, m["manifest_hash"], refs).json()["run_id"]
    other = env.admin_client()
    assert other.get(f"/v2/preparation/runs/{rid}", params={"workspace_id": env.ws}).status_code == 404     # another person's run looks missing
    assert advance(other, env, rid).status_code == 404
    drain(env)
    with env.store._tx() as cur:                                                                         # the decisions move before the next stage
        cur.execute("UPDATE v2_workspaces SET decision_epoch = decision_epoch + 1 WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, env.ws))
    st = advance(req, env, rid).json()
    assert st["status"] == "PAUSED" and st["reason"].startswith("inputs_changed")
    assert advance(req, env, rid).json()["status"] == "PAUSED"                                           # the old start is never resumed
    c = req.post(f"/v2/preparation/runs/{rid}/cancel", json={"workspace_id": env.ws}).json()
    assert c["status"] == "CANCELLED"
    assert start(req, env, iid, m["manifest_hash"], refs).json()["code"] == "manifest_changed"          # the new pins need a new manifest


# ---- admission follows the ACTUAL workspace policy (owner request 2026-10-21): the run is never a bypass ----

def _ready_manifest(req, env, iid):
    m0 = man(req, env, iid).json()
    refs = [m0["edits_offered"][0]["ref"]]
    m = man(req, env, iid, accepted=",".join(refs)).json()
    return m, refs


def test_in_the_sandbox_profile_the_run_needs_the_writing_session_and_then_follows_the_hold_rules(env, ch, monkeypatch):
    """Approval-requiring profile (sandbox): without the writing session the start itself is refused by the sandbox guard (no run, no job); with it, the
    deterministic children run (the hold applies to model executors only, `sandbox_hold.approval_hold`) and still nobody approves anything."""
    from tests.pitch_brief.conftest import sandbox_config
    _djid, iid = drafted(env, ch, monkeypatch)
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    req = env.req_client()
    m, refs = _ready_manifest(req, env, iid)
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))
    before = jobs_of(env, ("impact", "simulation", "proposal"))
    r = start(req, env, iid, m["manifest_hash"], refs)
    assert r.status_code == 409 and r.json()["code"] == "sandbox_lease_required"
    assert jobs_of(env, ("impact", "simulation", "proposal")) == before
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_preparation_runs WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, env.ws))
        assert cur.fetchone()[0] == 0
    lease = ch.admin.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire", "participants": [env.req]})
    assert lease.status_code == 200, lease.text
    rid = start(req, env, iid, m["manifest_hash"], refs).json()["run_id"]
    for _ in range(12):
        drain(env)
        st = advance(req, env, rid).json()
        if st["status"] != "RUNNING":
            break
    assert st["status"] == "PROPOSAL_READY", st
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s) AND approved_by IS NOT NULL",
                    (env.tenant, env.ws, [x["job_id"] for x in st["stages"]]))
        assert cur.fetchone()[0] == 0


def test_with_distinct_approvers_required_the_run_still_approves_nothing(env, ch, monkeypatch):
    _djid, iid = drafted(env, ch, monkeypatch)
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    env.store.set_workspace_policy(env.tenant, env.ws, require_distinct_approver=True)
    req = env.req_client()
    m, refs = _ready_manifest(req, env, iid)
    rid = start(req, env, iid, m["manifest_hash"], refs).json()["run_id"]
    for _ in range(12):
        drain(env)
        st = advance(req, env, rid).json()
        if st["status"] != "RUNNING":
            break
    assert st["status"] == "PROPOSAL_READY"
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s) AND approved_by IS NOT NULL",
                    (env.tenant, env.ws, [x["job_id"] for x in st["stages"]]))
        assert cur.fetchone()[0] == 0                                                                    # nobody approved anything; nothing to bypass


def test_a_revoked_starter_cannot_take_the_next_step(env, ch, monkeypatch):
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.pitch_brief.conftest import DSN
    _djid, iid = drafted(env, ch, monkeypatch)
    monkeypatch.setenv("RIPPLE_GUIDED_PREPARATION", "on")
    req = env.req_client()
    m, refs = _ready_manifest(req, env, iid)
    rid = start(req, env, iid, m["manifest_hash"], refs).json()["run_id"]
    drain(env)
    PostgresIdentityStore(DSN).grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, env.req, ("viewer",))   # change.prepare removed
    st = advance(req, env, rid).json()
    assert st["status"] == "PAUSED" and st["reason"].startswith("simulation_not_started"), st        # the job route re-reads the membership
    assert jobs_of(env, ("simulation",)).get("simulation", 0) == 0
