"""SYN-04: the per-axis journey of a business change through the real routes (understanding STUB, store, worker). What this proves: the axes stay separate
(knowledge committed while the files still show the old value; source applied is not implemented; a document that was never requested blocks nothing; a
completed job, an answered question or an existing document is never completion), the next actor/action is stated at every step, reject and request_changes
have distinct semantics, and only a read-back accepted by a person makes the change fully implemented (behaviour stays untested). Not human acceptance."""
from __future__ import annotations

from core.change_intelligence.v2 import business_journey as bj
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj, understand
from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_chg04_review import decide, prepared, review
from tests.final_loop.test_chg05_commit import TARGET, approved, commit
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno05_accept import accept
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn02_reconcile import analyse
from tests.final_loop.test_syn03_source_apply import choose, committed_and_file_writes_approved
from tests.pitch_brief.conftest import drain


def journey(q, client, cid):
    r = client.get(f"/v2/business/changes/{cid}/journey", params={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def states(j):
    return {s["key"]: s["state"] for s in j["steps"]}


def test_the_axes_stay_separate_from_understanding_to_a_verified_read_back(qa_env):
    q = qa_env
    got, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]))
    cid = got["change_id"]
    j = journey(q, q.req, cid)
    assert states(j)["understand"] == "done" and states(j)["confirm"] == "current" and j["next"]["step"] == "confirm" and j["knowledge_only_complete"] is False and j["fully_implemented"] is False
    assert j["documents"] == {"freshness": "not_requested", "blocks": False, "statement": j["documents"]["statement"]}
    cid, approver = prepared(q)                                                                              # a confirmed request (new change)
    j = journey(q, q.req, cid)
    assert states(j)["confirm"] == "done" and states(j)["review"] == "current" and j["next"]["step"] == "review" and "approver" in j["next"]["who"]
    assert states(j)["source_sync"] == "todo" and states(j)["code_sync"] in ("waiting", "not_required") and j["scope"] == "FULL"
    b = review(q, approver, cid).json()["binding"]
    assert decide(q, approver, cid, b).status_code == 200
    j = journey(q, approver, cid)
    assert states(j)["review"] == "done" and states(j)["commit"] == "current" and j["next"]["step"] == "commit" and "knowledge owner" in j["next"]["who"]
    assert decide(q, approver, cid, b, kind="file_writes").status_code == 200
    assert commit(q, q.admin, cid, b).status_code == 200
    drain(q.env)
    j = journey(q, q.admin, cid)
    s = states(j)
    assert j["knowledge_only_complete"] is True and j["fully_implemented"] is False and s["commit"] == "done" and s["source_sync"] == "current" and j["next"]["step"] == "source_sync"
    assert "Choose" in j["next"]["what"] and s["readback"] == "waiting" and s["behaviour"] == "todo" and s["user_result"] == "todo"
    assert j["labels"][TARGET] == {"accepted_target": 1500000, "observed_current": 1000000}                 # accepted target vs what the sources say
    assert "labels" in journey(q, q.req, cid) and journey(q, q.req, cid)["labels"] == {}                     # values are evidence
    assert j["documents"]["freshness"] == "not_requested" and j["documents"]["blocks"] is False
    pack = q.admin.post("/v2/business/context", json={"workspace_id": q.env.ws, "query": "standard manager approval threshold purchases", "view": "target", "semantic": False}).json()
    assert any("1500000" in i["text"] for i in pack["items"])                                                # the new rule answers now, files unsynced
    r = choose(q, q.admin, cid, "ai", b)
    assert r.status_code == 200, r.text
    drain(q.env)
    j = journey(q, q.admin, cid)
    s = states(j)
    assert s["source_sync"] == "partial" and j["fully_implemented"] is False and s["readback"] == "current" and j["next"]["step"] == "readback"   # published is not implemented
    assert "job" not in j["statement"].lower() or "not completion" in j["statement"]
    out = analyse(q)
    assert out["derived"]
    j = journey(q, q.admin, cid)
    assert states(j)["readback"] == "partial" and j["fully_implemented"] is False                            # read back, waiting for a person to accept
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    drain(q.env)
    j = journey(q, q.admin, cid)
    s = states(j)
    assert s["readback"] == "done" and j["labels"][TARGET]["observed_current"] == 1500000
    assert j["fully_implemented"] == (s["code_sync"] in ("done", "not_required")) and j["behaviour_verified"] is False and s["behaviour"] == "todo"
    assert "behaviour" in j["statement"].lower() or "Not fully implemented" in j["statement"]


def test_reject_and_request_changes_have_distinct_semantics_and_a_failed_apply_keeps_the_pending_axis(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    b = review(q, approver, cid).json()["binding"]
    assert decide(q, approver, cid, b, action="request_changes", reason="Say which processes.").status_code == 200
    j = journey(q, approver, cid)
    assert states(j)["review"] == "redo" and j["next"]["step"] == "review" and "restates" in j["next"]["why"] and j["rejected"] is False
    text = "Raise the standard manager approval threshold from 1,000,000 to 1,700,000 JPY."
    cid2, _ = confirmed(q, text, [dict(OP_VALUE, desired={"value": 1700000, "unit": "JPY"}, user_quote=text.rstrip("."))])           # a second request, same approver
    b2 = review(q, approver, cid2).json()["binding"]
    assert decide(q, approver, cid2, b2, action="reject", reason="Not wanted.").status_code == 200
    j = journey(q, approver, cid2)
    s = states(j)
    assert s["review"] == "rejected" and j["rejected"] is True and j["next"] is None and s["commit"] == "not_required" and s["source_sync"] == "not_required"
    assert j["knowledge_only_complete"] is False and j["fully_implemented"] is False


def test_pure_model_never_calls_a_completed_job_or_a_document_completion():
    base = {"understanding": {"status": "done", "ready": True, "can_confirm": True}, "confirmed": True, "knowledge_decision": {"action": "approve", "by": "a"}, "committed": {"business_revision": 2},
            "completion": {"knowledge_result": "committed", "source_sync": "pending", "code_sync": "pending", "behavioral_verification": "not_tested"},
            "source": {"automatic_edit_available": True, "file_writes_approved": True, "job": {"status": "COMPLETED"}, "manual_count": 1}, "documents": {"freshness": "available"},
            "readback": {"target_statuses": {"r-target": "pending"}, "pending_snapshot": False}, "code": {"supported": 1, "uncertain": 0, "unresolved": 0}}
    j = bj.build_journey(base)
    assert j["knowledge_only_complete"] and not j["fully_implemented"] and states(j)["source_sync"] == "partial" and j["documents"]["blocks"] is False
    verified = dict(base, readback={"target_statuses": {"r-target": "verified"}, "pending_snapshot": False})
    assert bj.build_journey(verified)["fully_implemented"] is False                                          # code still pending
    done_code = dict(verified, completion=dict(base["completion"], code_sync="not_required"))
    j = bj.build_journey(done_code)
    assert j["fully_implemented"] is True and j["behaviour_verified"] is False and states(j)["behaviour"] == "todo"
    divergent = dict(base, readback={"target_statuses": {"r-target": "divergent"}, "pending_snapshot": False})
    assert states(bj.build_journey(divergent))["readback"] == "blocked"
    narrow = dict(base, completion=dict(base["completion"], source_sync="not_required", code_sync="not_required"))
    assert bj.build_journey(narrow)["scope"] == "NARROW"
    clarify = {"understanding": {"status": "done", "ready": False, "can_confirm": False, "clarify": True}}
    j = bj.build_journey(clarify)
    assert states(j)["understand"] == "blocked" and j["next"]["step"] == "understand"
