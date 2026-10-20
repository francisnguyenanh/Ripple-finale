"""SUR-06 / SUR-08 advisory memo checks (pure; no DB, no provider). The model proposes statements; the server keeps only what its cited facts support."""
import pytest

from core.change_intelligence.v2 import advisory_memo as am

LIMITS = {"max_statements": 3, "max_statement_chars": 300, "max_refs": 3, "max_questions": 2}
KINDS = ("risk", "trade_off", "missing_control", "counterevidence", "minority")
PACK = [am.pack_item("op:1", "operation", "Raise the manager approval threshold from 1,000,000 to 1,300,000 JPY"),
        am.pack_item("rule:r-man", "affected_rule", "Manager review of purchases (algorithm severity HIGH; changed rule)", floor="HIGH", must_address=True, reason="HIGH"),
        am.pack_item("rule:r-fin", "affected_rule", "Finance review of CAPEX (algorithm severity MEDIUM)", floor="MEDIUM"),
        am.pack_item("clm-1", "finding", "adversarial finding: split orders below 1,300,000 avoid review (resolution upheld_by_raiser_disputed_by_others)",
                     floor="high", must_address=True, reason="minority or unresolved finding")]


def stmt(text, refs, materiality="medium", kind="risk", rationale="see the cited facts"):
    return {"kind": kind, "statement": text, "refs": refs, "materiality": materiality, "rationale": rationale}


def run(*statements, questions=()):
    model = am.validate_model_object({"status": "memo", "statements": list(statements), "open_questions": list(questions)}, LIMITS, KINDS)
    return am.check(model, PACK, LIMITS)


def test_a_grounded_statement_is_kept_with_the_severity_floor_and_unaddressed_items_are_listed_by_the_server():
    out = run(stmt("Purchases between 1,000,000 and 1,300,000 JPY lose manager review.", ["op:1", "rule:r-man"], materiality="low"))
    k = out["kept"][0]
    assert k["materiality_proposed"] == "low" and k["materiality"] == "high" and k["floor_applied"] is True      # the memo can never lower the algorithm's severity
    assert [n["ref"] for n in out["not_addressed"]] == ["clm-1"] and out["not_addressed"][0]["origin"] == "server"   # the minority finding is not hidden
    assert out["addressed"] == ["rule:r-man"] and out["counts"]["floor_applied"] == 1


@pytest.mark.parametrize("statement,refs,reason", [
    ("This change will save 2,000,000 JPY per year.", ["op:1"], "figure_not_in_cited_facts:2000000"),
    ("There is a 30% chance of split orders.", ["clm-1"], "figure_not_in_cited_facts:30%"),
    ("Split orders below 1,300,000 will cost the company money.", ["clm-1"], "forecast_or_certainty"),
    ("The new threshold is already approved by Finance.", ["op:1", "rule:r-fin"], "status_claim_not_in_facts"),
    ("Something about an unknown rule.", ["rule:other"], "ref_not_offered"),
    ("No reference at all.", [], "no_refs"),
    ("**Bold** risk.", ["op:1"], "not_plain_text"),
    ("The 1,000,000 JPY figure is cited from the wrong rule.", ["rule:r-fin"], "figure_not_in_cited_facts:1000000"),   # a number must be in the CITED facts
])
def test_unsupported_statements_are_refused_whole_with_their_reason(statement, refs, reason):
    out = run(stmt(statement, refs))
    assert out["kept"] == [] and out["rejected"][0]["reason"] == reason


def test_negated_status_words_cap_duplicates_and_questions_with_invented_numbers():
    ok = stmt("The higher threshold is not yet approved by anyone.", ["op:1"])
    out = run(ok, dict(ok), stmt("Finance review stays.", ["rule:r-fin"]), stmt("A fourth statement.", ["op:1"]), stmt("A fifth statement.", ["op:1"]),
              questions=["Who reviews purchases between 1,000,000 and 1,300,000 JPY?", "Is 5,000,000 the right limit?", "A third question?"])
    assert [r["reason"] for r in out["rejected"]] == ["duplicate", "over_statement_cap"]
    assert len(out["kept"]) == 3 and out["kept"][0]["statement"].startswith("The higher threshold is not yet approved")
    assert out["open_questions"] == ["Who reviews purchases between 1,000,000 and 1,300,000 JPY?"]          # 5,000,000 is in no fact; the third is over the cap


@pytest.mark.parametrize("obj", [
    {"status": "memo", "statements": []},
    {"status": "maybe", "statements": [], "open_questions": []},
    {"status": "memo", "statements": [{"kind": "risk", "statement": "x", "refs": ["op:1"], "materiality": "huge", "rationale": ""}], "open_questions": []},
    {"status": "memo", "statements": [{"kind": "opinion", "statement": "x", "refs": ["op:1"], "materiality": "low", "rationale": ""}], "open_questions": []},
    {"status": "memo", "statements": [{"kind": "risk", "statement": "x", "refs": "op:1", "materiality": "low", "rationale": ""}], "open_questions": []},
    {"status": "memo", "statements": [stmt("x", ["op:1"])] * 10, "open_questions": []},
])
def test_malformed_answers_are_refused(obj):
    with pytest.raises(ValueError):
        am.validate_model_object(obj, LIMITS, KINDS)


def test_numbers_are_normalised_and_percentages_are_distinct():
    assert am.numbers_in("1,000,000 JPY, 12.50 and 15 % but not v3 or rule_2") == {"1000000", "12.5", "15%"}
    rendered = am.render_memo({"model_status": "memo", "memo": run(stmt("Manager review is lost for part of the range.", ["rule:r-man", "clm-1"]))})
    assert rendered["advisory_only"] is True and rendered["complete"] is True and rendered["label"] == am.LABEL
    from core.change_intelligence.v2.business_impact import ref_key                        # EUI-07: typed link keys of the cited rules (op refs are not rule rows)
    assert rendered["statements"][0]["ref_keys"] == [ref_key("r-man")] and ref_key("r-man") == ref_key("r-man-target") and ref_key("r-man") != ref_key("r-other")
    assert "r-man" not in ref_key("r-man")                                                   # opaque: no id in clear


# ---- written refs: exact, or one of two deterministic repairs onto exactly one OFFERED ref (EV3-RISKMEMO-20261019-01: the model copied the prompt's `ref=` label) ----

def test_the_copied_ref_label_and_a_unique_bare_id_map_onto_the_offered_ref_and_the_written_form_is_kept():
    out = run(stmt("Purchases between 1,000,000 and 1,300,000 JPY lose manager review.", ["ref=rule:r-man", "ref=op:1"]))     # the observed live form
    k = out["kept"][0]
    assert k["refs"] == ["rule:r-man", "op:1"] and k["refs_written"] == ["ref=rule:r-man", "ref=op:1"] and out["counts"]["refs_canonicalized"] == 1
    assert out["addressed"] == ["rule:r-man"]
    bare = run(stmt("Finance review stays as it is.", ["r-fin"]))
    assert bare["kept"][0]["refs"] == ["rule:r-fin"] and bare["kept"][0]["refs_written"] == ["r-fin"]
    exact = run(stmt("Finance review stays as it is.", ["rule:r-fin"]))
    assert "refs_written" not in exact["kept"][0] and exact["counts"]["refs_canonicalized"] == 0


@pytest.mark.parametrize("refs", [
    ["ref=rule:other"],                   # the label stripped, still not offered
    ["other"],                            # a bare id naming nothing
    ["1"],                                # a numeric bare id is never guessed (op:1)
    ["rule:op:1"], ["gap:r-man"],         # a wrong prefix is not repaired
    ["ref= "], ["  "],
    ["rule:r-man", "unknown-x"],          # one unmatched ref refuses the whole statement
])
def test_unknown_numeric_mis_prefixed_or_partly_unmatched_refs_stay_refused(refs):
    out = run(stmt("Manager review of purchases changes.", refs))
    assert out["kept"] == [] and out["rejected"][0]["reason"] == "ref_not_offered" and out["rejected"][0]["refs"] == list(dict.fromkeys(refs))[:LIMITS["max_refs"]]


def test_a_bare_id_naming_two_offered_refs_is_ambiguous_and_refused():
    pack = PACK + [am.pack_item("gap:r-man", "open_question", "Who reviews manager purchases on holidays?")]
    model = am.validate_model_object({"status": "memo", "statements": [stmt("Manager review of purchases changes.", ["r-man"])], "open_questions": []}, LIMITS, KINDS)
    out = am.check(model, pack, LIMITS)
    assert out["kept"] == [] and out["rejected"][0]["reason"] == "ref_not_offered"
    assert am.canonical_ref("r-man", {p["ref"]: p for p in pack}) is None and am.canonical_ref("r-man", {p["ref"]: p for p in PACK}) == "rule:r-man"
