"""EUI-05 interview wording help through the real routes on the throwaway PostgreSQL: words of OTHER accepted rules of the same workspace (labelled as not established
for this rule; no amounts, quotes, ids or member directory), blank sentence starters, an unfilled [blank] refused before any turn or model call, and the same access
rule as the interview. Synthetic stub model only where a reading would run."""
from __future__ import annotations

import json

from tests.final_loop.test_aix_k2_interview import answer, start
from tests.final_loop.test_chg04_review import login
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401
from tests.final_loop.test_kno07_answer_interpret import actor_gap
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401


def vocab(q, client, sid, workspace=None):
    return client.get(f"/v2/business/interviews/{sid}/vocabulary", params={"workspace_id": workspace or q.env.ws})


def test_wording_help_offers_other_accepted_rules_words_and_blank_starters_without_amounts_ids_or_people(qa_env):
    q = qa_env
    g = actor_gap(q)
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    model = use(q, lambda p: {"never": "called"})
    r = vocab(q, q.admin, sid)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["gap_kind"] == "missing_actor" and "not established for this rule" in v["label"]
    assert v["starters"] and all("[" in s for s in v["starters"]) and not any("no exception" in s.lower() for s in v["starters"])
    blob = json.dumps(v)
    assert "rule-" not in blob and q.env.admin not in blob and q.env.req not in blob and "evidence" not in blob     # no ids, no accounts, no evidence
    assert not any(ch.isdigit() for item in v["vocabulary"] for ch in item["text"])                                 # no amounts are suggested
    assert all(item["group"] == "actor" and item["used_in"] >= 1 for item in v["vocabulary"])
    from infra.adapters import v2_business
    accepted = (v2_business.business_view(q.env.store, q.env.tenant, q.env.ws).get("accepted") or {}).get("rules") or []
    own = set(g["affected_rule_ids"])
    words = {x["actor_ref"].replace("role-", "").replace("-", " ") for x in accepted if x["rule_id"] not in own and x.get("actor_ref")}
    own_words = {x["actor_ref"].replace("role-", "").replace("-", " ") for x in accepted if x["rule_id"] in own and x.get("actor_ref")} - words
    assert {i["text"] for i in v["vocabulary"]} == words and v["accepted_knowledge"] is bool(accepted)                # exactly the words OTHER accepted rules use
    assert not ({i["text"] for i in v["vocabulary"]} & own_words)
    assert words, "the fixture has other accepted rules with actors, so the help is not trivially empty"
    assert model.calls == 0


def test_an_unfilled_blank_is_refused_before_a_turn_or_a_model_call_and_access_follows_the_interview(qa_env):
    q = qa_env
    g = actor_gap(q)
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    model = use(q, lambda p: {"never": "called"})
    before = counts(q.env)
    r = answer(q, q.admin, sid, "[role] reviews it when [condition].")
    assert r.status_code == 409 and r.json()["code"] == "unfilled_placeholder"
    st = q.admin.get(f"/v2/business/interviews/{sid}", params={"workspace_id": q.env.ws}).json()
    assert st["turns_left"] == st["cap"] and model.calls == 0 and counts(q.env) == before                          # no turn used, nothing written
    viewer = login(q, f"viewer-v-{q.env.nonce}", ("viewer",))
    assert vocab(q, viewer, sid).status_code == 403                                                                  # knowledge.confirm, like the interview
    assert vocab(q, q.admin, sid, workspace="ws-of-someone-else").status_code in (401, 403, 404)
    assert vocab(q, q.admin, "ivw-does-not-exist").status_code == 404
