"""QRY-02a: the `retrieval.semantic` adapter contract, verified with synthetic providers only. No model, no network, no cost.

What this proves: schema validation, id/scope limits, call/token/payload caps, timeout and bounded retry, cache keys, labelled fallback, no invented evidence, no filler,
authority and counter-evidence preserved, and that the three layers (candidate expansion, ranking, evidence sufficiency) fail and succeed independently.
What it does NOT prove: that any model finds, ranks or judges anything well. Every provider answer here is a SYNTHETIC fixture written by the test author, not a recorded provider response.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from core.change_intelligence.v2 import business_index as bi
from infra.adapters import semantic_retrieval as sr
from infra.adapters import v2_business_index as vbi
from tests.final_loop.test_kno01_business_store import bind, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno03_graph_gaps import seed
from tests.final_loop.test_kno05_accept import accept, answer
from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE, LEX, QUESTIONS, dev_corpus
from tests.pitch_brief.conftest import drain

ROOT = pathlib.Path(__file__).resolve().parents[2]
OFFERED = [{"item_id": "rule-a", "kind": "rule", "text": "A"}, {"item_id": "rule-b", "kind": "rule", "text": "B"}, {"item_id": "gap-c", "kind": "gap", "text": "C"}]
IDS = {i["item_id"] for i in OFFERED}
CAPS = dict(sr.DEFAULT_CAPS)


# ---- schema validation ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("resp", [
    {"terms": ["x"], "candidate_ids": ["rule-z"]},                      # id that was not offered
    {"terms": ["x"], "candidate_ids": ["rule-a", "rule-a"]},            # duplicate
    {"terms": [], "candidate_ids": ["rule-a"] * 11},                    # over the candidate cap
    {"terms": [], "candidate_ids": [], "evidence": "invented"},          # unknown key
    "text", None, [],
])
def test_expand_output_is_validated(resp):
    with pytest.raises(sr.InvalidOutput):
        sr.validate_expand(resp, IDS, CAPS)


@pytest.mark.parametrize("resp", [{"scores": {"rule-z": 0.5}}, {"scores": {"rule-a": 1.5}}, {"scores": {"rule-a": -0.1}}, {"scores": {"rule-a": True}},
                                  {"scores": {"rule-a": float("nan")}}, {"scores": {"rule-a": "0.5"}}, {"scores": [0.5]}, {"scores": {}, "extra": 1}])
def test_rank_output_is_validated(resp):
    with pytest.raises(sr.InvalidOutput):
        sr.validate_rank(resp, IDS, CAPS)


@pytest.mark.parametrize("resp", [{"labels": {"rule-a": "maybe"}, "sufficiency": "sufficient"}, {"labels": {"rule-z": "supports"}, "sufficiency": "sufficient"},
                                  {"labels": {}, "sufficiency": "certain"}, {"labels": [], "sufficiency": "partial"}, {"labels": {}}, {"labels": {}, "sufficiency": "partial", "x": 1}])
def test_judge_output_is_validated(resp):
    with pytest.raises(sr.InvalidOutput):
        sr.validate_judge(resp, IDS, CAPS)


def test_malformed_or_surplus_search_terms_are_dropped_and_counted_and_the_grounded_ids_survive():
    """2026-10-25 (acceptance run AIQ-ACC-QA): a term is not evidence; one bad term no longer discards the candidate ids. Ids stay strictly checked (above)."""
    for terms, kept in ((["x"] * 9, ["x"] * 8), ([" ", "leave"], ["leave"]), (["a\nb", "time off"], ["time off"]), (["x" * 41, "holiday"], ["holiday"]), ([3, "rest"], ["rest"])):
        got = sr.validate_expand({"terms": terms, "candidate_ids": ["rule-a"]}, IDS, CAPS)
        assert got["terms"] == kept and got["candidate_ids"] == ["rule-a"] and got["terms_dropped"] == len(terms) - len(kept)


def test_valid_outputs_pass():
    assert sr.validate_expand({"terms": [" leave "], "candidate_ids": ["rule-a"]}, IDS, CAPS) == {"terms": ["leave"], "candidate_ids": ["rule-a"], "terms_dropped": 0}
    assert sr.validate_rank({"scores": {"rule-a": 0.25, "rule-b": 1}}, IDS, CAPS)["scores"]["rule-b"] == 1.0
    assert sr.validate_judge({"labels": {"rule-a": "supports", "gap-c": "unrelated"}, "sufficiency": "partial"}, IDS, CAPS)["sufficiency"] == "partial"


# ---- adapter behaviour: caps, retry, timeout, fallback, cache ---------------------------------------------------------------------------------

def items_for(rules):
    return bi.build_items(rules, [], [])


def good(layer, request, timeout):
    ids = [i["item_id"] for i in request["items"]]
    if layer == "expand":
        return {"terms": ["extra"], "candidate_ids": ids[:1]}
    if layer == "rank":
        return {"scores": {i: 0.5 for i in ids}}
    return {"labels": {i: "supports" for i in ids}, "sufficiency": "partial"}


def run(adapter, query="who approves leave", *, items=None, **over):
    args = dict(snapshot_id="snap-1", view="target", index_version=bi.INDEX_VERSION, access_scope="business", lexical_candidates=["rule-a", "rule-b"])
    args.update(over)
    return adapter.run(query, items if items is not None else OFFERED_ITEMS, **args)


OFFERED_ITEMS = [dict(e, term_vector={}) for e in OFFERED]


def test_disarmed_adapter_makes_no_call_and_labels_the_fallback():
    out = run(sr.SemanticAdapter())
    assert out["status"] == "fallback" and out["calls"] == 0 and all(v == {"status": "fallback", "reason": "disarmed"} for v in out["layers"].values())
    assert out["sufficiency"] == "unknown" and out["terms"] == [] and out["scores"] == {}


def test_three_layers_apply_independently_and_the_result_is_cached_by_scope():
    p = sr.ScriptedProvider(good)
    a = sr.SemanticAdapter(p)
    first = run(a)
    assert first["status"] == "applied" and first["calls"] == 3 and p.calls == 3 and first["cache"] == "miss" and first["synthetic"] is True
    # the lexical candidates are offered first (selection policy v1, F-1), so the scripted expand layer's first offered id is rule-a
    assert first["terms"] == ["extra"] and first["candidate_ids"] == ["rule-a"] and set(first["labels"]) == {"rule-a", "rule-b"}
    assert first["coverage"]["judged_ids"] == ["rule-a", "rule-b"] and first["coverage"]["offered"] == 3 and first["coverage"]["omitted"] == 0
    again = run(a)
    assert again["cache"] == "hit" and again["calls"] == 0 and p.calls == 3
    for change in ({"query": "who approves holidays"}, {"snapshot_id": "snap-2"}, {"view": "observed"}, {"access_scope": "evidence"}, {"index_version": "v-other"}):
        before = p.calls
        assert run(a, **change)["cache"] == "miss" and p.calls == before + 3, change
    other = sr.SemanticAdapter(sr.ScriptedProvider(good, provider_id="other-model"), cache=a.cache)           # same cache object, different provider id
    assert run(other)["cache"] == "miss"
    newcfg = sr.SemanticAdapter(sr.ScriptedProvider(good, config_version="semantic-retrieval-config-v2"), cache=a.cache)
    assert run(newcfg)["cache"] == "miss"
    assert run(sr.SemanticAdapter(sr.ScriptedProvider(good), caps={"max_terms": 2}, cache=a.cache))["cache"] == "miss"    # caps are part of the key


def test_degraded_results_are_not_cached():
    flaky = {"n": 0}

    def fn(layer, request, timeout):
        if layer == "judge" and flaky["n"] == 0:
            flaky["n"] += 1
            return {"labels": {}, "sufficiency": "bogus"}
        return good(layer, request, timeout)
    a = sr.SemanticAdapter(sr.ScriptedProvider(fn))
    first = run(a)
    assert first["status"] == "partial" and first["layers"]["judge"]["reason"] == "invalid_output" and first["layers"]["expand"]["status"] == "applied" and first["sufficiency"] == "unknown"
    assert run(a)["status"] == "applied"                    # the next request tries again instead of reusing the degraded answer


def test_bounded_retry_and_call_cap():
    def down(layer, request, timeout):
        raise sr.ProviderUnavailable("network")
    p = sr.ScriptedProvider(down)
    out = run(sr.SemanticAdapter(p))
    assert out["status"] == "fallback" and p.calls <= sr.DEFAULT_CAPS["max_calls"] and out["calls"] == p.calls and out["layers"]["expand"]["reason"] == "network"
    assert "call_cap_reached" in {v.get("reason") for v in out["layers"].values()}
    p2 = sr.ScriptedProvider(down)
    run(sr.SemanticAdapter(p2, caps={"max_retries": 0}))
    assert p2.calls == 3                                    # one attempt per layer when retries are off, never more than the call cap


def test_a_retry_that_succeeds_stays_visible_with_its_cause_and_the_slot_it_used():
    """Live finding FNL-LIVE-20261018-01/-02: expand and rank applied, judge fell back with call_cap_reached and nothing said why a third layer had no call left.
    A transient failure followed by a successful retry used that slot; the layer now reports its attempts and the first failure (safe words only), and a retry is
    taken only when every later layer keeps its call."""
    seen = {"expand": 0}

    def once(layer, request, timeout):
        if layer == "expand" and seen["expand"] == 0:
            seen["expand"] += 1
            exc = sr.ProviderUnavailable("transport_error")
            exc.detail = "RuntimeError"
            raise exc
        return good(layer, request, timeout)
    out = run(sr.SemanticAdapter(sr.ScriptedProvider(once), caps={"max_calls": 4}))                   # a spare call: the retry is allowed and stays visible
    assert out["layers"]["expand"] == {"status": "applied", "attempts": 2, "retried_after": {"reason": "transport_error", "detail": "RuntimeError"}}
    assert out["layers"]["rank"] == {"status": "applied"} and out["layers"]["judge"] == {"status": "applied"} and out["calls"] == 4 and out["status"] == "applied"
    seen["expand"] = 0
    tight = run(sr.SemanticAdapter(sr.ScriptedProvider(once)))                                          # one call per layer: no retry takes the judge's call
    assert tight["layers"]["expand"]["reason"] == "transport_error" and tight["layers"]["expand"]["status"] == "fallback"
    assert tight["layers"]["rank"] == {"status": "applied"} and tight["layers"]["judge"] == {"status": "applied"} and tight["calls"] == 3
    clean = run(sr.SemanticAdapter(sr.ScriptedProvider(good)))
    assert all(v == {"status": "applied"} for v in clean["layers"].values())                   # one clean call per layer: nothing extra is reported


def test_timeout_is_a_labelled_fallback_after_the_allowed_attempts():
    ticks = iter(range(0, 1000, 20))                        # every clock reading jumps 20 s: each call "takes" longer than the 8 s timeout
    a = sr.SemanticAdapter(sr.ScriptedProvider(good), clock=lambda: float(next(ticks)))
    out = run(a)
    assert out["status"] == "fallback" and out["layers"]["expand"]["reason"] == "timeout" and out["terms"] == [] and out["calls"] <= sr.DEFAULT_CAPS["max_calls"]


def test_a_late_answer_is_discarded_but_its_usage_is_booked():
    """Live finding FNL-LIVE-20261018-03: an expand answer that arrived after the adapter's timeout was discarded and its usage never reached the job's accounting."""
    def paid(layer, request, timeout):
        return good(layer, request, timeout), {"input_tokens": 100, "output_tokens": 10}
    ticks = iter(range(0, 1000, 20))
    out = run(sr.SemanticAdapter(sr.ScriptedProvider(paid), clock=lambda: float(next(ticks)), caps={"max_retries": 0}))
    assert {v["reason"] for v in out["layers"].values()} == {"timeout"} and out["calls"] == 3
    assert out["tokens_in"] == 300 and out["tokens_out"] == 30                                          # three late, discarded, paid answers: all booked


def test_invalid_output_discards_only_that_layer():
    def fn(layer, request, timeout):
        return {"terms": [], "candidate_ids": ["rule-not-offered"]} if layer == "expand" else good(layer, request, timeout)
    out = run(sr.SemanticAdapter(sr.ScriptedProvider(fn)))
    assert out["layers"]["expand"]["reason"] == "invalid_output" and out["terms"] == [] and out["candidate_ids"] == []            # nothing from the bad layer is used
    assert out["layers"]["rank"]["status"] == "applied" and out["layers"]["judge"]["status"] == "applied" and out["status"] == "partial"


def test_missing_usage_fails_closed_and_stops_further_calls():
    p = sr.ScriptedProvider(lambda l, r, t: (good(l, r, t), None))
    out = run(sr.SemanticAdapter(p))
    assert p.calls == 1 and out["layers"]["expand"]["reason"] == "usage_unknown" and out["layers"]["rank"]["reason"] == "usage_unknown" and out["terms"] == []
    ok = sr.ScriptedProvider(lambda l, r, t: (good(l, r, t), None))
    assert run(sr.SemanticAdapter(ok, caps={"fail_closed_on_missing_usage": False}))["status"] == "applied"


def test_token_cap_stops_the_run():
    p = sr.ScriptedProvider(lambda l, r, t: (good(l, r, t), {"input_tokens": 6100, "output_tokens": 100}))   # two calls pass the 12,000-token total
    out = run(sr.SemanticAdapter(p))
    assert p.calls == 2 and out["layers"]["expand"]["status"] == "applied" and out["layers"]["rank"]["reason"] == "token_cap_reached" and out["layers"]["judge"]["reason"] == "token_cap_reached"


def test_external_provider_is_not_called_while_the_provider_switch_is_off():
    p = sr.ScriptedProvider(good, external=True)
    out = run(sr.SemanticAdapter(p, gate=lambda: True))
    assert p.calls == 0 and out["layers"]["expand"]["reason"] == "provider_switch_off" and out["status"] == "fallback"
    q = sr.ScriptedProvider(good, external=False)                                  # a local replay is not a provider call
    assert run(sr.SemanticAdapter(q, gate=lambda: True))["status"] == "applied"


def test_replay_provider_needs_a_record_and_reports_synthetic():
    req = sr.build_request("expand", "who approves leave", sr.offered_items(OFFERED_ITEMS, max_chars=1000, priority=["rule-a", "rule-b"])[0])   # the run's selection
    rp = sr.ReplayProvider({req["request_hash"]: {"terms": ["time off"], "candidate_ids": []}}, usage={"input_tokens": 0, "output_tokens": 0})
    out = run(sr.SemanticAdapter(rp))
    assert out["layers"]["expand"]["status"] == "applied" and out["terms"] == ["time off"] and out["synthetic"] is True
    assert out["layers"]["rank"]["reason"] == "no_replay_record" and rp.calls == 3                               # the missing records are labelled, not guessed


def test_the_provider_sees_only_offered_business_text_within_the_payload_cap():
    seen = []

    def spy(layer, request, timeout):
        seen.append(request)
        return good(layer, request, timeout)
    rules, items, relations = dev_corpus()
    out = sr.SemanticAdapter(sr.ScriptedProvider(spy)).run("capex review", items, snapshot_id="s", view="target", index_version="v", access_scope="business", lexical_candidates=[])
    assert seen and set(seen[0]) == {"layer", "query", "items", "request_hash"}
    for req in seen:
        for i in req["items"]:
            assert set(i) == {"item_id", "kind", "text"}
    blob = json.dumps(seen)
    for restricted in ("evidence_refs", "approval.yaml#", "cfg-approval", "rev-1", "sha256", "decision_ids", "source_bindings", "term_vector"):
        assert restricted not in blob, restricted
    tiny = sr.SemanticAdapter(sr.ScriptedProvider(spy), caps={"max_payload_chars": 400}).run("capex review", items, snapshot_id="s", view="target", index_version="v2", access_scope="business")
    assert tiny["payload_truncated"] is True and len(seen[-3]["items"]) < len(items)


# ---- merge into ranking: adds nothing but validated candidates, no filler, authority and versions kept ------------------------------------------------

def test_merge_adds_only_validated_existing_items_and_never_pads():
    rules, items, relations = dev_corpus()
    q = "Is there an exception for emergency purchases?"
    base = bi.rank_items(items, q, lexicon=LEX, relations=relations, top_k=40)
    sem = {"terms": ["fast track"], "candidate_ids": ["rule-high-risk-director", "gap-not-in-this-snapshot"], "scores": {"rule-emergency-routing": 0.9}}
    merged = bi.rank_items(items, q, lexicon=LEX, relations=relations, top_k=40, semantic=sem)
    by = {i["item_id"]: i for i in merged["items"]}
    stored = {i["item_id"]: i for i in items}
    assert "gap-not-in-this-snapshot" not in by                                          # an id the snapshot does not have is not created
    assert len(merged["items"]) <= len(base["items"]) + 1                                 # one validated candidate at most; nothing to fill top_k
    assert merged["retrieval"]["semantic_terms"] == ["fast", "track"] and merged["retrieval"]["learned_embeddings"] is False
    for item_id, entry in by.items():                                                    # every returned entry is the stored item, field for field
        for key in ("title", "authority", "status", "actor_ref", "condition", "evidence_refs", "decision_ids"):
            if key in stored[item_id]:
                assert entry[key] == stored[item_id][key], (item_id, key)
    assert by["rule-emergency-routing"]["semantic_rank_score"] == 0.9 and "semantic" in by["rule-emergency-routing"]["via"]
    if "rule-high-risk-director" in by and "rule-high-risk-director" not in {i["item_id"] for i in base["items"]}:
        assert by["rule-high-risk-director"]["via"] == ["semantic"] and by["rule-high-risk-director"]["semantic_candidate"] is True
    versions = {i["item_id"]: [m["authority"] for m in i["versions"]] for i in merged["items"] if i["versions"]}
    assert versions and all({"OBSERVED", "TARGET"} == set(v) for v in versions.values())  # observed and decided versions stay distinguishable


def test_rank_layer_only_reorders_and_cannot_remove():
    rules, items, relations = dev_corpus()
    q = QUESTIONS["Q09"]["text"]
    base = bi.rank_items(items, q, lexicon=LEX, relations=relations, top_k=40)
    ids = [i["item_id"] for i in base["items"]]
    flipped = bi.rank_items(items, q, lexicon=LEX, relations=relations, top_k=40, semantic={"scores": {ids[-1]: 1.0}})
    assert sorted(i["item_id"] for i in flipped["items"]) == sorted(ids)
    assert [i["item_id"] for i in flipped["items"]] != ids


def test_runtime_never_reads_the_expected_results():
    for rel in ("infra/adapters/semantic_retrieval.py", "core/change_intelligence/v2/business_index.py", "infra/adapters/v2_business_index.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for name in ("retrieval_expected", "retrieval_validation", "questions.json", "tests/fixtures"):
            assert name not in text, (rel, name)


# ---- through the real route, store and worker-built index --------------------------------------------------------------------------------------------

@pytest.fixture
def pack_env(env, ws):                                           # noqa: F811
    seed(env, ws, rules=bind(KNOWLEDGE["observed_rules"] + DISTRACTORS, ws.source_revision_id))
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "conflicting_facts")
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "Finance reviews CAPEX from 2,000,000 JPY.")
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    yield admin
    vbi.set_semantic_adapter(None)


def ask(client, env, query, **extra):
    r = client.post("/v2/business/context", json={"workspace_id": env.ws, "query": query, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_pack_default_is_disarmed_and_labelled(env, pack_env):
    vbi.set_semantic_adapter(None)
    pack = ask(pack_env, env, "Who reviews CAPEX requests?")
    assert pack["semantic"]["status"] == "fallback" and pack["semantic"]["layers"]["expand"]["reason"] == "disarmed" and pack["semantic"]["calls"] == 0
    assert pack["sufficiency"] == "unknown" and any("Semantic retrieval was not applied" in n for n in pack["notes"])


def test_an_item_the_judge_reads_as_unrelated_is_set_aside_and_listed_but_counter_evidence_stays(env, pack_env):
    """Live run QRY02B-V4 (2026-10-24): a distractor the judge labelled `unrelated` stayed in the pack. It now leaves the pack and is listed under `set_aside`; an
    item that is counter-evidence of a kept rule stays whatever its label, and `unknown` (not read) stays."""
    keep = "rule-capex-finance-config-target"

    def judge_all_unrelated(layer, request, timeout):
        ids = [i["item_id"] for i in request["items"]]
        if layer == "judge":
            return {"labels": {i: ("supports" if i == keep else "unrelated") for i in ids}, "sufficiency": "sufficient"}
        return good(layer, request, timeout)
    vbi.set_semantic_adapter(sr.SemanticAdapter(sr.ScriptedProvider(judge_all_unrelated)))
    plain = ask(pack_env, env, "Who reviews CAPEX requests?", semantic=False)
    pack = ask(pack_env, env, "Who reviews CAPEX requests?")
    kept = {i["item_id"] for i in pack["items"]}
    aside = {i["item_id"] for i in pack["set_aside"]}
    assert keep in kept and aside and not (aside & kept) and all(i["relevance"] == "unrelated" for i in pack["set_aside"])
    partners = {c["rule_id"] for c in pack["counterevidence"] if c["kind"] == "conflicting_rule"}
    assert partners and not (aside & partners)                                                    # counter-evidence is never set aside
    assert all(i["item_id"] in partners or i["relevance"] != "unrelated" for i in pack["items"])
    assert {c["rule_id"] for c in plain["counterevidence"] if c["kind"] == "conflicting_rule"} <= partners | aside | kept
    vbi.set_semantic_adapter(sr.SemanticAdapter(sr.ScriptedProvider(good)))
    assert ask(pack_env, env, "Who reviews CAPEX requests? (all supporting)")["set_aside"] == []


def test_pack_with_a_synthetic_provider_keeps_authority_and_counterevidence_and_labels_everything(env, pack_env):
    p = sr.ScriptedProvider(good)
    vbi.set_semantic_adapter(sr.SemanticAdapter(p))
    plain = ask(pack_env, env, "Who reviews CAPEX requests?", semantic=False)
    assert plain["semantic"] == {"status": "off", "reason": "off_by_request"} and p.calls == 0
    pack = ask(pack_env, env, "Who reviews CAPEX requests?")
    assert pack["semantic"]["status"] == "applied" and pack["semantic"]["synthetic"] is True and pack["semantic"]["provider"] == "scripted" and p.calls == 3
    # the admin reader also gets source passages, which the judge never receives: its "partial" covers only what it saw and is kept as sufficiency_judged (F-1)
    assert pack["sufficiency"] == "unknown" and pack["sufficiency_judged"] == "partial" and pack["judge_coverage"]["unjudged_observations"] >= 1
    assert all(i["relevance"] in ("supports", "partial", "unrelated", "unknown") for i in pack["items"])
    snapshot_ids = {i["item_id"] for i in plain["items"]} | {i["item_id"] for i in pack["items"]}
    with env.store._tx() as cur:
        cur.execute("SELECT item_id FROM v2_business_index_items WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (env.tenant, env.ws, pack["pin"]["snapshot_id"]))
        stored = {r[0] for r in cur.fetchall()}
    assert stored
    assert snapshot_ids <= stored                                                       # nothing outside the pinned snapshot
    assert any(c["kind"] == "conflicting_rule" for c in pack["counterevidence"]) and pack["graph"]
    target = next(i for i in pack["items"] if i["item_id"] == "rule-capex-finance-config-target")
    assert target["authority"] == "TARGET" and {m["authority"] for m in target["versions"]} == {"OBSERVED", "TARGET"}
    again = ask(pack_env, env, "Who reviews CAPEX requests?")
    assert again["semantic"]["cache"] == "hit" and p.calls == 3                          # same query, snapshot, view, access scope, provider and config
    req = env.req_client()                                                              # another access scope never shares the entry
    low = ask(req, env, "Who reviews CAPEX requests?")
    assert low["semantic"]["cache"] == "miss" and p.calls == 6 and low["observations"]["results"] == []
    assert low["sufficiency"] == ("partial" if low["judge_coverage"]["complete"] else "unknown")              # no passage shown: the verdict stands when the judge saw every item
    obs = ask(pack_env, env, "Who reviews CAPEX requests?", view="observed")
    assert obs["semantic"]["cache"] == "miss" and all(i["authority"] == "OBSERVED" for i in obs["items"] if i["kind"] == "rule" and not i["versions"])


def test_provider_failures_fall_back_to_the_lexical_pack_with_a_label(env, pack_env):
    lexical = ask(pack_env, env, "Who reviews CAPEX requests?", semantic=False)
    for fn, reason in ((lambda l, r, t: (_ for _ in ()).throw(sr.ProviderUnavailable("network")), "network"),
                       (lambda l, r, t: {"terms": [], "candidate_ids": ["rule-ghost"]}, "invalid_output")):
        vbi.set_semantic_adapter(sr.SemanticAdapter(sr.ScriptedProvider(fn)))
        pack = ask(pack_env, env, "Who reviews CAPEX requests?")
        assert pack["semantic"]["layers"]["expand"]["reason"] == reason and pack["semantic"]["status"] in ("fallback", "partial")
        if pack["semantic"]["status"] == "fallback":
            assert [i["item_id"] for i in pack["items"]] == [i["item_id"] for i in lexical["items"]]          # same context as the lexical method
    assert "rule-ghost" not in json.dumps(pack)


def test_semantic_candidates_are_real_items_with_stored_provenance_never_invented(env, pack_env):
    def fn(layer, request, timeout):
        ids = [i["item_id"] for i in request["items"]]
        if layer == "expand":
            return {"terms": ["unrelated words"], "candidate_ids": [i for i in ids if i == "rule-high-risk-director"]}
        return good(layer, request, timeout)
    vbi.set_semantic_adapter(sr.SemanticAdapter(sr.ScriptedProvider(fn)))
    pack = ask(pack_env, env, "zzzz qqqq nothing lexical matches")
    entry = next(i for i in pack["items"] if i["item_id"] == "rule-high-risk-director")
    assert entry["via"] == ["semantic"] and entry["semantic_candidate"] is True and entry["evidence_refs"] and entry["authority"] == "OBSERVED"
    assert [i["item_id"] for i in pack["items"]] == ["rule-high-risk-director"]          # the single validated candidate; nothing was added to fill the list
    assert "unrelat" in pack["retrieval"]["semantic_terms"] and pack["retrieval"]["learned_embeddings"] is False
