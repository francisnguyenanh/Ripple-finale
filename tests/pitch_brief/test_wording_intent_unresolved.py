"""Three defects found on the cloud (2026-10-12), reproduced through the REAL callers (routes, worker, store) with a stub provider:
1. the assistant said "recorded" BEFORE the person confirmed and before the server stored anything -> the server's state decides the words;
2. the person's typed words and the files they named were replaced by an AI paraphrase -> the verbatim text, the exact files and the source revision are stored;
3. a revise silently resolved open items because the model excluded / left them out -> the server carries them forward, identified by what they are (artifact + location)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infra import chat_safety
from infra.worker import change_draft
from infra.worker.change_draft import CARRIED_MISSING, CARRIED_MODEL_EXCLUSION, carry_unresolved
from infra.worker.change_draft_binding import resolve_draft_edits
from infra.worker.typed_scope import files_in_text, known_files
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_change_draft import Model, _res, judge, run
from tests.pitch_brief.test_free_text_purposes import _draft_with_question, _run_purpose
from tests.pitch_brief.test_intent_chat import api, settle, use  # noqa: F401


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


TYPED = ("Keep this change limited to approval.yaml. Do not update approval_matrix.xlsx or workflow.yaml in this request. "
         "Keep those inconsistencies visible as unresolved.")


# ------------------------------------------------------------------------------------------------ 1. words follow the server state
@pytest.mark.parametrize("claim", ["Your decision has been recorded.", "I have recorded your answer.", "Saved - your feedback is stored.",
                                   "Your comment was logged and submitted.", "The note has been added.",
                                   "Recorded the instruction to leave the workflow settings unchanged.", "Saving your reply as a decision.", "Noted: only approval.yaml."])
def test_a_model_sentence_can_never_state_that_something_was_recorded(claim):
    text = chat_safety.server_turn_text("READY", claim)
    assert text == chat_safety.PREVIEW_LINE["READY"]            # the claim is dropped; only the server's statement of state is shown
    assert "recorded until you confirm" in text


def test_a_plain_model_sentence_is_shown_only_as_ai_wording_after_the_state_line():
    text = chat_safety.server_turn_text("CLARIFY", "Which file do you mean?")
    assert text.startswith(chat_safety.PREVIEW_LINE["CLARIFY"]) and text.endswith("AI wording: Which file do you mean?")


def test_gap_reply_preview_turn_says_nothing_is_recorded_until_the_server_stores_it(env, ch, use, monkeypatch):
    jid, key = _draft_with_question(env, ch, monkeypatch)
    adm = env.admin_client()
    reply = {"status": "READY", "message": "Your decision has been recorded.", "questions": [], "assumptions": [],
             "fields": {"answer": "Do not update the approval matrix spreadsheet or the workflow configuration.", "kind": "decision", "wish_or_fact": "wish",
                        "rationale": None, "quote": "Do not update approval_matrix.xlsx or workflow.yaml"}}
    _m, cid, got = _run_purpose(env, ch, use, "gap_reply", TYPED, reply, context={"draft_job_id": jid, "gap_key": key}, client=adm)
    assistant = [t for t in got["turns"] if t["role"] == "assistant"][-1]
    assert assistant["text"] == chat_safety.preview_turn_text(got["understanding"]) and "has been recorded" not in assistant["text"]
    assert "has been recorded" not in got["understanding"]["ai_message"]                    # the resolver already replaced the claim (now also for "recorded" and its kin)
    assert any(n["code"] == "MODEL_CLAIMED_COMPLETION" for n in got["understanding"]["safety"])
    # confirming the preview is not recording either
    c = adm.post(f"/v2/chat/{cid}/confirm", json={"workspace_id": env.ws, "version": got["version"], "acknowledged": True}).json()
    assert c["outcome"]["kind"] == "decision" and not any("Recorded by the server" in t["text"] for t in c["turns"])
    # "executed" with a reference the server does not hold is refused: the page cannot make the chat say "recorded"
    bad = adm.post(f"/v2/chat/{cid}/executed", json={"workspace_id": env.ws, "ref": "decision"})
    assert bad.status_code == 409 and "nothing is marked as recorded" in bad.text
    # record the decision for real, then the server (and only then) says so
    r = adm.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": jid, "gap_key": key, "chat_id": cid, "rationale": "verification"})
    assert r.status_code == 200, r.text
    done = adm.post(f"/v2/chat/{cid}/executed", json={"workspace_id": env.ws, "ref": r.json()["decision_id"]})
    assert done.status_code == 200 and any("Recorded by the server" in t["text"] for t in done.json()["turns"])


@pytest.mark.parametrize("purpose,context_fn", [("knowledge_qa", None), ("whatif", None)])
def test_the_same_rule_holds_for_the_other_purposes(env, ch, use, purpose, context_fn):
    fields = {"knowledge_qa": {"question": "What is the CAPEX threshold?", "files": []},
              "whatif": {"rule": "r1", "value": 900000, "case": "route", "quote": "900000"}}[purpose]
    reply = {"status": "CLARIFY", "message": "Your question has been saved and submitted.", "questions": ["Which files?"], "assumptions": [], "fields": fields}
    _m, cid, got = _run_purpose(env, ch, use, purpose, "What is the CAPEX threshold? 900000", reply, client=env.admin_client())
    text = [t for t in got["turns"] if t["role"] == "assistant"][-1]["text"]
    assert text == chat_safety.preview_turn_text(got["understanding"])


# ------------------------------------------------------------------------------------------------ 2. the person's words and files survive
def test_typed_files_are_found_in_the_persons_own_words_with_their_role():
    known = known_files([{"logical_path": "harbor_demo/config/approval.yaml", "artifact_id": "a1"},
                         {"logical_path": "harbor_demo/config/workflow.yaml", "artifact_id": "a2"},
                         {"logical_path": "harbor_demo/data/approval_matrix.xlsx", "artifact_id": "a3"},
                         {"logical_path": "harbor_demo/docs/other.md", "artifact_id": "a4"}])
    got = {f["name"]: f["role"] for f in files_in_text(TYPED, known)}
    assert got == {"approval.yaml": "in_scope", "approval_matrix.xlsx": "not_changed", "workflow.yaml": "not_changed"}      # other.md is not named: not listed
    assert files_in_text("please look at approval.yaml.bak", known) == []                                                    # a longer name is a different file


def test_the_decision_keeps_the_verbatim_words_the_exact_files_the_revision_and_the_ai_wording(env, ch, use, monkeypatch):
    jid, key = _draft_with_question(env, ch, monkeypatch)
    adm = env.admin_client()
    names = {a["logical_path"].split("/")[-1]: a for a in env.store.source_revision_view(env.tenant, env.ws, env.store.head(env.tenant, env.ws).source_revision_id)["artifacts"]}
    typed = f"Keep this change limited to {next(iter(names))}. Do not update {list(names)[1]} in this request. Keep it unresolved."
    reply = {"status": "READY", "message": "ok", "questions": [], "assumptions": [],
             "fields": {"answer": "Leave the other file out.", "kind": "decision", "wish_or_fact": "wish", "rationale": None, "quote": "Do not update"}}
    _m, cid, got = _run_purpose(env, ch, use, "gap_reply", typed, reply, context={"draft_job_id": jid, "gap_key": key}, client=adm)
    f = got["understanding"]["fields"]
    assert f["typed_text"] == typed and f["answer"] == "Leave the other file out."          # the AI wording is beside the words, never instead of them
    assert [(x["name"], x["role"]) for x in f["files"]] == [(list(names)[0], "in_scope"), (list(names)[1], "not_changed")]
    assert all(x["artifact_id"] == names[x["name"]]["artifact_id"] for x in f["files"])
    assert f["source_revision_id"] == env.store.head(env.tenant, env.ws).source_revision_id
    adm.post(f"/v2/chat/{cid}/confirm", json={"workspace_id": env.ws, "version": got["version"], "acknowledged": True})
    r = adm.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": jid, "gap_key": key, "chat_id": cid, "answer": "FORGED BY THE CLIENT"})
    assert r.status_code == 200, r.text
    g = adm.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": jid}).json()
    q = next(x for x in g["gaps"] if x["key"] == key)
    assert q["confirmation"]["answer"] == typed and "FORGED" not in json.dumps(q)           # the server's confirmed words win over what the client sends
    with env.store._tx() as cur:
        cur.execute("SELECT scope FROM v2_decisions WHERE decision_id=%s", (r.json()["decision_id"],))
        scope = cur.fetchone()[0]
    scope = scope if isinstance(scope, dict) else json.loads(scope)
    assert scope["typed_text"] == typed and scope["ai_wording"] == "Leave the other file out." and scope["chat_id"] == cid
    assert [x["role"] for x in scope["files"]] == ["in_scope", "not_changed"] and scope["pinned_source_revision_id"]
    assert "limited to" in scope["provenance"] or "typed by the person" in scope["provenance"]
    # a chat that is not a confirmed decision preview of this person for this point cannot be used
    bad = adm.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": jid, "gap_key": key, "chat_id": "chat-nope"})
    assert bad.status_code == 409


# ------------------------------------------------------------------------------------------------ 3. open items survive a revise
def _open(res):
    return {(i["artifact_id"], json.dumps(i["locator"], sort_keys=True)): i for i in res["items"] if i["status"] in ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED")}


def _revise(env, ch, monkeypatch, intent_id, d1, mutate):
    model = Model(mutate=mutate)
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    jid, r = create_job(env.req_client(), env, "change.draft",
                        {"intent_id": intent_id, "extraction_job_ids": list(ch.jobs.values()), "revises_draft_job_id": d1})
    assert r.status_code == 200, r.text
    drain(env)
    return jid, _res(job_body(ch.admin, env, jid)), model


def test_a_revise_keeps_what_the_earlier_draft_left_open_even_when_the_model_excludes_or_omits_it(env, ch, monkeypatch):
    def first(obj, prompt):
        obj["questions_for_human"] = ["Should the other files also be updated?"]
        for it in obj["items"]:
            if it["action"] == "manual":
                it["action"] = "ambiguity"
    m1 = Model(mutate=first)
    d1, body = run(env, ch, monkeypatch, m1)
    r1 = _res(body)
    open1 = _open(r1)
    assert open1, "the first draft must leave something open for this test to mean anything"
    # a person answers the question (limit to one file), which makes the first draft stale and the revise legal
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit this to the YAML edit."}).status_code == 200

    def second(obj, prompt):
        obj["questions_for_human"] = []
        kept = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"] = "exclude"; it["rationale"] = "limited to the one file"      # the model reads the instruction as "not relevant"
                kept.append(it)
        if kept:
            obj["items"].remove(kept[-1])                                                  # and silently omits one item altogether
    d2, r2, _m = _revise(env, ch, monkeypatch, r1["intent"]["intent_id"], d1, second)
    open2 = _open(r2)
    for k, it in open1.items():
        now = open2.get(k)
        un = [u for u in r2["unaddressed"] if (u["logical_path"], u["location"]) == (it["logical_path"], change_draft._label(it["locator"]))]
        assert now is not None or un, f"{it['logical_path']} vanished from the revised draft"
        if now is None:
            assert un[0]["carried_from"]["draft_job_id"] == d1 and un[0]["disposition"] in ("unresolved", "potentially_affected")      # omitted by the model: listed as unaddressed WITH its lineage
        if now is not None:
            assert now["status"] in ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED") and now["carried_from"]["draft_job_id"] == d1
            assert now["reason"] in (CARRIED_MODEL_EXCLUSION, CARRIED_MISSING) or now["carried_from"].get("status") == it["status"]
            assert now["disposition"] in ("unresolved", "potentially_affected")            # no decision names the file as outside the request: it is simply still open
    assert r2["status"] == "PARTIAL" and r2["carried_unresolved"], r2["status"]
    # the open-points list the people see carries the lineage (no fixed code E00x is used anywhere)
    g2 = ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d2}).json()
    carried = [x for x in g2["gaps"] if x.get("carried_from")]
    assert carried and all(x["disposition"] in ("unresolved", "potentially_affected") for x in carried)
    # the proposal binding keeps them as "left for people" with the lineage
    edit = next(e for e in r2["edits"])
    ctx = SimpleNamespace(store=env.store, job={"tenant_id": env.tenant, "workspace_id": env.ws, "pinned_source_revision_id": r2["pinned_input"]["pinned_source_revision_id"],
                                                 "pinned_decision_epoch": r2["pinned_input"]["pinned_decision_epoch"]})
    bound = resolve_draft_edits(ctx, {"draft_job_id": d2, "intent_id": r1["intent"]["intent_id"], "target_artifact_id": edit["artifact_id"]})
    left = [x for x in bound["left_for_humans"] if x.get("carried_from")]
    assert left and {x["logical_path"] for x in left} <= {i["logical_path"] for i in open1.values()} | {u["logical_path"] for u in r1["unaddressed"]}


def _item(ref, status, aid="art-1", key=7, path="harbor_demo/config/workflow.yaml", reason="model_could_not_decide"):
    return {"ref": ref, "artifact_id": aid, "logical_path": path, "locator": {"type": "line_span", "line_start": key, "line_end": key + 1},
            "status": status, "reason": reason, "model_action": "x", "rationale": "r", "kind": "manual", "candidate_ids": []}


def test_carry_forward_identifies_items_by_artifact_and_location_not_by_the_draft_local_code():
    prev = {"items": [_item("E006", "AMBIGUITY")], "unaddressed": []}
    base = {"items": [_item("E031", "EXCLUDED", reason="model_excluded_with_rationale")], "unaddressed": []}      # renumbered by the new draft
    out = carry_unresolved(base, prev, [], "d1")
    assert base["items"][0]["status"] == "AMBIGUITY" and base["items"][0]["reason"] == CARRIED_MODEL_EXCLUSION
    assert base["items"][0]["carried_from"]["ref"] == "E006" and base["items"][0]["carried_from"]["model_now"]["reason"] == "model_excluded_with_rationale"
    assert out[0]["disposition"] == "unresolved"


def test_a_server_checked_fact_resolves_an_item_but_a_proposed_edit_does_not():
    prev = {"items": [_item("E006", "AMBIGUITY")], "unaddressed": []}
    base = {"items": [_item("E006", "NO_OP", reason=None)], "unaddressed": []}                  # the stored value now equals the new value: a fact the server read
    assert carry_unresolved(base, prev, [], "d1") == [] and base["items"][0]["status"] == "NO_OP"


def test_an_item_missing_from_the_revision_and_an_unaddressed_one_are_kept():
    unaddr = {"ref": "E005", "logical_path": "harbor_demo/config/workflow.yaml", "location": "steps", "text": "steps: [...]"}
    prev = {"items": [_item("E007", "MANUAL_CHANGE", aid="art-2", path="harbor_demo/data/approval_matrix.xlsx", reason="model_marked_manual")], "unaddressed": [unaddr]}
    base = {"items": [], "unaddressed": []}
    out = carry_unresolved(base, prev, [], "d1")
    assert {c["logical_path"] for c in out} == {"harbor_demo/data/approval_matrix.xlsx", "harbor_demo/config/workflow.yaml"}
    assert base["items"][0]["reason"] == CARRIED_MISSING and base["unaddressed"][0]["carried_from"]["status"] == "UNADDRESSED"


def test_a_decision_that_names_the_file_as_not_changed_labels_it_outside_this_request_but_never_resolves_it():
    prev = {"items": [_item("E006", "AMBIGUITY")], "unaddressed": []}
    base = {"items": [_item("E006", "EXCLUDED", reason="model_excluded_with_rationale")], "unaddressed": []}
    used = [{"decision_id": "dec-1", "epoch": 1, "actor_id": "admin-account-1", "rationale": "scope limit", "files": [{"artifact_id": "art-1", "name": "workflow.yaml", "role": "not_changed"}]}]
    carry_unresolved(base, prev, used, "d1")
    it = base["items"][0]
    assert it["status"] == "AMBIGUITY" and it["disposition"] == "outside_this_request" and it["disposition_basis"] == {"decision_id": "dec-1", "by": "admin-account-1", "epoch": 1, "reason": "scope limit"}


def test_the_test_is_sensitive_without_the_carry_the_model_exclusion_does_resolve_the_item(env, ch, monkeypatch):
    """Reproduces the cloud defect: with the server carry switched off, the model's own "exclude" silently turns an open item into a closed one."""
    def first(obj, prompt):
        obj["questions_for_human"] = ["Q?"]
        for it in obj["items"]:
            if it["action"] == "manual":
                it["action"] = "ambiguity"
    d1, body = run(env, ch, monkeypatch, Model(mutate=first))
    r1 = _res(body)
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit this."}).status_code == 200
    monkeypatch.setattr(change_draft, "carry_unresolved", lambda base, prev, used, prev_id: [])

    def second(obj, prompt):
        obj["questions_for_human"] = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"] = "exclude"
    d2, r2, _m = _revise(env, ch, monkeypatch, r1["intent"]["intent_id"], d1, second)
    still = set(_open(r1)) & set(_open(r2))
    assert _open(r1) and all(_open(r2)[k]["disposition"] == "potentially_affected" and _open(r2)[k]["reason"].startswith("excluded_by_model_but") for k in still)   # only what the SERVER reads as the same rule stays open without the carry
    gone = set(_open(r1)) - still
    assert all(i["status"] == "EXCLUDED" and i.get("exclusion_basis") for i in r2["items"] if (i["artifact_id"], json.dumps(i["locator"], sort_keys=True)) in gone)   # the rest is excluded WITH a stated basis


def test_after_two_revises_the_item_is_still_open_and_points_to_the_draft_where_it_was_first_left_open(env, ch, monkeypatch):
    """The cloud case: draft 1 left items ambiguous, draft 2 (a revise) had the model exclude them. A THIRD draft must bring them back, with draft 1 as the origin."""
    def first(obj, prompt):
        obj["questions_for_human"] = ["Q?"]
        for it in obj["items"]:
            if it["action"] == "manual":
                it["action"] = "ambiguity"
    d1, body = run(env, ch, monkeypatch, Model(mutate=first))
    r1 = _res(body)
    intent_id = r1["intent"]["intent_id"]
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit this."}).status_code == 200
    real_carry = change_draft.carry_unresolved
    monkeypatch.setattr(change_draft, "carry_unresolved", lambda base, prev, used, prev_id: [])      # draft 2 = the 2026-10-12 cloud draft (no carry yet)

    def exclude(obj, prompt):
        obj["questions_for_human"] = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"] = "exclude"
    d2, r2, _m = _revise(env, ch, monkeypatch, intent_id, d1, exclude)
    assert _open(r1)
    monkeypatch.setattr(change_draft, "carry_unresolved", real_carry)                                  # the fixed code is back
    d3, r3, _m = _revise(env, ch, monkeypatch, intent_id, d2, exclude)
    back = _open(r3)
    assert all(k in back for k in _open(r1))
    for k in _open(r1):
        assert back[k]["carried_from"]["draft_job_id"] == d1 and back[k]["disposition"] in ("unresolved", "potentially_affected")       # the origin is draft 1, not the draft before


def test_a_server_checked_resolution_closes_an_item_for_good_over_a_chain():
    ident = {"artifact_id": "art-1", "locator": {"type": "line_span", "line_start": 3, "line_end": 4}, "logical_path": "p.yaml"}
    chain = [("d1", {"items": [{**ident, "ref": "E1", "status": "AMBIGUITY"}], "unaddressed": []}),
             ("d2", {"items": [{**ident, "ref": "E9", "status": "EXCLUDED"}], "unaddressed": []}),
             ("d3", {"items": [{**ident, "ref": "E2", "status": "NO_OP"}], "unaddressed": []})]
    assert change_draft.open_over_chain(chain[:2])["items"][0]["_first_open_in"] == "d1"        # a model exclusion does not close it
    assert change_draft.open_over_chain(chain)["items"] == []                                   # the value now equals the new value: closed, with a basis


def test_a_proposed_edit_is_a_remediation_not_a_resolution_and_outside_this_request_keeps_person_and_reason():
    ident = {"artifact_id": "art-1", "locator": {"type": "line_span", "line_start": 3, "line_end": 4}, "logical_path": "p.yaml"}
    prev = {"items": [{**ident, "ref": "E1", "status": "AMBIGUITY", "reason": "model_could_not_decide"}], "unaddressed": []}
    base = {"items": [{**ident, "ref": "E7", "status": "PROPOSED", "reason": None, "model_action": "edit", "rationale": "r"}], "unaddressed": []}
    out = carry_unresolved(base, prev, [], "d1")
    assert base["items"][0]["status"] == "PROPOSED" and base["items"][0]["disposition"] == "proposed_remediation" and out[0]["disposition"] == "proposed_remediation"
    chain = [("d1", {"items": [{**ident, "ref": "E1", "status": "AMBIGUITY"}], "unaddressed": []}), ("d2", {"items": [{**ident, "ref": "E2", "status": "PROPOSED"}], "unaddressed": []})]
    assert change_draft.open_over_chain(chain)["items"], "a proposed edit must not close the item"
    prev2 = {"items": [{**ident, "ref": "E1", "status": "AMBIGUITY"}], "unaddressed": []}
    base2 = {"items": [{**ident, "ref": "E1", "status": "EXCLUDED", "reason": "model_excluded_with_rationale"}], "unaddressed": []}
    used = [{"decision_id": "dec-1", "epoch": 2, "actor_id": "admin-account-1", "rationale": "limit this request to approval.yaml", "answer": "typed",
             "files": [{"artifact_id": "art-1", "name": "p.yaml", "role": "not_changed"}]}]
    carry_unresolved(base2, prev2, used, "d1")
    it = base2["items"][0]
    assert it["status"] == "AMBIGUITY" and it["disposition"] == "outside_this_request"
    assert it["disposition_basis"]["by"] == "admin-account-1" and it["disposition_basis"]["reason"] == "limit this request to approval.yaml"
