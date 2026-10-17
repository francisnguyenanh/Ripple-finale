"""QRY-02: retrieval quality on two fixed question sets, measured against expectations written before the method was changed.

Development set: 12 questions over 20 rules (retrieval_expected.json, retrieval_distractors.json), used to build the method. Validation set: 8 questions over 18 rules of a
different domain (retrieval_validation_*.json), written before the rework and never used to tune it. A question passes when its required rules are among the first `window`
returned entries (an entry stands for all versions of one statement) and no forbidden rule is. The runtime never reads the expectations.
Misses are kept visible as strict xfail: a miss that starts passing fails the suite until someone records that the method changed. xfail records a failure; it is not acceptance.
Method: lexicon expansion + tf cosine + version grouping + graph neighbors (ADR-FKL-RETRIEVAL). No model is used; nothing here proves learned-embedding quality."""
from __future__ import annotations

import json
import pathlib

import pytest

from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2 import business_index as bi
from core.change_intelligence.v2.business_knowledge import validate_business_rule
from tests.final_loop.test_kno01_business_store import bind, observed, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno03_graph_gaps import seed
from tests.final_loop.test_kno05_accept import accept, analysed, answer, conflict_gap  # noqa: F401  (analysed is a fixture)
from tests.pitch_brief.conftest import drain

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "final_knowledge_loop"
LEX = bi.lexicon_from_bytes((ROOT / "config" / "business_lexicon.json").read_bytes())
QUESTIONS = {q["id"]: q for q in json.loads((FIX / "questions.json").read_text(encoding="utf-8"))["questions"]}
EXPECTED = json.loads((FIX / "retrieval_expected.json").read_text(encoding="utf-8"))
DISTRACTORS = json.loads((FIX / "retrieval_distractors.json").read_text(encoding="utf-8"))["rules"]
KNOWLEDGE = json.loads((FIX / "expected_knowledge.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((FIX / "contract_examples.json").read_text(encoding="utf-8"))
VAL = json.loads((FIX / "retrieval_validation_rules.json").read_text(encoding="utf-8"))
VAL_Q = {q["id"]: q for q in json.loads((FIX / "retrieval_validation_questions.json").read_text(encoding="utf-8"))["questions"]}
VAL_WINDOW = json.loads((FIX / "retrieval_validation_questions.json").read_text(encoding="utf-8"))["window"]
DECISION = {"decision_id": "decision-s1-capex", "kind": "answer_gap", "quote": "Finance reviews CAPEX from 2,000,000 JPY.", "body": {"summary": ["CAPEX Finance review"]}}


def dev_corpus():
    target = dict(EXAMPLES["target_rule"], rule_id="rule-capex-finance-config-target")
    rules = KNOWLEDGE["observed_rules"] + [target] + DISTRACTORS
    return rules, bi.build_items(rules, [], [DECISION]), bg.derive_relations(rules)


def val_corpus():
    rules = VAL["rules"] + VAL["distractors"]
    return rules, bi.build_items(rules, [], []), bg.derive_relations(rules)


def entries(out):
    return [i for i in out["items"] if i["kind"] == "rule"]


def covered(entry_list, window):
    """Rule ids covered by the first `window` entries: the entry itself and every version merged into it."""
    return [v for e in entry_list[:window] for v in [e["item_id"]] + [m["item_id"] for m in e["versions"]]]


def verdict(required, forbidden, ids):
    return {"missing": [r for r in required if r not in ids], "forbidden": [r for r in forbidden if r in ids]}


def dev_verdict(qid, **kw):
    rules, items, relations = dev_corpus()
    out = bi.rank_items(items, QUESTIONS[qid]["text"], lexicon=LEX, relations=relations, **kw)
    exp = EXPECTED["expected"][qid]
    return verdict(exp["required"], exp["forbidden"], covered(entries(out), EXPECTED["window"]))


def val_verdict(qid, **kw):
    rules, items, relations = val_corpus()
    out = bi.rank_items(items, VAL_Q[qid]["text"], lexicon=LEX, relations=relations, **kw)
    return verdict(VAL_Q[qid]["required"], VAL_Q[qid]["forbidden"], covered(entries(out), VAL_WINDOW))


CLEAN = {"missing": [], "forbidden": []}
DEV_PASSING = ["Q01", "Q02", "Q03", "Q04", "Q05", "Q07", "Q08", "Q09", "Q11", "Q12"]
DEV_MISSES = {"Q06": "2-hop over roles: the high-risk rule shares no term with the question, so it is never a candidate",
              "Q10": "unknown question: loosely related rules still rank; retrieval has no absolute relevance signal"}
VAL_PASSING = ["VQ2", "VQ3", "VQ5", "VQ6"]
VAL_MISSES = {"VQ1": "vocabulary gap: 'holiday' and 'three week' share no term with 'leave' and 'days'; no candidate at all",
              "VQ4": "the required reporting rule is a candidate but ranks behind rules that share generic words",
              "VQ7": "no evidence exists, loosely related rules (shared generic words) are still returned",
              "VQ8": "no evidence exists, loosely related rules (shared generic words) are still returned"}


def test_validation_rules_are_valid_business_rules():
    for r in VAL["rules"] + VAL["distractors"]:
        validate_business_rule(r)
    assert len(VAL["rules"]) == 8 and len(VAL["distractors"]) == 10 and len(VAL_Q) == 8
    assert {q["group"] for q in VAL_Q.values()} == {"paraphrase", "multi_rule", "distractor", "conflict", "no_evidence"}


@pytest.mark.parametrize("qid", DEV_PASSING)
def test_dev_questions_that_the_method_serves(qid):
    assert dev_verdict(qid) == CLEAN, qid


@pytest.mark.parametrize("qid", sorted(DEV_MISSES))
@pytest.mark.xfail(strict=True, reason="known retrieval miss recorded in the QRY-02 evidence")
def test_dev_known_misses_stay_visible(qid):
    assert dev_verdict(qid) == CLEAN, (qid, DEV_MISSES[qid])


@pytest.mark.parametrize("qid", VAL_PASSING)
def test_validation_questions_that_the_method_serves(qid):
    assert val_verdict(qid) == CLEAN, qid


@pytest.mark.parametrize("qid", sorted(VAL_MISSES))
@pytest.mark.xfail(strict=True, reason="known retrieval miss on the validation set recorded in the QRY-02 evidence")
def test_validation_known_misses_stay_visible(qid):
    assert val_verdict(qid) == CLEAN, (qid, VAL_MISSES[qid])


def test_recorded_scores_and_the_ablation_that_chose_the_default():
    """The numbers in the evidence, reproduced. The default (version grouping only) was chosen on the development set; the validation set stayed at its baseline."""
    def score(fn, ids, **kw):
        return sum(1 for q in ids if fn(q, **kw) == CLEAN)
    dev_ids, val_ids = sorted(QUESTIONS), sorted(VAL_Q)
    assert (score(dev_verdict, dev_ids, features=()), score(val_verdict, val_ids, features=())) == (9, 4)                       # before the rework
    assert (score(dev_verdict, dev_ids), score(val_verdict, val_ids)) == (10, 4)                                                # default: grouping
    assert score(dev_verdict, dev_ids, features=("idf",)) == 8 and score(dev_verdict, dev_ids, features=("idf", "qualify", "group", "coverage")) == 9
    assert score(dev_verdict, dev_ids, features=("group", "coverage")) == 10 and score(val_verdict, val_ids, features=("idf", "qualify", "group", "coverage")) == 4


def test_versions_share_a_slot_but_stay_listed_and_distinguishable():
    rules, items, relations = dev_corpus()
    out = bi.rank_items(items, QUESTIONS["Q11"]["text"], lexicon=LEX, relations=relations)
    entry = next(e for e in entries(out) if e["item_id"] == "rule-capex-finance-config-target")
    assert entry["authority"] == "TARGET" and {m["item_id"]: m["authority"] for m in entry["versions"]} == {"rule-capex-finance-config": "OBSERVED", "rule-capex-finance-config-target": "TARGET"}
    assert not any(e["item_id"] == "rule-capex-finance-config" for e in entries(out))                                          # one slot, not two
    assert any(e["item_id"] == "rule-capex-finance-policy" for e in entries(out))                                              # the conflicting statement is a separate entry
    assert out["coverage"]["versions_merged"] >= 1
    plain = bi.rank_items(items, QUESTIONS["Q11"]["text"], lexicon=LEX, relations=relations, features=())
    assert {"rule-capex-finance-config", "rule-capex-finance-config-target"} <= {e["item_id"] for e in entries(plain)}         # without grouping both occupy slots


def test_no_shared_term_is_never_filler_and_nothing_is_invented():
    rules, items, relations = dev_corpus()
    out = bi.rank_items(items, "zzzz qqqq xxxx", lexicon=LEX, relations=relations)
    assert out["items"] == [] and out["coverage"]["returned"] == 0
    wide = bi.rank_items(items, QUESTIONS["Q01"]["text"], lexicon=LEX, relations=relations, top_k=40)
    assert all(i["score"] > 0 or i["via"] == ["graph"] for i in wide["items"])


def test_expansion_is_bounded_and_disclosed():
    w = bi.expand(bi.tokens("who must approve a purchase or authorize a request and review the risk and the emergency"), LEX)
    lim = LEX["limits"]
    assert sum(1 for x in w.values() if x != 1.0) <= lim["max_expanded_terms"] and all(x == lim["expansion_weight"] for x in w.values() if x != 1.0)
    assert bi.expand(bi.tokens("zzz qqq"), LEX) == {"zzz": 1.0, "qqq": 1.0}
    rules, items, relations = dev_corpus()
    r = bi.rank_items(items, QUESTIONS["Q03"]["text"], lexicon=LEX, relations=relations)["retrieval"]
    assert r["learned_embeddings"] is False and r["embedding_version"] is None and r["lexicon_sha256"] == LEX["sha256"] and r["expanded_terms"] and r["method"] == bi.METHOD


def test_conflicting_rule_is_added_by_the_graph_even_without_a_lexical_match():
    rules, items, relations = dev_corpus()
    pair = [dict(i, term_vector={}) if i["item_id"] == "rule-capex-finance-policy" else i for i in items if i["item_id"] in ("rule-capex-finance-config", "rule-capex-finance-policy")]
    out = bi.rank_items(pair, "capex finance review", lexicon=LEX, relations=relations)
    assert [i["item_id"] for i in out["items"]] == ["rule-capex-finance-config", "rule-capex-finance-policy"]
    assert out["items"][1]["via"] == ["graph"] and out["items"][1]["graph_from"] == "rule-capex-finance-config" and out["coverage"]["graph_added"] == 1


def _api_pack_ok(admin, env, text, required, forbidden, window):
    r = admin.post("/v2/business/context", json={"workspace_id": env.ws, "query": text}).json()
    assert r["served_from"] == "index" and r["retrieval"]["method"] == bi.METHOD and r["retrieval"]["lexicon_sha256"] == LEX["sha256"]
    return verdict(required, forbidden, covered([i for i in r["items"] if i["kind"] == "rule"], window)) == CLEAN


def _accepted_workspace(env, ws, rules, ack=True):          # noqa: F811
    seed(env, ws, rules=bind(rules, ws.source_revision_id))
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    return admin


def test_the_real_api_gives_the_same_verdicts_on_the_development_set(env, ws):           # noqa: F811
    admin = _accepted_workspace(env, ws, KNOWLEDGE["observed_rules"] + DISTRACTORS)
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "conflicting_facts")
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "Finance reviews CAPEX from 2,000,000 JPY.")
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    failing = [qid for qid in sorted(QUESTIONS) if not _api_pack_ok(admin, env, QUESTIONS[qid]["text"], EXPECTED["expected"][qid]["required"], EXPECTED["expected"][qid]["forbidden"], EXPECTED["window"])]
    assert failing == sorted(DEV_MISSES), failing


def test_the_real_api_gives_the_same_verdicts_on_the_validation_set(env, ws):            # noqa: F811
    admin = _accepted_workspace(env, ws, VAL["rules"] + VAL["distractors"])
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    failing = [qid for qid in sorted(VAL_Q) if not _api_pack_ok(admin, env, VAL_Q[qid]["text"], VAL_Q[qid]["required"], VAL_Q[qid]["forbidden"], VAL_WINDOW)]
    assert failing == sorted(VAL_MISSES), failing
