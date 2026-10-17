"""The item-by-item decisions made with a result decision are checked by the SERVER (not by the disabled button): the right, the set of items in force, the state the person reviewed, and each
decision. A "judged unrelated" is stored as the person's judgement with its basis, never as a conclusion of the system. Everything is written in the transaction of the decision."""
from __future__ import annotations

import pytest

from tests.ai8t03.conftest import login
from tests.ai8t04.conftest import approver_client, awaiting_change, change_dto, report_digest, submit_result_decision

REASON = "a different control owned by another team"


@pytest.fixture
def injected(monkeypatch):
    """Two items that may be affected by the change: a HIGH finding and a reading elsewhere. `state["extra"]` lets a test add a new one later (the state changes after the person reviewed it)."""
    from infra import v2_current_findings as CF
    real = CF.classify_for_change
    state = {"extra": []}

    def fake(*a, **k):
        out = real(*a, **k)
        out["possible_effect"] = list(out.get("possible_effect", [])) + [
            {"finding_id": "fnd-injected-1", "kind": "GAP", "reason": "missing_scope", "severity": "HIGH", "plain": "p", "todo": "t",
             "claims": [{"candidate_id": "c1", "logical_path": "x/emergency.py", "subject": "capex"}]}] + state["extra"]
        out["same_concept"] = list(out.get("same_concept", [])) + [
            {"item_id": "reading:guide", "candidate_id": "g1", "logical_path": "x/finance_desk_guide.md", "subject": "purchasing commitments", "value": 2000000, "shared_words": ["finance"], "stance": "old_value", "numbers": [2000000]}]
        return out
    monkeypatch.setattr(CF, "classify_for_change", fake)
    return state


def _body(dto, **over):
    items = [{"item_id": "fnd-injected-1", "disposition": "judged_unrelated", "reason": REASON},
             {"item_id": "reading:guide", "disposition": "unhandled"}]
    b = {"item_dispositions": items, "expected_decision_basis": dto["findings"]["decision_basis"]}
    b.update(over)
    return b


def test_the_server_decides_not_the_page(setup, monkeypatch, owner, tmp_path, injected):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, jid, store, t = change["workspace"], change["apply_job_id"], setup["store"], setup["tenant"]
    ap = approver_client(change)
    dig = report_digest(change, setup)
    dto = change_dto(ap, ws, jid)
    assert dto["findings"]["decision_basis"].startswith("dbasis-")
    send = lambda c, **kw: submit_result_decision(c, ws, jid, digest=dig, extra=kw)

    r = send(ap)                                                                                      # a bare accept (the old single tick) is refused
    assert r.status_code == 409 and "items_without_disposition" in r.text and "decision_basis_stale" in r.text
    r = send(ap, **_body(dto, item_dispositions=[_body(dto)["item_dispositions"][0]]))               # one item left out
    assert r.status_code == 409 and "items_without_disposition" in r.text
    forged = _body(dto)["item_dispositions"] + [{"item_id": "fnd-of-another-workspace", "disposition": "unhandled"}]
    r = send(ap, **_body(dto, item_dispositions=forged))                                              # an id that is not in force for this request
    assert r.status_code == 409 and "disposition_unknown_item" in r.text
    r = send(ap, **_body(dto, item_dispositions=_body(dto)["item_dispositions"] + [{"item_id": "reading:guide", "disposition": "unhandled"}]))
    assert r.status_code == 409 and "disposition_duplicate_item" in r.text
    r = send(ap, **_body(dto, expected_decision_basis="dbasis-someone-elses-review"))
    assert r.status_code == 409 and "decision_basis_stale" in r.text
    short = _body(dto)
    short["item_dispositions"][0]["reason"] = "ok"
    r = send(ap, **short)                                                                             # the length is input validation only
    assert r.status_code == 409 and "disposition_reason_required" in r.text

    asker = setup["browser"]()                                                                        # the right is checked on the server, with a valid payload
    login(asker, setup["requester"], setup["credential"])
    assert send(asker, **_body(dto)).status_code == 403

    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_change_item_dispositions WHERE tenant_id=%s AND workspace_id=%s", (t, ws))
        assert cur.fetchone()[0] == 0 and not change_dto(ap, ws, jid)["completion"].get("decision")  # nothing half-written by the refusals

    state_changes = injected["extra"]                                                                 # the state moves after the review: a new serious finding appears
    state_changes.append({"finding_id": "fnd-injected-2", "kind": "GAP", "reason": "missing_scope", "severity": "HIGH", "plain": "p", "todo": "t",
                          "claims": [{"candidate_id": "c2", "logical_path": "x/routing.py", "subject": "capex"}]})
    r = send(ap, **_body(dto))
    assert r.status_code == 409 and {"decision_basis_stale", "disposition_unknown_item"} & set(r.json()["reasons"]) or "decision_basis_stale" in r.text
    state_changes.clear()

    ok = send(ap, **_body(dto))
    assert ok.status_code == 200, ok.text
    after = change_dto(ap, ws, jid)
    assert after["completion"]["decision"]["acceptance_scope"] == "NARROW"
    assert after["status_axes"]["request_result"]["scope"] == "NARROW" and after["status_axes"]["request_result"]["unhandled"] == 1
    rows = {d["item_id"]: d for d in after["findings"]["dispositions"]}
    assert rows["fnd-injected-1"]["disposition"] == "judged_unrelated" and rows["fnd-injected-1"]["reason"] == REASON and rows["fnd-injected-1"]["actor_id"]
    assert rows["fnd-injected-1"]["basis"]["statement"] == "user_judgement" and rows["fnd-injected-1"]["basis"]["not_a_system_conclusion"] is True
    assert rows["fnd-injected-1"]["decided_by"] == "person" and rows["reading:guide"]["disposition"] == "unhandled"
    with store._tx() as cur:                                                                          # tied to THIS apply job and the reviewed state; nothing carries to another revision
        cur.execute("SELECT DISTINCT apply_job_id, decision_basis FROM v2_change_item_dispositions WHERE tenant_id=%s AND workspace_id=%s", (t, ws))
        assert cur.fetchall() == [(jid, dto["findings"]["decision_basis"])]


def test_a_critical_item_is_not_waved_through_and_a_blocker_is_not_passed_by_a_narrow_scope(setup, monkeypatch, owner, tmp_path, injected):
    injected["extra"].append({"finding_id": "fnd-critical", "kind": "GAP", "reason": "missing_scope", "severity": "CRITICAL", "plain": "p", "todo": "t",
                              "claims": [{"candidate_id": "c3", "logical_path": "x/limits.py", "subject": "capex"}]})
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, jid = change["workspace"], change["apply_job_id"]
    ap = approver_client(change)
    dig = report_digest(change, setup)
    dto = change_dto(ap, ws, jid)
    items = [{"item_id": "fnd-injected-1", "disposition": "unhandled"}, {"item_id": "reading:guide", "disposition": "unhandled"},
             {"item_id": "fnd-critical", "disposition": "accepted_limit", "reason": "accepted by the approver for this request"}]
    r = submit_result_decision(ap, ws, jid, digest=dig, extra={"item_dispositions": items, "expected_decision_basis": dto["findings"]["decision_basis"]})
    assert r.status_code == 409 and "disposition_not_allowed_critical" in r.text
    items[2] = {"item_id": "fnd-critical", "disposition": "unhandled"}
    r = submit_result_decision(ap, ws, jid, digest=dig, extra={"item_dispositions": items, "expected_decision_basis": dto["findings"]["decision_basis"]})
    assert r.status_code == 200, r.text                                                               # narrow scope is the only way through for a CRITICAL item
    assert change_dto(ap, ws, jid)["completion"]["decision"]["acceptance_scope"] == "NARROW"
