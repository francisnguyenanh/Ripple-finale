"""EUI-03 citation -> source through the real route on the throwaway PostgreSQL: the source of a cited impact fact is resolved by the SERVER from the pack the
answer was built on and rechecked at read time (own thread, same access, cited by this answer, evidence.read). No model call on click; a non-source fact, a
reader without evidence rights, an uncited fact and a changed request get honest reasons instead of a location. Synthetic stub model."""
from __future__ import annotations

import json

from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, ask, fact_id, ra_env, settle, thread, use  # noqa: F401  (ra_env is a fixture)


def source(q, client, tid, mid, ref, workspace=None):
    return client.get(f"/v2/request-assistant/threads/{tid}/messages/{mid}/citations/{ref}/source", params={"workspace_id": workspace or q.env.ws})


def cite_impact_and_state(p):
    return answer(blocks=[("The change reaches an affected rule.", [fact_id(p, "impact")], "fact"), ("The stage is in the facts.", [fact_id(p, "state")], "fact")])


def asked(q, client, cid, key):
    t = thread(q, client, cid)
    ask(q, client, t["thread_id"], "What does this change reach?", key, 0)
    m = settle(q, client, t["thread_id"])["messages"][0]
    assert m["state"] == "answered", m
    refs = {c["kind"]: r for r, c in m["citations"].items()}
    return t["thread_id"], m["message_id"], refs


def test_an_evidence_reader_gets_the_server_resolved_source_and_no_model_call(ra_env):
    q = ra_env
    cid, approver = prepared(q)                                                                    # change approver + reviewer: holds evidence.read
    model = use(q, cite_impact_and_state)
    tid, mid, refs = asked(q, approver, cid, "key-0060-aaaa")
    calls = model.calls
    r = source(q, approver, tid, mid, refs["impact"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True and body["kind"] == "impact" and body["sources"]
    for s in body["sources"]:
        assert s["logical_path"] and s["state"] in ("current", "changed_since", "absent_now") and s["revision_id"]
        if s["logical_path"].endswith((".yaml", ".yml")) and s["locator"] and s["state"] != "absent_now":
            assert s["excerpt"] and s["excerpt"].startswith(s["locator"] + ": ")                     # the stored value at that key, read from the pinned revision
    assert any(s["excerpt"] for s in body["sources"]) or not any(s["logical_path"].endswith(".yaml") for s in body["sources"])
    assert model.calls == calls                                                                     # a click never calls a model
    local = source(q, approver, tid, mid, refs["state"]).json()
    assert local == {"fact_ref": refs["state"], "available": False, "kind": "state", "reason": "no_source_locator"}   # no locator is made up
    uncited = next(f"F{i}" for i in range(1, 60) if f"F{i}" not in refs.values())
    assert source(q, approver, tid, mid, uncited).json()["reason"] == "not_cited_by_this_answer"


def test_without_evidence_rights_no_path_or_excerpt_and_other_people_and_a_changed_request_get_nothing(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    use(q, cite_impact_and_state)
    tid, mid, refs = asked(q, q.req, cid, "key-0061-bbbb")                                         # the requester: no evidence.read
    r = source(q, q.req, tid, mid, refs["impact"])
    assert r.status_code == 200 and r.json() == {"fact_ref": refs["impact"], "available": False, "kind": "impact", "reason": "needs_evidence_rights"}
    assert ".yaml" not in r.text and "harbor" not in r.text.lower()                                 # no path, no excerpt
    other = login(q, f"appr-cs-{q.env.nonce}", ("change_approver", "reviewer"))
    assert source(q, other, tid, mid, refs["impact"]).status_code == 404                             # another person's thread looks missing
    assert source(q, q.req, tid, mid, refs["impact"], workspace="ws-of-someone-else").status_code in (401, 403, 404)
    atid, amid, arefs = asked(q, approver, cid, "key-0062-cccc")
    rv = approver.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "technical"}).json()
    d = approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": "request_changes",
                                                                          "reason": "Please name the scope.", "binding": rv["binding"]})
    assert d.status_code == 200, d.text
    stale = source(q, approver, atid, amid, arefs["impact"]).json()
    assert stale["available"] is False and stale["reason"] == "stale_context" and "sources" not in json.dumps(stale)


def test_r12_the_server_computed_range_is_cited_in_the_proposal_mood_and_its_source_is_the_rule_it_was_computed_from(ra_env):
    """R12 (context/4): the computed range fact ("rule_change:draft:NNN:range") is kept when written as a proposal and its source is the same stored rule as the
    draft's Before / After fact (it was computed from that rule's conditions); context/3 had no such fact, and a key ending in ":range" resolved to nothing."""
    q = ra_env
    cid, approver = prepared(q)

    def cite_range(p):
        rng = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[rule_change] Computed by the server for the rule" in l)
        draft = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[rule_change] Before:" in l)
        return answer(blocks=[("If approved, requests from 1,000,000 up to, not including, 1,500,000 JPY would no longer need manager review.", [rng], "interpretation"),
                              ("The draft would raise the threshold.", [draft], "fact")])
    use(q, cite_range)
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "Which requests would this change affect?", "key-r12-0101", 0)
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert m["state"] == "answered" and m["answer"]["rejected"] == [], m["answer"]
    by_text = {c["text"][:40]: r for r, c in m["citations"].items()}
    rng = next(r for t_, r in by_text.items() if t_.startswith("Computed by the server for the rule"))
    draft = next(r for t_, r in by_text.items() if t_.startswith("Before:"))
    a, b = source(q, approver, t["thread_id"], m["message_id"], rng).json(), source(q, approver, t["thread_id"], m["message_id"], draft).json()
    assert a["available"] is True and a["kind"] == "rule_change" and a["sources"] == b["sources"] and a["sources"]
