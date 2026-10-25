"""F-1 (AI quality round 2026-10-25): the semantic judge's sufficiency verdict is bound to the evidence it actually received.

Found on the live run QRY02B-V5 (item q11): the offered payload was the authorized corpus cut in id order at `max_payload_chars`, so the second rule of a two-hop
question never reached expand / rank / judge; the judge called the half it saw "insufficient" and the worker made no answer call although the final pack held
both rules. What this file proves, with synthetic providers only (no model, no cost): the pack's own candidates and their counter-evidence are offered first,
whole records only, omissions counted; the judge's received ids and request digest are recorded; a verdict over part of the pack's evidence never vetoes
(sufficiency "unknown", the verdict kept as `sufficiency_judged`, counts only); a complete "insufficient" still stops the answer call; the selection policy is
in the cache key. It says nothing about the quality of any model.
"""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import business_index as bi
from infra.adapters import semantic_retrieval as sr
from infra.adapters import v2_business_index as vbi
from infra.worker import knowledge_qa
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import claim, obj, qa_env, run, use  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import job_body


def item(iid, text, kind="rule"):
    return {"item_id": iid, "kind": kind, "text": text, "term_vector": {}}


# ---- selection: the pack's candidates first, whole records, omissions counted ------------------------------------------------------------------------

def test_the_selected_candidates_are_offered_first_even_when_the_corpus_is_over_the_cap():
    corpus = [item(f"rule-a{n:02d}", "x" * 200) for n in range(60)] + [item("rule-zz-needed", "One security officer is rostered for every 100 event guests " + "y" * 150)]
    by_id, _ = sr.offered_selection(corpus, max_chars=2000)                                      # no priority: the old id order, the last rule is cut
    assert "rule-zz-needed" not in {i["item_id"] for i in by_id}
    offered, omitted = sr.offered_selection(corpus, max_chars=2000, priority=["rule-zz-needed", "rule-a05"])
    ids = [i["item_id"] for i in offered]
    assert ids[:2] == ["rule-zz-needed", "rule-a05"] and len(omitted) == len(corpus) - len(offered) and "rule-zz-needed" not in omitted
    assert sum(len(i["text"]) + len(i["item_id"]) + 24 for i in offered) <= 2000
    assert all(set(i) == {"item_id", "kind", "text"} for i in offered)                          # business text only, whole records


def test_an_item_too_large_for_the_payload_is_left_out_and_counted_while_smaller_ones_still_fit():
    corpus = [item("rule-big", "y" * 5000), item("rule-small", "short"), item("rule-other", "also short")]
    offered, omitted = sr.offered_selection(corpus, max_chars=200, priority=["rule-big", "rule-small"])
    assert [i["item_id"] for i in offered] == ["rule-small", "rule-other"] and omitted == ["rule-big"]
    assert sr.offered_items(corpus, max_chars=200, priority=["rule-big", "rule-small"]) == (offered, True)
    assert sr.offered_items(corpus, max_chars=10000) == ([{"item_id": i["item_id"], "kind": "rule", "text": i["text"]} for i in sorted(corpus, key=lambda i: i["item_id"])], False)


def test_an_empty_lexical_search_keeps_the_id_ordered_discovery_path():
    corpus = [item(f"rule-{c}", "text " + c) for c in "dcba"]
    offered, omitted = sr.offered_selection(corpus, max_chars=10000, priority=[])
    assert [i["item_id"] for i in offered] == ["rule-a", "rule-b", "rule-c", "rule-d"] and omitted == []
    offered, _ = sr.offered_selection(corpus, max_chars=10000, priority=["rule-unknown", "rule-c"])   # ids outside the corpus are ignored, never invented
    assert [i["item_id"] for i in offered] == ["rule-c", "rule-a", "rule-b", "rule-d"]


# ---- the adapter records what the judge received; the cache key carries the selection policy ------------------------------------------------------------

def judge_insufficient(layer, request, timeout):
    ids = [i["item_id"] for i in request["items"]]
    if layer == "expand":
        return {"terms": [], "candidate_ids": []}
    if layer == "rank":
        return {"scores": {i: 0.5 for i in ids}}
    return {"labels": {i: "partial" for i in ids}, "sufficiency": "insufficient"}


def test_the_adapter_records_the_judged_ids_and_digest_and_counts_omissions():
    seen = []

    def spy(layer, request, timeout):
        seen.append((layer, request))
        return judge_insufficient(layer, request, timeout)
    corpus = [item(f"rule-a{n:02d}", "x" * 200) for n in range(30)] + [item("rule-zz-needed", "One officer per 100 guests")]
    out = sr.SemanticAdapter(sr.ScriptedProvider(spy), caps={"max_payload_chars": 1500}).run(
        "how many officers", corpus, snapshot_id="s", view="target", index_version="v", access_scope="business", lexical_candidates=["rule-zz-needed", "rule-a03"])
    judge_req = [r for layer, r in seen if layer == "judge"][0]
    assert out["coverage"]["judged_ids"] == sorted(i["item_id"] for i in judge_req["items"]) == ["rule-a03", "rule-zz-needed"]
    assert out["coverage"]["judge_digest"] == judge_req["request_hash"] and out["coverage"]["policy"] == sr.SELECTION_POLICY
    assert out["coverage"]["corpus"] == 31 and out["coverage"]["offered"] + out["coverage"]["omitted"] == 31 and out["payload_truncated"] is True


def test_a_failed_judge_records_no_judged_ids():
    def no_judge(layer, request, timeout):
        if layer == "judge":
            raise sr.ProviderUnavailable("timeout")
        return judge_insufficient(layer, request, timeout)
    out = sr.SemanticAdapter(sr.ScriptedProvider(no_judge), caps={"max_retries": 0}).run(
        "q", [item("rule-a", "A")], snapshot_id="s", view="target", index_version="v", access_scope="business", lexical_candidates=["rule-a"])
    assert out["layers"]["judge"]["status"] == "fallback" and out["coverage"]["judged_ids"] == [] and out["sufficiency"] == "unknown"


def test_the_selection_policy_is_part_of_the_cache_key(monkeypatch):
    provider = sr.ScriptedProvider(judge_insufficient)
    args = dict(query="q", snapshot_id="s", view="target", index_version="v", access_scope="business", provider=provider, caps=sr.DEFAULT_CAPS)
    before = sr.SemanticCache.key(**args)
    monkeypatch.setattr(sr, "SELECTION_POLICY", "another-selection")
    assert sr.SemanticCache.key(**args) != before                                               # a verdict judged on another selection is never reused


# ---- the pack applies the verdict only within the evidence the judge received ----------------------------------------------------------------------

def sem_result(judged, sufficiency="insufficient", judge_status="applied"):
    return {"layers": {"judge": {"status": judge_status}}, "sufficiency": sufficiency, "coverage": {"policy": sr.SELECTION_POLICY, "judged_ids": sorted(judged)}}


def pack_of(items, counter=(), observations=(), sufficiency="insufficient"):
    return {"items": [{"item_id": i, "versions": []} for i in items], "counterevidence": list(counter), "observations": {"results": list(observations)},
            "sufficiency": sufficiency, "notes": []}


def test_a_complete_insufficient_verdict_stands():
    pack = pack_of(["rule-a", "rule-b"])
    vbi._judge_scope(pack, sem_result({"rule-a", "rule-b", "rule-x"}))
    assert pack["sufficiency"] == "insufficient" and pack["judge_coverage"]["complete"] is True and "sufficiency_judged" not in pack and pack["notes"] == []


@pytest.mark.parametrize("pack", [
    pack_of(["rule-a", "rule-b"]),                                                                         # a pack item the judge never saw (the q11 shape)
    pack_of(["rule-a"], counter=[{"kind": "conflicting_rule", "rule_id": "rule-c"}]),                     # counter-evidence the judge never saw
    pack_of(["rule-a"], counter=[{"kind": "open_question", "gap_id": "gap-1"}]),                          # an open question the judge never saw
    pack_of(["rule-a"], observations=[{"passage_id": "p1"}]),                                             # a source passage shown to the reader (the judge never sees them)
])
def test_a_verdict_over_part_of_the_evidence_is_not_applied_and_only_counts_are_shown(pack):
    vbi._judge_scope(pack, sem_result({"rule-a"}))
    cov = pack["judge_coverage"]
    assert pack["sufficiency"] == "unknown" and pack["sufficiency_judged"] == "insufficient" and cov["complete"] is False
    assert set(cov) == {"complete", "pack_items", "unjudged_items", "unjudged_observations", "policy"} and cov["unjudged_items"] + cov["unjudged_observations"] >= 1
    assert "did not see everything" in pack["notes"][0] and "rule-b" not in pack["notes"][0] and "rule-c" not in pack["notes"][0]


def test_a_sufficient_verdict_over_part_of_the_evidence_is_unknown_too_and_a_judge_that_did_not_run_changes_nothing():
    pack = pack_of(["rule-a", "rule-b"], sufficiency="sufficient")
    vbi._judge_scope(pack, sem_result({"rule-a"}, "sufficient"))
    assert pack["sufficiency"] == "unknown" and pack["sufficiency_judged"] == "sufficient"
    untouched = pack_of(["rule-a", "rule-b"], sufficiency="unknown")
    vbi._judge_scope(untouched, sem_result(set(), "unknown", judge_status="fallback"))
    assert "judge_coverage" not in untouched and untouched["sufficiency"] == "unknown"
    vbi._judge_scope(untouched, None)
    assert "judge_coverage" not in untouched


# ---- through the real Q&A worker (test database, scripted providers) ------------------------------------------------------------------------------

QUESTION = "Who has to approve a 1.2 million yen purchase?"


@pytest.fixture(autouse=True)
def _reset_adapter():
    yield
    vbi.set_semantic_adapter(None)


@pytest.fixture
def ceiling_four(qa_env, monkeypatch, tmp_path):                                                         # noqa: F811
    """The answer call plus the three semantic layers (the production shape); a per-job cap that holds four worst-case calls."""
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("max_provider_calls_per_job: 1", "max_provider_calls_per_job: 4").replace('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "5.00"')
    path = tmp_path / "budget_ceiling_four.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return qa_env


def answer_stub(p):
    return obj("ANSWERED", "A manager approves standard purchases of 1,000,000 JPY or more.", [claim("Standard purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")])


def test_a_judge_that_saw_only_part_of_the_pack_does_not_stop_the_answer_call(ceiling_four):
    q = ceiling_four
    external = sr.ScriptedProvider(judge_insufficient, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False, caps={"max_payload_chars": 700}))   # only part of the pack fits the payload
    assert knowledge_qa.planned_calls() == 4
    model = use(q, answer_stub)
    got = run(q, q.admin, QUESTION)
    rec = job_body(q.admin, q.env, got["question_id"])
    res = rec["execution_result"]
    assert rec["execution_status"] == "COMPLETED" and external.calls == 3 and model.calls == 1                    # the answer call was made
    assert res["pack"]["sufficiency"] == "unknown" and res["pack"]["sufficiency_judged"] == "insufficient" and res["pack"]["judge_coverage"]["complete"] is False
    assert "evidence_judged_insufficient" not in res["reasons"] and res["pack"]["semantic"]["coverage"]["omitted"] >= 1
    assert rec["execution_provider_calls"] == 4 <= knowledge_qa.planned_calls()                                 # inside the reservation: no extra call


def test_a_judge_that_saw_the_whole_pack_and_found_it_insufficient_still_stops_the_answer_call(ceiling_four):
    """A reader without evidence rights: no source passage is shown, so the judge received everything the answer could use and its verdict stands."""
    q = ceiling_four
    external = sr.ScriptedProvider(judge_insufficient, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, answer_stub)
    got = run(q, q.req, QUESTION)
    rec = job_body(q.admin, q.env, got["question_id"])
    res = rec["execution_result"]
    assert rec["execution_status"] == "COMPLETED" and external.calls == 3 and model.calls == 0                    # genuine insufficiency: no answer call
    assert res["status"] == "ABSTAINED" and "evidence_judged_insufficient" in res["reasons"]
    assert res["pack"]["judge_coverage"] == dict(res["pack"]["judge_coverage"], complete=True, unjudged_items=0, unjudged_observations=0)
    assert res["pack"]["sufficiency"] == "insufficient" and rec["execution_provider_calls"] == 3


def test_source_passages_the_judge_never_receives_leave_the_answer_to_the_answer_call(ceiling_four):
    """A reader with evidence rights also gets source passages in the answer prompt; the judge never receives them, so its "insufficient" covers only the rules
    it saw: the answer call is made and the grounded answer path decides (it still abstains when the pack cannot support an answer)."""
    q = ceiling_four
    external = sr.ScriptedProvider(judge_insufficient, external=True)
    vbi.set_semantic_adapter(sr.SemanticAdapter(external, gate=lambda: False))
    model = use(q, lambda p: obj("ABSTAINED", "", [], "The rules shown do not say who approves this."))
    got = run(q, q.admin, QUESTION)
    rec = job_body(q.admin, q.env, got["question_id"])
    res = rec["execution_result"]
    cov = res["pack"]["judge_coverage"]
    assert model.calls == 1 and cov["complete"] is False and cov["unjudged_items"] == 0 and cov["unjudged_observations"] >= 1
    assert res["status"] == "ABSTAINED" and "evidence_judged_insufficient" not in res["reasons"] and res["pack"]["sufficiency_judged"] == "insufficient"


def _spy_run(corpus, lexical, protected=(), expand_ids=(), caps=None):
    seen = {}

    def spy(layer, request, timeout):
        ids = [i["item_id"] for i in request["items"]]
        seen[layer] = ids
        if layer == "expand":
            return {"terms": [], "candidate_ids": list(expand_ids)}
        if layer == "rank":
            return {"scores": {i: 0.5 for i in ids}}
        return {"labels": {i: "partial" for i in ids}, "sufficiency": "partial"}
    out = sr.SemanticAdapter(sr.ScriptedProvider(spy), caps=caps).run("q", corpus, snapshot_id="s", view="target", index_version="v", access_scope="business",
                                                                       lexical_candidates=lexical, protected=list(protected))
    return out, seen


def test_rank_and_judge_get_the_top_lexical_then_counter_evidence_then_the_expansions_finds_within_the_answer_budget():
    """Continuation 2026-10-25: with 40 lexical candidates (pack top-k) a flat cut at 20 dropped the counter-evidence and the expansion's paraphrase finds — the
    ones expand exists for. Groups: top 10 lexical, protected (counter-evidence), the expansion's new finds, the rest of the lexical; each id only while the
    layer's answer fits the provider's output limit and the candidates' text fits half the payload; omissions counted per group."""
    corpus = [item(f"rule-a{n:02d}", f"rule text {n}") for n in range(60)]
    lexical = [f"rule-a{n:02d}" for n in range(40)]
    out, seen = _spy_run(corpus, lexical, protected=["rule-a55"], expand_ids=["rule-a50", "rule-a51"])
    rank = seen["rank"]
    assert rank[:10] == lexical[:10] and rank[10] == "rule-a55" and rank[11:13] == ["rule-a50", "rule-a51"] and seen["judge"] == rank
    lc = out["coverage"]["layer_candidates"]["rank"]
    assert lc["count"] == len(rank) and lc["omitted"]["protected"] == 0 and lc["omitted"]["expanded"] == 0 and lc["omitted"]["lexical"] == 40 - (len(rank) - 3)
    assert len(rank) * 0 + sum(len(i) + sr.ANSWER_CHARS_PER_ITEM for i in rank) <= sr.PROVIDER_OUTPUT_TOKENS * sr.OUTPUT_CHARS_PER_TOKEN - sr.ANSWER_WRAPPER_CHARS


def test_long_ids_or_long_texts_shrink_the_candidate_list_instead_of_cutting_the_answer():
    long_ids = [item(f"rule-a-very-long-identifier-of-a-business-rule-number-{n:02d}", "t") for n in range(30)]
    _, seen = _spy_run(long_ids, [i["item_id"] for i in long_ids])
    assert 0 < len(seen["rank"]) < 20 and sum(len(i) + sr.ANSWER_CHARS_PER_ITEM for i in seen["rank"]) <= sr.PROVIDER_OUTPUT_TOKENS * sr.OUTPUT_CHARS_PER_TOKEN - sr.ANSWER_WRAPPER_CHARS
    long_text = [item(f"rule-b{n:02d}", "w" * 900) for n in range(12)]
    _, seen = _spy_run(long_text, [i["item_id"] for i in long_text])
    assert sum(900 + len(i) + 24 for i in seen["rank"]) <= sr.DEFAULT_CAPS["max_payload_chars"] // 2


def test_the_token_total_holds_three_layers_at_their_bounds():
    assert sr.worst_case_tokens(sr.DEFAULT_CAPS) <= sr.DEFAULT_CAPS["max_tokens"]


def test_an_answer_cut_at_the_output_limit_is_a_labelled_fallback_never_a_semantic_success():
    def cut(layer, request, timeout):
        if layer == "rank":
            raise sr.InvalidOutput("invalid_json")
        return judge_insufficient(layer, request, timeout) if layer != "expand" else {"terms": [], "candidate_ids": []}
    out = sr.SemanticAdapter(sr.ScriptedProvider(cut)).run("q", [item("rule-a", "A"), item("rule-b", "B")], snapshot_id="s", view="target", index_version="v",
                                                          access_scope="business", lexical_candidates=["rule-a", "rule-b"])
    assert out["layers"]["rank"]["status"] == "fallback" and out["status"] == "partial" and out["status"] != "applied"
