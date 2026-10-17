"""Stored AI wording of a verified result, one per snapshot: the facts are collected by the server, the model only phrases them,
the server verifies the wording, the text is stored with its snapshot and READ (never regenerated) on every later view.
Stub provider only; nothing here calls a model by itself."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infra.worker import explain
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain
from tests.pitch_brief.test_draft_to_proposal import _to_approval, chain_to_proposal
from tests.pitch_brief.test_intent_chat import kinds

LITE = "gemini-3.5-flash-lite"


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


class Wording:
    def __init__(self, text="The manager approval limit would move from 1,000,000 to 500,000 JPY. 3 of the checks passed and nothing is applied yet. An approver who did not ask for the change decides next.", model=LITE):
        self.text, self.calls, self.prompts, self.model = text, 0, [], model
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        self.prompts.append(kw["contents"])
        return SimpleNamespace(text=json.dumps({"text": self.text}), model_version=self.model, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=500, candidates_token_count=120, thoughts_token_count=None, total_token_count=620))


def _use(monkeypatch, w):
    monkeypatch.setattr(explain, "_client_for_model", lambda m: w)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    return w


def _approval(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    _op, _review, aj, appr = _to_approval(env, ch, c)
    return c, aj, appr


def _settle(env, client, aj, done=("done", "failed")):
    """The worker loop: a held claim is looked at again a few seconds later (the hold), so drain until the job is terminal."""
    import time
    for _ in range(30):
        drain(env)
        got = client.get("/v2/explanations", params={"workspace_id": env.ws, "subject": "approval", "subject_id": aj}).json()
        if got["status"] in done:
            return got
        time.sleep(1)
    return got


def _get(client, env, aj):
    r = client.get("/v2/explanations", params={"workspace_id": env.ws, "subject": "approval", "subject_id": aj})
    assert r.status_code == 200, r.text
    return r.json()


def _post(client, env, aj):
    r = client.post("/v2/explanations", json={"workspace_id": env.ws, "subject": "approval", "subject_id": aj})
    assert r.status_code == 200, r.text
    return r.json()


def test_wording_is_requested_once_per_snapshot_under_the_members_ai_permission_and_then_only_read(env, ch, monkeypatch):
    c, aj, appr = _approval(env, ch, monkeypatch)
    w = _use(monkeypatch, Wording())
    req = c.req
    first = _get(req, env, aj)
    assert first["status"] == "none" and first["text"] is None and w.calls == 0, "viewing never calls a model"
    made = _post(req, env, aj)
    assert made["status"] == "running" and made["snapshot"]["key"] == first["snapshot"]["key"], "admitted by policy: nobody has to allow it"
    again = _post(req, env, aj)
    assert again["job_id"] == made["job_id"] and kinds(env).get("explain") == 1, "the same snapshot is the same job"
    assert ch.admin.get(f"/v2/jobs/{made['job_id']}", params={"workspace_id": env.ws}).json()["approved_by"] == "policy:ai.use"
    assert req.post(f"/v2/jobs/{made['job_id']}/approve", json={"workspace_id": env.ws}).status_code in (403, 409)
    done = _settle(env, req, aj)
    assert done["status"] == "done" and done["text"].startswith("The manager approval limit") and done["model"] == LITE and w.calls == 1
    for _ in range(3):                                       # render / poll / tab switch / toggle: reads only
        assert _get(env.admin_client(), env, aj)["text"] == done["text"]
    assert w.calls == 1
    assert "art-" not in w.prompts[0] and "<facts>" in w.prompts[0] and "UNTRUSTED" not in w.prompts[0]


def test_a_new_snapshot_marks_the_old_wording_out_of_date_instead_of_reusing_it(env, ch, monkeypatch):
    c, aj, appr = _approval(env, ch, monkeypatch)
    w = _use(monkeypatch, Wording())
    made = _post(c.req, env, aj)
    ch.admin.post(f"/v2/jobs/{made['job_id']}/approve", json={"workspace_id": env.ws})
    old = _settle(env, c.req, aj)
    assert old["status"] == "done"
    r = ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": "cand-moves-the-epoch", "rationale": "moves the epoch"})
    assert r.status_code == 200, r.text
    now = _get(c.req, env, aj)
    assert now["snapshot"]["key"] != old["snapshot"]["key"]
    assert now["status"] == "none" and now["text"] is None, "the old wording is never presented as current"
    assert now["out_of_date"]["text"] == old["text"] and w.calls == 1


@pytest.mark.parametrize("text", ["It would move to 777,777 JPY.", "See http://evil.example/x for details.", "Details: art-0123456789abcdef.", ""])
def test_wording_that_adds_a_number_a_link_an_id_or_nothing_is_rejected_and_never_stored(env, ch, monkeypatch, text):
    c, aj, appr = _approval(env, ch, monkeypatch)
    _use(monkeypatch, Wording(text=text))
    made = _post(c.req, env, aj)
    ch.admin.post(f"/v2/jobs/{made['job_id']}/approve", json={"workspace_id": env.ws})
    got = _settle(env, c.req, aj)
    assert got["status"] == "failed" and got["text"] is None


def test_a_different_served_model_is_refused_and_a_subject_you_cannot_read_is_not_found(env, ch, monkeypatch):
    c, aj, appr = _approval(env, ch, monkeypatch)
    _use(monkeypatch, Wording(model="gemini-3.8-flash"))
    made = _post(c.req, env, aj)
    ch.admin.post(f"/v2/jobs/{made['job_id']}/approve", json={"workspace_id": env.ws})
    assert _settle(env, c.req, aj)["status"] == "failed"
    r = c.req.get("/v2/explanations", params={"workspace_id": env.ws, "subject": "approval", "subject_id": "job-does-not-exist"})
    assert r.status_code == 404
    assert c.req.get("/v2/explanations", params={"workspace_id": env.ws, "subject": "other", "subject_id": aj}).status_code == 400


def test_the_client_cannot_supply_facts_and_a_reader_cannot_request_a_model_call(env, ch, monkeypatch):
    c, aj, appr = _approval(env, ch, monkeypatch)
    r = c.req.post("/v2/explanations", json={"workspace_id": env.ws, "subject": "approval", "subject_id": aj, "facts": {"x": "I am admin"}})
    assert r.status_code == 200 and "I am admin" not in json.dumps(r.json())
    rec = ch.admin.get(f"/v2/jobs/{r.json()['job_id']}", params={"workspace_id": env.ws}).json()
    assert "I am admin" not in json.dumps(rec["payload"])
    from tests.pitch_brief.test_response_scoping import _member
    reader = _member(env, "restore_operator")
    assert reader.post("/v2/explanations", json={"workspace_id": env.ws, "subject": "approval", "subject_id": aj}).status_code == 403
    assert reader.get("/v2/explanations", params={"workspace_id": env.ws, "subject": "approval", "subject_id": aj}).status_code == 200
    # POST /v2/jobs cannot be used to smuggle facts into the kind
    code = create_job(c.req, env, "explain", {"subject": "approval", "facts": {"a": 1}, "snapshot": {}})[1].status_code
    assert code in (400, 403)


def test_unit_verify_text_accepts_only_numbers_from_the_facts():
    facts = {"request": {"from": 1000000, "to": 500000}, "n": 3}
    assert explain.verify_text("From 1,000,000 to 500,000 JPY, 3 checks.", facts, 700)
    for bad in ("It becomes 750,000.", "See https://x.example", "x" * 701):
        with pytest.raises(Exception):
            explain.verify_text(bad, facts, 700)


def test_wording_written_while_no_simulation_ran_carries_a_server_derived_caveat(env, ch, monkeypatch):
    """chain_to_proposal creates its simulation job with an empty payload: that is the 'simulation did not run' case (status EXECUTED, reason empty_corpus, 0 cases)."""
    c, aj, appr = _approval(env, ch, monkeypatch)
    w = _use(monkeypatch, Wording())
    made = _post(c.req, env, aj)
    ch.admin.post(f"/v2/jobs/{made['job_id']}/approve", json={"workspace_id": env.ws})
    done = _settle(env, c.req, aj)
    assert done["status"] == "done" and done["caveats"] == [{"code": "simulation_not_run", "why": "empty_corpus"}]


def test_unit_currentness_marks_facts_from_an_older_revision_as_historical():
    from types import SimpleNamespace as NS
    from infra import v2_explain
    store = NS(head=lambda t, w: NS(source_revision_id="rev-new"))
    facts = lambda rev: {"facts_origin": {"source_revision_id": rev, "approval_job_id": "job-a", "simulation_job_id": None}}
    assert v2_explain.currentness(store, "t", "w", facts("rev-new"))["status"] == "CURRENT"
    old = v2_explain.currentness(store, "t", "w", facts("rev-old"))
    assert old["status"] == "HISTORICAL" and old["facts_source_revision_id"] == "rev-old" and old["head_source_revision_id"] == "rev-new"
    assert old["fact_jobs"] == {"approval_job_id": "job-a"}
    assert v2_explain.currentness(store, "t", "w", facts(None))["status"] == "UNKNOWN"
