"""Two cloud findings (2026-10-12), through the real callers:
1. a member WITHOUT `evidence.read` (requester, approver) must not receive raw source text in ANY response about a draft: the stored output keeps it, the response does not;
   a member WITH it (admin, reviewer) still does. Not a CSS matter: the strings are searched for in the JSON the server sends.
2. the words about state come from the server: the model's free sentence is never shown as the assistant turn, older stored turns are re-rendered on read,
   and the sentence after a confirmation says what was saved and what is NOT done."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from infra import chat_safety, judge
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.worker import change_draft
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_change_draft import Model, _res, run
from tests.pitch_brief.test_wording_intent_unresolved import _revise
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal
from tests.pitch_brief.test_free_text_purposes import _draft_with_question, _run_purpose
from tests.pitch_brief.test_intent_chat import settle, use  # noqa: F401


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _member(env, name, role):
    ids = PostgresIdentityStore(env.dsn)
    acct = f"{name}-{env.nonce}"
    ids.create_account(acct, f"cred-{name}-{env.nonce}")
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, acct, (role,))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": f"cred-{name}-{env.nonce}"}).status_code == 200
    return c


def _raw_strings(draft_result: dict) -> list[str]:
    out = [u["text"] for u in draft_result.get("unaddressed") or [] if u.get("text")]
    for x in draft_result.get("items") or []:
        q = (x.get("citation") or {}).get("quote")
        if q:
            out.append(q)
    return [s for s in out if len(s) > 12]


def _strings(node):
    """Every string value of a DECODED response (a raw passage with quotes or line breaks would be escaped in the JSON text: searching the text would miss it)."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _leaks(body, raw):
    return [r for r in raw if any(r in v for v in _strings(body))]


def _responses(env, client, draft, proposal=None):
    ws = {"workspace_id": env.ws}
    out = {"draft_job": client.get(f"/v2/jobs/{draft}", params=ws).json(),
           "gaps": client.get("/v2/change/gaps", params={**ws, "draft_job_id": draft}).json()}
    if proposal:
        out["proposal_job"] = client.get(f"/v2/jobs/{proposal}", params=ws).json()
    return out


def test_members_without_evidence_read_receive_no_raw_source_text_in_draft_gaps_or_proposal(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    admin = ch.admin
    raw = _raw_strings(_res(job_body(admin, env, c.draft)))
    assert raw, "the fixture draft must carry source passages for this test to mean anything"
    seen_by_admin = _responses(env, admin, c.draft, c.proposal)
    assert any(_leaks(body, raw) for body in seen_by_admin.values())                        # positive control: with evidence.read the text is there
    reviewer = _member(env, "rev", "reviewer")
    seen_by_reviewer = _responses(env, reviewer, c.draft, c.proposal)
    assert any(_leaks(body, raw) for body in seen_by_reviewer.values())
    for who, client in (("requester", env.req_client()), ("approver", _member(env, "apv", "change_approver"))):
        for name, body in _responses(env, client, c.draft, c.proposal).items():
            assert not _leaks(body, raw), f"{who} received raw source text in {name}: {_leaks(body, raw)[0][:60]!r}"
    # the stored draft is untouched: only the response changes
    assert _raw_strings(_res(job_body(admin, env, c.draft))) == raw
    # file, location and the fact that nobody classified it remain for the member without the right
    g = env.req_client().get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": c.draft}).json()
    hidden = [x for x in g["gaps"] if x["kind"] == "unaddressed"]
    assert hidden and all(x["text_redacted"] and x["logical_path"] and "not classified by the model" in x["text"] for x in hidden)
    d = env.req_client().get(f"/v2/jobs/{c.draft}", params={"workspace_id": env.ws}).json()["execution_result"]
    assert all(u["text"] == "" and u["text_redacted"] and u["logical_path"] and u["location"] for u in d["unaddressed"])


def test_carried_items_of_a_revise_are_redacted_the_same_way(env, ch, monkeypatch):
    def first(obj, prompt):
        obj["questions_for_human"] = ["Q?"]
        for it in obj["items"]:
            if it["action"] == "manual":
                it["action"] = "ambiguity"
    d1, body = run(env, ch, monkeypatch, Model(mutate=first))
    r1 = _res(body)
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit."}).status_code == 200

    def second(obj, prompt):
        obj["questions_for_human"] = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"] = "exclude"
        if obj["items"]:
            obj["items"].pop()                                  # an omitted item becomes an unaddressed (carried) entry
    model = Model(mutate=second)
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    d2, r = create_job(env.req_client(), env, "change.draft", {"intent_id": r1["intent"]["intent_id"], "extraction_job_ids": list(ch.jobs.values()), "revises_draft_job_id": d1})
    assert r.status_code == 200
    drain(env)
    res2 = _res(job_body(ch.admin, env, d2))
    raw = _raw_strings(res2)
    assert any(u.get("carried_from") for u in res2["unaddressed"]) or res2["carried_unresolved"]
    for name, body in _responses(env, env.req_client(), d2).items():
        assert not _leaks(body, raw), name


# ------------------------------------------------------------------------------------------------ state words
BENIGN = "Which file do you mean? I will look at it."


def test_the_models_free_sentence_is_never_the_assistant_turn_and_older_turns_are_rerendered_on_read(env, ch, use, monkeypatch):
    jid, key = _draft_with_question(env, ch, monkeypatch)
    adm = env.admin_client()
    reply = {"status": "READY", "message": BENIGN, "questions": [], "assumptions": [],
             "fields": {"answer": "Leave the other file out.", "kind": "decision", "wish_or_fact": "wish", "rationale": None, "quote": "x"}}
    _m, cid, got = _run_purpose(env, ch, use, "gap_reply", "Keep it to approval.yaml.", reply, context={"draft_job_id": jid, "gap_key": key}, client=adm)
    turn = [t for t in got["turns"] if t["role"] == "assistant"][-1]["text"]
    assert turn == chat_safety.preview_turn_text(got["understanding"]) and BENIGN not in turn and "Nothing is recorded until you confirm" in turn
    # an assistant turn stored by the OLD rule (the model's own sentence) is rendered from the structured understanding when read
    seq = [t["seq"] for t in got["turns"] if t["role"] == "assistant"][-1]
    with env.store._tx() as cur:
        cur.execute("UPDATE v2_chat_turns SET text=%s WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s AND seq=%s",
                    ("Proposed understanding (preview). Nothing is recorded until you confirm it. AI wording: Recorded the instruction to leave the file unchanged.", env.tenant, env.ws, cid, seq))
    again = adm.get(f"/v2/chat/{cid}", params={"workspace_id": env.ws}).json()
    old = [t for t in again["turns"] if t["role"] == "assistant"][-1]["text"]
    assert "Recorded the instruction" not in old and old == turn
    with env.store._tx() as cur:
        cur.execute("SELECT text FROM v2_chat_turns WHERE tenant_id=%s AND workspace_id=%s AND conversation_id=%s AND seq=%s", (env.tenant, env.ws, cid, seq))
        assert "Recorded the instruction" in cur.fetchone()[0]       # the stored data is untouched; only the response is re-rendered


def test_the_sentence_after_a_confirmation_says_what_was_saved_and_what_is_not_done(env, ch, use, monkeypatch):
    jid, key = _draft_with_question(env, ch, monkeypatch)
    adm = env.admin_client()
    reply = {"status": "READY", "message": "ok", "questions": [], "assumptions": [],
             "fields": {"answer": "Leave it.", "kind": "decision", "wish_or_fact": "wish", "rationale": None, "quote": "x"}}
    _m, cid, got = _run_purpose(env, ch, use, "gap_reply", "Keep it to approval.yaml.", reply, context={"draft_job_id": jid, "gap_key": key}, client=adm)
    c = adm.post(f"/v2/chat/{cid}/confirm", json={"workspace_id": env.ws, "version": got["version"], "acknowledged": True}).json()
    note = [t["text"] for t in c["turns"] if t["role"] == "system"][-1]
    assert "At this step the server saved: your confirmation of this preview" in note
    assert "Not done at this step: the decision is not recorded and no source file was changed" in note and "Nothing has been done yet" not in note
    # a failed or refused record never reads as recorded; after a real record the server says so, with the decision id
    assert adm.post(f"/v2/chat/{cid}/executed", json={"workspace_id": env.ws, "ref": "decision"}).status_code == 409
    assert not any("Recorded by the server" in t["text"] for t in adm.get(f"/v2/chat/{cid}", params={"workspace_id": env.ws}).json()["turns"])
    r = adm.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": jid, "gap_key": key, "chat_id": cid})
    assert r.status_code == 200
    done = adm.post(f"/v2/chat/{cid}/executed", json={"workspace_id": env.ws, "ref": r.json()["decision_id"]}).json()
    assert any(r.json()["decision_id"] in t["text"] and t["text"].startswith("Recorded by the server") for t in done["turns"])
    # the same call by someone else, or for another point, is refused (no decision of theirs)
    other = env.req_client()
    assert other.post(f"/v2/chat/{cid}/executed", json={"workspace_id": env.ws, "ref": r.json()["decision_id"]}).status_code in (403, 404, 409)


def _first_unaddressed(env, ch, monkeypatch):
    d1, body = run(env, ch, monkeypatch, Model())
    r1 = _res(body)
    raw = _raw_strings(r1)
    un = next(g for g in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if g["kind"] == "unaddressed")
    return d1, r1, raw, un


def test_a_decision_on_an_unaddressed_point_never_copies_the_source_passage(env, ch, monkeypatch):
    d1, r1, raw, un = _first_unaddressed(env, ch, monkeypatch)
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": un["key"], "answer": "Leave it."}).status_code == 200
    with env.store._tx() as cur:
        cur.execute("SELECT scope FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s AND target=%s", (env.tenant, env.ws, un["gap_id"]))
        scope = cur.fetchone()[0]
    scope = scope if isinstance(scope, dict) else json.loads(scope)
    assert "not classified by the model" in scope["question"] and not any(r in scope["question"] for r in raw)


def test_a_decision_stored_by_the_old_rule_is_scrubbed_on_read_for_members_without_evidence_read(env, ch, monkeypatch):
    d1, r1, raw, un = _first_unaddressed(env, ch, monkeypatch)
    passage = un["text"].split("not classified by the model - ", 1)[1]
    assert passage
    head = env.store.head(env.tenant, env.ws)
    env.store.record_human_decision(env.tenant, env.ws, actor_id=env.admin, current_permission="knowledge.confirm", target=un["gap_id"], action="confirm",
                                    expected_version=head.version, idempotency_key="legacy-q", rationale=None,
                                    scope={"kind": "draft_gap_answer", "intent_id": r1["intent"]["intent_id"], "draft_job_id": d1, "key": un["key"],
                                           "question": un["text"][:300], "answer": "Legacy."})
    sig = [passage[:40]]
    # positive control: the admin (evidence.read) still receives the decision's question as stored; the stored decision itself is untouched
    assert any(_leaks(b, sig) for b in _responses(env, ch.admin, d1).values())
    for name, b in _responses(env, env.req_client(), d1).items():
        assert not _leaks(b, sig), name
    # and in the draft that revises it (clarifications used)
    d2, r2, _m = _revise(env, ch, monkeypatch, r1["intent"]["intent_id"], d1, lambda obj, prompt: None)
    assert any(_leaks(b, sig) for b in _responses(env, ch.admin, d2).values())
    for name, b in _responses(env, env.req_client(), d2).items():
        assert not _leaks(b, sig), name
