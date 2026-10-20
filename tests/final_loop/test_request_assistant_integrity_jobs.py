"""AIG-01 / AIG-02 through the real routes, admission, worker, validator and store on the throwaway PostgreSQL (contract docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md
section 6). The provider transport is the SYNTHETIC recording stub of the RQA tests (no real model; nothing here is AI-quality evidence). Covers: server-rendered authority
and refused free authority end to end with the result identity, the required enqueue -> worker equality test (independent reads, reversed enumeration, another read time),
a blocker that appears before the answer, mandatory overflow at ask and in the worker, and answers stored under older checks."""
from __future__ import annotations

import json

from core.change_intelligence.v2 import request_assistant as ra
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, ask, fact_id, own_obligation, ra_env, read, settle, thread, use  # noqa: F401  (ra_env is a fixture)

WORKER_READ_AT = "2099-12-31T23:59:59+00:00"


def holds_mine(p):
    return {"subject": "self", "fact_ref": own_obligation(p), "relation": "holds", "polarity": True, "scope": "current"}


def current_obligation(p):
    return next(l.split(" ", 1)[0] for l in p.split(chr(10)) if l.startswith("F") and "[obligation]" in l and "state current" in l)


def stage_block(p):
    return ("The stage is in the facts.", [fact_id(p, "state")], "fact")


def test_typed_authority_is_rendered_by_the_server_free_authority_is_refused_and_the_result_carries_its_identity(ra_env):
    q = ra_env
    cid, _approver = prepared(q)

    def model_fn(p):
        act = fact_id(p, "action")
        return answer(blocks=[("The change waits for the knowledge decision before any file is edited.", [fact_id(p, "state")], "interpretation"),
                              ("You can approve the knowledge change.", [act], "fact")],
                      assertions=[{"subject": "self", "fact_ref": act, "relation": "may", "polarity": True, "scope": "current"},          # false for the requester
                                  {"subject": "holder", "fact_ref": current_obligation(p), "relation": "holds", "polarity": True, "scope": "current"}])
    model = use(q, model_fn)
    t = thread(q, q.req, cid)
    ask(q, q.req, t["thread_id"], "Can I approve it, and who decides?", "key-0040-aaaa", 0)
    m = settle(q, q.req, t["thread_id"])["messages"][0]
    reasons = sorted(r["reason"] for r in m["answer"]["rejected"])
    assert model.calls == 1 and m["answer"]["status"] == "partial" and [b["kind"] for b in m["answer"]["blocks"]] == ["authority", "interpretation"]
    assert "assertion_contradicts_fact" in reasons and ("contradicts_cited_permission" in reasons or "authority_in_free_text" in reasons)
    assert "holds this step" in m["answer"]["blocks"][0]["text"] and "legacy_checks" not in m["answer"]
    assert (m["answer"]["checks_version"], m["answer"]["answer_schema"], m["answer"]["locale"], m["answer"]["context_version"]) == \
        (ra.CHECKS_VERSION, ra.ANSWER_SCHEMA, "en", ra.CONTEXT_VERSION)
    assert "authority_assertions" in model.prompts[0]


def test_enqueue_and_worker_select_the_same_pack_under_reversed_enumeration_and_another_read_time(ra_env, monkeypatch):
    """AIG-02 required equality test: the pack at ask and the pack rebuilt by the worker come from two independent reads; the worker's read enumerates every list in
    reverse and has another read time. Same selected facts, same citation mapping, same manifest hash: the question is answered (not `context_changed`)."""
    from infra.adapters import v2_business_review, v2_request_tasks
    from infra.adapters import v2_request_assistant as vra
    q = ra_env
    cid, approver = prepared(q)
    packs, flip = [], {"on": False}
    real_build, real_tv, real_review = vra.build_context, v2_request_tasks.task_view, v2_business_review.review_view

    def build(*a, **k):
        packs.append(real_build(*a, **k))
        return packs[-1]

    def tv(*a, **k):
        v = real_tv(*a, **k)
        if not flip["on"]:
            return v
        return {**v, "as_of": WORKER_READ_AT, **{key: list(reversed(v.get(key) or [])) for key in ("obligations", "permitted_actions", "waiting_on", "blockers")}}

    def review(*a, **k):
        v = real_review(*a, **k)
        return v if v is None or not flip["on"] else {**v, "decisions": list(reversed(v["decisions"]))}
    monkeypatch.setattr(vra, "build_context", build)
    monkeypatch.setattr(v2_request_tasks, "task_view", tv)
    monkeypatch.setattr(v2_business_review, "review_view", review)
    model = use(q, lambda p: answer(blocks=[stage_block(p)], assertions=[holds_mine(p)]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "What next?", "key-0041-bbbb", 0)
    at_ask = packs[-1]
    assert sum(1 for f in at_ask["facts"] if f["kind"] in ("obligation", "action")) >= 2      # the reversal is not trivial
    flip["on"] = True
    m = settle(q, approver, t["thread_id"])["messages"][0]
    at_worker = next(p for p in reversed(packs) if p["as_of"] == WORKER_READ_AT)
    assert [(f["id"], f["key"], f["text"]) for f in at_worker["facts"]] == [(f["id"], f["key"], f["text"]) for f in at_ask["facts"]]
    assert at_worker["manifest_hash"] == at_ask["manifest_hash"] and at_worker["coverage"] == at_ask["coverage"]
    assert model.calls == 1 and m["state"] == "answered" and m["answer"]["status"] == "answered"
    assert {r: c["key"] for r, c in m["citations"].items()} == {r: next(f["key"] for f in at_ask["facts"] if f["id"] == r) for r in m["citations"]}


def test_a_blocker_that_appears_between_ask_and_answer_stops_before_the_provider(ra_env, monkeypatch):
    from infra.adapters import v2_request_tasks
    q = ra_env
    cid, approver = prepared(q)
    real_tv, extra = v2_request_tasks.task_view, {"on": False}

    def tv(*a, **k):
        v = real_tv(*a, **k)
        return {**v, "blockers": list(v.get("blockers") or []) + [{"code": "late_blocker", "text": "A late blocker."}]} if extra["on"] else v
    monkeypatch.setattr(v2_request_tasks, "task_view", tv)
    model = use(q, lambda p: answer(blocks=[stage_block(p)]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "What next?", "key-0042-cccc", 0)
    extra["on"] = True
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert model.calls == 0 and m["state"] == "unavailable" and "context_changed" in (m["answer"].get("reason") or "")


def _message_count(q, tid):
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (q.env.tenant, q.env.ws, tid))
        return cur.fetchone()[0]


def test_mandatory_overflow_is_refused_at_ask_and_in_the_worker_before_any_provider_call(ra_env, monkeypatch):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[stage_block(p)]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "Queued before the overflow", "key-0043-dddd", 0)
    monkeypatch.setattr(ra, "MAX_FACTS", 2)                                                    # far fewer than the request's mandatory facts
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert model.calls == 0 and m["state"] == "unavailable" and "context_overflow" in (m["answer"].get("reason") or "")
    st = read(q, approver, t["thread_id"])
    assert st.status_code == 200 and st.json()["context"]["coverage"]["overflow"] == "mandatory_facts_exceed_bounds"    # history stays readable
    before = _message_count(q, t["thread_id"])
    r = ask(q, approver, t["thread_id"], "Another question", "key-0044-eeee", 1)
    assert r.status_code == 409 and r.json()["code"] == "context_overflow" and model.calls == 0
    assert _message_count(q, t["thread_id"]) == before                                         # no message, no job


def test_an_answer_stored_under_older_checks_is_shown_with_a_note_and_without_free_authority_and_is_not_rewritten(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[stage_block(p)]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "What next?", "key-0045-ffff", 0)
    m = settle(q, approver, t["thread_id"])["messages"][0]
    old = {**m["answer"], "checks_version": "request-assistant-checks/3", "blocks": m["answer"]["blocks"] + [{"text": "Yes, the next step is yours.", "fact_refs": [], "kind": "fact"}]}
    old.pop("answer_schema", None)
    with q.env.store._tx() as cur:
        cur.execute("UPDATE v2_request_messages SET answer=%s WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s", (json.dumps(old), q.env.tenant, q.env.ws, m["message_id"]))
    calls = model.calls
    shown = read(q, approver, t["thread_id"]).json()["messages"][0]["answer"]
    assert shown["legacy_checks"] is True and shown["legacy_withheld"] == 1 and [b["text"] for b in shown["blocks"]] == ["The stage is in the facts."]
    with q.env.store._tx() as cur:
        cur.execute("SELECT answer FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s", (q.env.tenant, q.env.ws, m["message_id"]))
        stored = cur.fetchone()[0]
    assert len((stored if isinstance(stored, dict) else json.loads(stored))["blocks"]) == 2 and model.calls == calls    # not rewritten, nothing regenerated on read


def test_every_shown_impact_rule_is_its_own_fact_even_when_rules_share_a_link_kind(ra_env):
    """AIG-02 regression found 2026-10-20: impact facts were keyed by the link KIND ("direct"), so the pack's one-fact-per-key rule collapsed several affected
    rules into one. The key is now the logical rule (`ref_key`)."""
    from infra.adapters import v2_business_review
    from infra.adapters import v2_request_assistant as vra
    from infra.v2_business_routes import _review_payload
    q = ra_env
    cid, _approver = prepared(q)
    auth = {"tenant_id": q.env.tenant, "workspace_id": q.env.ws, "account_id": q.env.admin, "roles": ("admin",)}
    pack = vra.build_context(q.env.store, q.env.tenant, q.env.ws, "business_change", cid, auth)
    view = v2_business_review.review_view(q.env.store, q.env.tenant, q.env.ws, cid, account_id=q.env.admin, roles=("admin",))
    rules = (_review_payload(view, mode="business", can_evidence=True, change_id=cid).get("impact") or {}).get("affected_rules") or []
    impact = [f for f in pack["facts"] if f["kind"] == "impact"]
    assert len({r["link"] for r in rules}) < len(rules), "precondition: at least two shown rules share a link kind"
    assert rules and len(impact) == min(len(rules), vra.IMPACT_LIMIT)
    assert len({f["key"] for f in impact}) == len(impact) and all(f["key"].startswith("impact:rk-") for f in impact)
