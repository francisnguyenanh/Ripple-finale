"""Scorer v0.2 tests on an INDEPENDENT hand-made fixture (tiny own artifacts/labels/predictions; not the Pitch set).
No DB, no provider. Descriptive scorer: tests assert counts only. Uses the product parsers for observations."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

from pitch.eval.scorer import (AliasLockError, GENERIC_WORDS, alias_set_hash, anonymised_pairs, build_observations,
                               make_run_context)
from pitch.eval.scorer import score as _score

YAML = """\
expense:
  meal_limit_jpy: 5000
  hotel:
    tokyo_limit: 15000
  effective_from: 2026-04-01
fees:
  standard_rate: 1.5
  premium_rate: 2.5
  tier_note: tiers are 1.5 and 2.5 percent
  cancellation_fee_jpy: 300
approval:
  approver: manager
  enabled: true
  tags: [a, b]
  empty:
notes:
  review_date: 2026-12-31
  hotel_hint: guests should book early, ideally 30 days ahead
  founded: the office opened in 1999
  old_limit: previous meal limit was 4000 JPY, now superseded
"""
MD = "Heading line\n\nRefunds are accepted within 14 days.\n\nSecond block of text.\n"
ART = {"FX1": {"text": YAML, "format": "yaml", "file": "FX1.yaml"},
       "FX2": {"text": MD, "format": "md", "file": "FX2.md"}}
# TRUSTED run context as the runner would supply it (manifest / import mapping); never taken from a prediction.
HASH = {"FX1": "sha256:" + hashlib.sha256(YAML.encode()).hexdigest(),
        "FX2": "sha256:" + hashlib.sha256(MD.encode()).hexdigest()}  # product blob_hash form
CTX = make_run_context("T1", "W1", ["SR1"], {"FX1": ["FX1"], "FX2": ["FX2"]}, artifact_content_hashes=HASH)


def score(labels, artifacts, runs, *a, run_contexts="default", **kw):
    """Fixture default: every run (incl. expected_runs) has the legitimate trusted context CTX."""
    if run_contexts == "default":
        ids = set(runs) | set(kw.get("expected_runs") or [])
        run_contexts = {r: CTX for r in ids}
        if kw.get("trusted_context_hashes") is None:  # fixture-only independent hash source
            kw["trusted_context_hashes"] = {r: CTX["sha256"] for r in ids}
    return _score(labels, artifacts, runs, *a, run_contexts=run_contexts, **kw)


def gold(i, cls, subject, predicate, value, unit=None, scope="all", cmp="==", exceptions=(), status="gold", **kw):
    g = {"id": i, "class": cls, "subject": subject, "predicate": predicate, "comparator": cmp, "value": value,
         "unit": unit, "scope": scope, "modality": "must", "exceptions": list(exceptions), "quote": "",
         "status": status}
    g.update(kw)
    return g


def label(golds, traps=(), soft=(), observed=(), aid="FX1"):
    return {"artifact_id": aid, "file": f"{aid}.yaml", "format": "yaml", "primary_class": "C1", "gold_rules": golds,
            "traps": [{"text": t, "why": "x"} for t in traps],
            "soft_guidance": [{"text": t, "why": "x"} for t in soft], "observed_facts": list(observed),
            "non_input_notes": []}


def kp_loc(kp):
    return {"type": "key_path", "key_path": kp}


def cand(subject, predicate, value, unit=None, scope=None, modality="must", kp=None, aid="FX1", **kw):
    ev = []
    if kp is not None:
        ev = [{"locator": json.dumps(kp_loc(kp), sort_keys=True), "quote": f"{kp}: {value}",
               "artifact_revision_id": aid, "source_revision_id": "SR1", "content_hash": HASH[aid]}]
    c = {"candidate_id": f"c-{subject}-{predicate}-{value}", "subject": subject, "predicate": predicate,
         "value": value, "unit": unit, "scope": scope or {"section": subject}, "modality": modality,
         "applicability": "RECOGNIZED", "exceptions": [], "evidence": ev}
    c.update(kw)
    return c


def run1(golds, cands, **lab):
    return score({"FX1": label(golds, **lab)}, ART, {"r1": {"FX1": {"candidates": cands}}})["runs"]["r1"]


def good_meal(**kw):
    return cand("expense", "meal_limit", 5000, "JPY", kp="expense.meal_limit_jpy", **kw)


MEAL = gold("G1", "C1", "meal expense", "limit", 5000, "JPY")
HOTEL = gold("G2", "C2", "hotel", "limit", 15000, "JPY", scope="Tokyo")
STD = gold("G3", "C5", "fee", "standard rate", 1.5, "%")
PREM = gold("G4", "C5", "fee", "premium rate", 2.5, "%")
APPR = gold("G5", "C3", "approval", "approver", "manager")


# ------------------------------------------------------------------ basics
def test_correct_is_strict():
    r = run1([MEAL], [good_meal()])
    assert r["overall"]["found"] == 1 and r["overall"]["T"] == 1 and r["overall"]["partial"] == 0
    assert r["other"]["spurious"] == 0 and r["evidence"]["correct"] == 1
    assert r["text"]["C1"] == "C1: 1 of 1"


def test_product_parser_dates_lists_none_do_not_crash():
    obs = build_observations(ART["FX1"])
    locs = {o["locator"]["key_path"]: o["raw_value"] for o in obs}
    assert locs["expense.effective_from"] == "2026-04-01" and locs["approval.tags"] == ["a", "b"]
    assert locs["approval.empty"] is None
    # evidence pointing at a date leaf scores without error
    g = gold("GD", "C8", "expense", "effective from", "2026-04-01")
    r = run1([g], [cand("expense", "effective_from", "2026-04-01", kp="expense.effective_from")])
    assert r["overall"]["found"] == 1 and r["evidence"]["correct"] == 1


def test_value_normalisation_string_forms():
    r = run1([MEAL], [cand("expense", "meal_limit", "5,000 JPY", kp="expense.meal_limit_jpy")])
    assert r["overall"]["found"] == 1


def test_boolean_polarity():
    g = gold("GB", "C7", "approval", "enabled", True)
    assert run1([g], [cand("approval", "enabled", True, kp="approval.enabled")])["overall"]["found"] == 1
    r = run1([g], [cand("approval", "enabled", False, kp="approval.enabled")])
    assert r["overall"]["found"] == 0 and r["overall"]["wrong_value"] == 1


# ------------------------------------------------------------------ value / unit / operator
def test_wrong_value_is_reported_and_not_spurious():
    r = run1([MEAL], [cand("expense", "meal_limit", 4000, "JPY", kp="expense.meal_limit_jpy")])
    o = r["overall"]
    assert o["found"] == 0 and o["partial"] == 0 and o["missed"] == 1 and o["wrong_value"] == 1
    assert r["other"]["spurious"] == 0


def test_unit_mismatch_is_not_found():  # M04
    r = run1([MEAL], [cand("expense", "meal_limit", 5000, "USD", kp="expense.meal_limit_jpy")])
    assert r["overall"]["found"] == 0 and r["overall"]["partial"] == 0 and r["overall"]["wrong_value"] == 1


def test_unitless_candidate_is_partial_wrong_unit_not_strict():
    pct = gold("GP", "C5", "fee", "standard rate", 10, "%")
    r = run1([pct], [cand("fee", "standard_rate", 10, kp="fees.standard_rate")])
    assert r["overall"]["found"] == 0 and r["overall"]["partial"] == 1 and r["overall"]["partial_wrong_unit"] == 1
    assert r["overall"]["missed"] == 0
    ok = run1([pct], [cand("fee", "standard_rate", "10%", kp="fees.standard_rate")])
    assert ok["overall"]["partial"] == 1  # evidence leaf holds 1.5, not 10: lenient reason evidence_not_correct
    assert ok["overall"]["partial_evidence_not_correct"] == 1


@pytest.mark.parametrize("val,gcmp,expect", [
    ("at most 5000", "<=", "found"), ("up to 5000", "<=", "found"), ("at least 5000", ">=", "found"),
    ("under 5000", "<=", "wrong"), ("under 5000", ">", "wrong"), ("more than 5000", "<=", "wrong"),
    (">= 5000", "<=", "wrong"), (5000, "<=", "partial"), (5000, ">", "partial"), (5000, "==", "found")])
def test_comparator_words_and_missing_operator(val, gcmp, expect):
    g = gold("GC", "C1", "meal expense", "limit", 5000, "JPY", cmp=gcmp)
    r = run1([g], [cand("expense", "meal_limit", val, "JPY", kp="expense.meal_limit_jpy")])
    o = r["overall"]
    assert {"found": o["found"] == 1, "wrong": o["wrong_value"] == 1, "partial": o["partial"] == 1}[expect]
    if expect == "partial":
        assert o["partial_comparator_missing"] == 1


def test_merged_any_atom_counts_m08():  # candidate value atoms: gold matches the SECOND atom
    r = run1([STD], [cand("fee", "standard rate", [2.5, 1.5], "%", kp="fees.tier_note")])
    assert r["overall"]["partial"] == 1 and r["overall"]["partial_merged_tiers"] == 1 and r["overall"]["missed"] == 0


def test_partial_merged_tiers_credited_once():
    merged = cand("fee", "standard rate premium rate", "1.5 and 2.5", "%", kp="fees.tier_note")
    r = run1([STD, PREM], [merged])
    o = r["overall"]
    assert o["found"] == 0 and o["partial"] == 1 and o["missed"] == 1 and o["partial_merged_tiers"] == 1
    assert r["other"]["spurious"] == 0


def test_tiers_separately_found():
    r = run1([STD, PREM], [cand("fee", "standard_rate", 1.5, "%", kp="fees.standard_rate"),
                           cand("fee", "premium_rate", 2.5, "%", kp="fees.premium_rate")])
    assert r["overall"]["found"] == 2


# ------------------------------------------------------------------ scope
def test_wrong_scope_reported_separately_from_value():
    r = run1([MEAL], [good_meal(scope={"region": "Osaka"})])
    o = r["overall"]
    assert o["found"] == 0 and o["partial"] == 1 and o["wrong_scope"] == 1 and o["wrong_value"] == 0


def test_missing_scope_on_scoped_gold_is_partial_scope():
    r = run1([HOTEL], [cand("hotel", "limit", 15000, "JPY", kp="expense.hotel.tokyo_limit")])
    assert r["overall"]["partial"] == 1 and r["overall"]["wrong_scope"] == 1


def test_scoped_gold_found_strict_when_scope_present():
    r = run1([HOTEL], [cand("hotel", "limit", 15000, "JPY", scope={"city": "Tokyo"}, kp="expense.hotel.tokyo_limit")])
    assert r["overall"]["found"] == 1


def test_scope_negation_is_wrong_scope():  # 'outside Tokyo' vs Tokyo
    r = run1([HOTEL], [cand("hotel", "limit", 15000, "JPY", scope={"city": "outside Tokyo"},
                            kp="expense.hotel.tokyo_limit")])
    assert r["overall"]["found"] == 0 and r["overall"]["partial_scope"] == 1


def test_scope_superset_is_partial_reported():
    r = run1([HOTEL], [cand("hotel", "limit", 15000, "JPY", scope={"city": "Tokyo", "other": "Osaka"},
                            kp="expense.hotel.tokyo_limit")])
    o = r["overall"]
    assert o["found"] == 0 and o["partial_scope_superset"] == 1 and o["wrong_scope"] == 1


def test_wrong_value_attribution_requires_compatible_scope():  # M07
    r = run1([HOTEL], [cand("hotel", "limit", 99, "JPY", scope={"city": "Osaka"}, kp="expense.hotel.tokyo_limit")])
    assert r["overall"]["wrong_value"] == 0 and r["overall"]["missed"] == 1 and r["other"]["spurious"] == 1
    r2 = run1([HOTEL], [cand("hotel", "limit", 99, "JPY", scope={"city": "Tokyo"}, kp="expense.hotel.tokyo_limit")])
    assert r2["overall"]["wrong_value"] == 1 and r2["other"]["spurious"] == 0


# ------------------------------------------------------------------ subject heuristic (pre-registered, fixed)
def test_generic_stoplist_is_fixed_and_echoed():
    out = score({"FX1": label([MEAL])}, ART, {"r": {"FX1": {"candidates": []}}})
    assert out["generic_words"] == sorted(GENERIC_WORDS) and "cap" in GENERIC_WORDS and "daily" in GENERIC_WORDS


def test_taxi_daily_cap_vs_hotel_nightly_cap_not_matched():
    g = gold("GT", "C1", "taxi", "daily cap", 5000, "JPY")
    r = run1([g], [cand("hotel", "nightly cap", 5000, "JPY", kp="expense.meal_limit_jpy")])
    assert r["overall"]["found"] == 0 and r["other"]["spurious"] == 1 and r["other"]["undecided_pairs"] == 0


@pytest.mark.parametrize("gs,gp,cs,cp", [("fee", "amount", "cancellation fee", "amount"),
                                         ("domestic travel", "limit", "international travel", "limit")])
def test_known_false_matches_are_undecided_not_strict_not_missed(gs, gp, cs, cp):
    g = gold("GU", "C5", gs, gp, 300, "JPY")
    r = run1([g], [cand(cs, cp, 300, "JPY", kp="fees.cancellation_fee_jpy")])
    o = r["overall"]
    assert o["found"] == 0 and o["partial"] == 0 and o["missed"] == 0 and o["undecided"] == 1 and o["T"] == 1
    assert r["other"]["spurious"] == 0 and r["other"]["undecided_pairs"] == 1
    assert r["coverage"] == {"gold_total": 1, "undecided": 1, "decided": 0}
    u = r["artifacts"]["FX1"]["undecided"][0]
    assert u["gold_text"]["subject"] == gs and u["candidate_text"]["subject"] == cs
    assert [x["category"] for x in r["artifacts"]["FX1"]["unmatched_predictions"]] == ["undecided"]


def test_all_generic_gold_falls_back_to_exact_subject():
    g = gold("GL", "C1", "limit", "cap", 5000, "JPY")
    assert run1([g], [cand("limit", "cap", 5000, "JPY", kp="expense.meal_limit_jpy")])["overall"]["found"] == 1
    r = run1([g], [cand("meal limit", "cap", 5000, "JPY", kp="expense.meal_limit_jpy")])
    assert r["overall"]["found"] == 0 and r["overall"]["undecided"] == 0


def test_subject_override_is_disclosed():
    c = cand("zzz", "qqq", 5000, "JPY", kp="expense.meal_limit_jpy")
    base = score({"FX1": label([MEAL])}, ART, {"r": {"FX1": {"candidates": [c]}}})
    forced = score({"FX1": label([MEAL])}, ART, {"r": {"FX1": {"candidates": [c]}}},
                   subject_overrides={c["candidate_id"]: "G1"})
    assert base["runs"]["r"]["overall"]["found"] == 0 and base["runs"]["r"]["other"]["spurious"] == 1
    assert forced["runs"]["r"]["overall"]["found"] == 1 and forced["subject_overrides"]


# ------------------------------------------------------------------ adjudication (provisional vs final)
def _undecided_setup():
    g = gold("GU", "C5", "fee", "amount", 300, "JPY")
    c = cand("cancellation fee", "amount", 300, "JPY", kp="fees.cancellation_fee_jpy")
    return {"FX1": label([g])}, {"r1": {"FX1": {"candidates": [c]}}, "r2": {"FX1": {"candidates": [c]}}}


def test_provisional_then_final_after_adjudication():
    labels, runs = _undecided_setup()
    prov = score(labels, ART, runs)
    assert prov["metrics_status"] == "PROVISIONAL_ONLY" and prov["final"] is None
    assert prov["aggregate_over_runs"]["overall"]["found"] == 0 and prov["aggregate_over_runs"]["overall"]["T"] == 2
    assert prov["aggregate_over_runs"]["coverage"] == {"gold_total": 2, "undecided": 2, "decided": 0}
    pairs = anonymised_pairs(prov)
    assert len(pairs) == 1 and set(pairs[0]) == {"pair_id", "artifact_id", "gold_text", "candidate_text"}
    blob = json.dumps(pairs)
    assert "r1" not in blob and "r2" not in blob and "c-cancellation" not in blob
    pid = pairs[0]["pair_id"]
    meta = {"adjudicator": "blind-A", "basis": "same fee", "timestamp": "2026-10-06T00:00:00Z"}
    fin = score(labels, ART, runs, adjudications={pid: {"decision": "match", **meta}})
    assert fin["metrics_status"] == "FINAL_AVAILABLE"
    assert fin["final"]["aggregate_over_runs"]["overall"]["found"] == 2
    assert fin["final"]["aggregate_over_runs"]["coverage"]["undecided"] == 0
    assert fin["aggregate_over_runs"]["overall"]["found"] == 0  # provisional stays visible and separate
    assert "FINAL" in fin["final"]["formula"]
    no = score(labels, ART, runs, adjudications={pid: {"decision": "no_match", **meta}})
    f = no["final"]["aggregate_over_runs"]
    assert f["overall"]["found"] == 0 and f["overall"]["missed"] == 2 and f["other"]["spurious"] == 2


@pytest.mark.parametrize("bad", [{"decision": "match"}, {"decision": "maybe", "adjudicator": "a", "basis": "b",
                                                         "timestamp": "t"},
                                 {"decision": "match", "adjudicator": "a", "basis": "b", "timestamp": "t",
                                  "arm": "d0"}])
def test_bad_adjudication_refused(bad):
    labels, runs = _undecided_setup()
    with pytest.raises(ValueError):
        score(labels, ART, runs, adjudications={"x": bad})


# ------------------------------------------------------------------ aliases (locked, scorer-side only)
def _aliased(subject_aliases=None, value_aliases=None, value="manager", subject="approval", predicate="approver"):
    g = gold("GA", "C3", subject, predicate, value)
    if subject_aliases:
        g["subject_aliases"] = subject_aliases
    if value_aliases:
        g["value_aliases"] = value_aliases
    return {"FX1": label([g])}


def test_alias_requires_matching_locked_hash():
    labels = _aliased(subject_aliases=["sign off chain"])
    runs = {"r": {"FX1": {"candidates": []}}}
    with pytest.raises(AliasLockError):
        score(labels, ART, runs)
    with pytest.raises(AliasLockError):
        score(labels, ART, runs, locked_alias_hash="0" * 64)
    h = alias_set_hash(labels)
    out = score(labels, ART, runs, locked_alias_hash=h)
    assert out["alias_set_sha256"] == h and out["alias_lock_checked"] is True
    with pytest.raises(AliasLockError):  # hash locked for no-alias labels refuses an alias list added later
        score(labels, ART, runs, locked_alias_hash=alias_set_hash({"FX1": label([gold("x", "C1", "a", "b", 1)])}))


def test_subject_alias_used_before_token_overlap_and_echoed():
    labels = _aliased(subject_aliases=["sign off chain"])
    c = cand("sign off chain", "owner", "manager", kp="approval.approver")
    out = score(labels, ART, {"r": {"FX1": {"candidates": [c]}}}, locked_alias_hash=alias_set_hash(labels))
    r = out["runs"]["r"]
    assert r["overall"]["found"] == 1 and r["artifacts"]["FX1"]["verdicts"]["GA"]["subject_basis"] == "alias"


def test_alias_does_not_change_polarity_scope_or_modality():
    labels = _aliased(subject_aliases=["sign off chain"])
    h = alias_set_hash(labels)

    def go(c):
        return score(labels, ART, {"r": {"FX1": {"candidates": [c]}}}, locked_alias_hash=h)["runs"]["r"]
    assert go(cand("no sign off chain", "owner", "manager", kp="approval.approver"))["overall"]["found"] == 0
    r = go(cand("sign off chain", "owner", "manager", scope={"region": "EU"}, kp="approval.approver"))
    assert r["overall"]["found"] == 0 and r["overall"]["partial_scope"] == 1
    r = go(cand("sign off chain", "owner", "manager", modality="observed", kp="approval.approver"))
    assert r["overall"]["found"] == 0 and r["overall"]["partial_modality_observed"] == 1


def test_value_alias_for_string_values_only_when_listed():
    labels = _aliased(value_aliases=["line manager"])
    h = alias_set_hash(labels)

    def go(v, lab=labels, hh=h):
        c = cand("approval", "approver", v, kp="approval.approver")
        return score(lab, ART, {"r": {"FX1": {"candidates": [c]}}}, locked_alias_hash=hh)["runs"]["r"]
    assert go("line manager")["overall"]["found"] == 1
    assert go("senior manager")["overall"]["found"] == 0  # no token overlap acceptance
    plain = _aliased()
    assert go("line manager", plain, None)["overall"]["found"] == 0  # unlisted: not accepted


def test_value_aliases_never_apply_to_booleans_or_numbers():
    g = gold("GB", "C7", "approval", "enabled", True, value_aliases=["false", "no"])
    lab = {"FX1": label([g])}
    h = alias_set_hash(lab)
    r = score(lab, ART, {"r": {"FX1": {"candidates": [cand("approval", "enabled", False, kp="approval.enabled")]}}},
              locked_alias_hash=h)["runs"]["r"]
    assert r["overall"]["found"] == 0 and r["overall"]["wrong_value"] == 1
    n = gold("GN", "C1", "meal expense", "limit", 5000, "JPY", value_aliases=[4000])
    lab = {"FX1": label([n])}
    r = score(lab, ART, {"r": {"FX1": {"candidates": [cand("expense", "meal_limit", 4000, "JPY",
                                                              kp="expense.meal_limit_jpy")]}}},
              locked_alias_hash=alias_set_hash(lab))["runs"]["r"]
    assert r["overall"]["found"] == 0


def test_labels_and_aliases_not_imported_by_d0_or_extraction():
    root = Path(__file__).resolve().parents[2]
    files = list((root / "pitch" / "d0").glob("*.py")) + [root / "infra" / "worker" / "extraction.py"]
    assert len(files) >= 4
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                assert not m.startswith("pitch.eval"), (f.name, m)
        src = f.read_text(encoding="utf-8")
        for needle in ("labels_private", "subject_aliases", "value_aliases", "gold_rules"):
            assert needle not in src, (f.name, needle)


# ------------------------------------------------------------------ partial reasons / neutral buckets
def test_missing():
    r = run1([MEAL, APPR], [good_meal()])
    assert r["overall"]["missed"] == 1 and r["overall"]["missed_wrong_value"] == 0
    assert r["artifacts"]["FX1"]["verdicts"]["G5"]["missed_reason"] == "absent"


def test_spurious_counted():
    r = run1([MEAL], [good_meal(), cand("queue", "depth_max", 99, kp="expense.meal_limit_jpy")])
    assert r["other"]["spurious"] == 1 and r["other"]["duplicates"] == 0 and r["overall"]["found"] == 1


def test_trap_hit_is_spurious_and_counted():
    r = run1([MEAL], [good_meal(), cand("meal", "old_limit", 4000, "JPY", kp="notes.old_limit")],
             traps=["previous meal limit was 4000 JPY, now superseded"])
    assert r["other"]["spurious"] == 1 and r["other"]["trap_hits"] == 1


def test_duplicate_counted_once_not_spurious_and_unmatched_listed():
    d = good_meal()
    d2 = copy.deepcopy(d)
    d2["candidate_id"] = "dup"
    r = run1([MEAL], [d, d2, cand("queue", "depth_max", 99)])
    cats = sorted(u["category"] for u in r["artifacts"]["FX1"]["unmatched_predictions"])
    assert cats == ["duplicate", "spurious"]
    assert r["other"]["duplicates"] == 1 and r["overall"]["found"] == 1


def test_observed_modality_on_gold_is_partial():
    r = run1([MEAL], [good_meal(modality="observed")])
    assert r["overall"]["partial"] == 1 and r["overall"]["partial_modality_observed"] == 1


def test_observed_unmatched_not_spurious():
    r = run1([MEAL], [good_meal(), cand("misc", "count", 7, modality="observed")])
    assert r["other"]["observed_reported"] == 1 and r["other"]["spurious"] == 0


SOFT = ["guests should book early, ideally 30 days ahead for the hotel"]
FACT = ["the office opened in 1999"]


def test_soft_guidance_observed_is_neutral():
    r = run1([MEAL], [good_meal(), cand("hotel", "book_ahead", 30, "days", modality="observed", kp="notes.hotel_hint")],
             soft=SOFT)
    assert r["other"]["soft_guidance_extracted"] == 1 and r["other"]["spurious"] == 0
    assert r["overall"]["T"] == 1


@pytest.mark.parametrize("mod", ["must", "may"])
def test_soft_guidance_as_normative_rule_is_error(mod):
    r = run1([MEAL], [good_meal(), cand("hotel", "book_ahead", 30, "days", modality=mod, kp="notes.hotel_hint")],
             soft=SOFT)
    assert r["other"]["normative_from_soft"] == 1 and r["other"]["spurious"] == 1
    assert r["other"]["soft_guidance_extracted"] == 0 and r["classes"]["C1"]["normative_from_soft"] == 1


def test_observed_fact_neutral_and_normative_error():
    ok = run1([MEAL], [good_meal(), cand("office", "opened year", 1999, modality="observed", kp="notes.founded")],
              observed=FACT)
    assert ok["other"]["observed_fact_neutral"] == 1 and ok["other"]["spurious"] == 0
    bad = run1([MEAL], [good_meal(), cand("office", "opened year", 1999, kp="notes.founded")], observed=FACT)
    assert bad["other"]["normative_from_fact"] == 1 and bad["other"]["spurious"] == 1


def test_excluded_ambiguous_not_scored_and_neutral():
    ex = gold("G9", "C7", "refund", "method", "original_payment", status="excluded_ambiguous")
    r = run1([MEAL, ex], [good_meal(), cand("refund", "method", "original_payment")])
    assert r["overall"]["T"] == 1 and r["other"]["excluded_ambiguous"] == 1
    assert r["other"]["excluded_matched"] == 1 and r["other"]["spurious"] == 0


def test_class_mismatch_found_but_class_reported():
    r = run1([MEAL], [good_meal(rule_class="C4")])
    assert r["overall"]["found"] == 1 and r["overall"]["class_mismatch"] == 1 and r["classes"]["C1"]["found"] == 1


def test_c6_exception_omitted_is_partial():
    g = gold("G6", "C6", "meal expense", "limit", 5000, "JPY", exceptions=["board members"])
    assert run1([g], [good_meal()])["overall"]["partial_exception_omitted"] == 1
    assert run1([g], [good_meal(exceptions=["board members"])])["overall"]["found"] == 1


# ------------------------------------------------------------------ evidence
def _ev(evidence, aid="FX1", gold_=MEAL, ctx="default", pred_extra=None, hashes=None, policy="required"):
    c = good_meal()
    c["evidence"] = evidence
    lab = label([gold_], aid=aid)
    pred = {"candidates": [c], **(pred_extra or {})}
    out = score({aid: lab}, ART, {"r": {aid: pred}}, run_contexts=ctx, trusted_context_hashes=hashes,
                 content_hash_policy=policy)
    return out["runs"]["r"]


def _e(loc, **kw):
    d = {"locator": loc if isinstance(loc, str) else json.dumps(loc, sort_keys=True), "artifact_revision_id": "FX1",
         "source_revision_id": "SR1", "content_hash": HASH["FX1"]}
    d.update(kw)
    return d


def test_evidence_correct_strict_and_quote_ignored():
    r = _ev([_e(kp_loc("expense.meal_limit_jpy"), quote="[chunk 3] totally reformatted (5,000 yen)")])
    assert r["overall"]["found"] == 1 and r["evidence"]["correct"] == 1


def test_evidence_wrong_location_not_containing_value_makes_partial():
    r = _ev([_e(kp_loc("expense.hotel.tokyo_limit"))])
    assert r["evidence"]["not_containing_value"] == 1 and r["overall"]["found"] == 0
    assert r["overall"]["partial"] == 1 and r["overall"]["partial_evidence_not_correct"] == 1


def test_evidence_wrong_path_and_no_chunk_numbering():
    assert _ev([_e(kp_loc("no.such.path"))])["evidence"]["wrong_path"] == 1
    assert _ev([_e({"chunk": 0})])["evidence"]["unresolvable"] == 1


def test_evidence_line_span_exact_and_bounds():  # M22
    g = gold("GM", "C4", "refund", "window", 14, "days")
    good = {"type": "line_span", "line_start": 3, "line_end": 3}
    c = cand("refund", "window", 14, "days")
    for loc, key in ((good, "correct"), ({"type": "line_span", "line_start": 3, "line_end": 4}, "wrong_chunk"),
                     ({"type": "line_span", "line_start": 99, "line_end": 99}, "wrong_chunk"),
                     ({"type": "line_span", "line_start": 1, "line_end": 1}, "not_containing_value")):
        c["evidence"] = [{"locator": json.dumps(loc), "artifact_revision_id": "FX2", "source_revision_id": "SR1",
                          "content_hash": HASH["FX2"]}]
        lab = label([g], aid="FX2")
        r = score({"FX2": lab}, ART, {"r": {"FX2": {"candidates": [c]}}})["runs"]["r"]
        assert r["evidence"][key] == 1, (loc, r["evidence"])


LOC = kp_loc("expense.meal_limit_jpy")


def test_legitimate_run_is_not_forced_to_strict_zero():
    r = _ev([_e(LOC)])  # EvidenceRef has no tenant field; ownership comes from the trusted context
    assert r["overall"]["found"] == 1 and r["evidence"]["correct"] == 1
    assert r["artifacts"]["FX1"]["verdicts"]["G1"]["evidence_rows"] == {"content": "correct", "ownership": "valid",
                                                                                   "content_hash": "valid"}
    assert r["evidence_content_row"]["correct"] == 1 and r["evidence_ownership_row"]["valid"] == 1
    assert r["context_state"] == "ok"


def test_foreign_artifact_rejected():
    r = _ev([_e(LOC, artifact_revision_id="OTHER")])
    assert r["evidence"]["wrong_artifact"] == 1 and r["overall"]["found"] == 0 and r["overall"]["partial"] == 1
    assert r["evidence_content_row"]["wrong_artifact"] == 1


def test_foreign_tenant_workspace_via_source_revision_rejected():
    r = _ev([_e(LOC, source_revision_id="SR-OTHER-TENANT")])
    assert r["evidence"]["foreign_tenant_workspace"] == 1 and r["overall"]["found"] == 0
    assert r["evidence_ownership_row"]["foreign_tenant_workspace"] == 1 and r["evidence_content_row"]["correct"] == 1


@pytest.mark.parametrize("where,key,val", [("ev", "tenant_id", "T2"), ("ev", "workspace_id", "W2"),
                                           ("pred", "tenant_id", "T2"), ("pred", "workspace_id", "W2")])
def test_claimed_foreign_tenant_or_workspace_rejected(where, key, val):
    if where == "ev":
        r = _ev([_e(LOC, **{key: val})])
    else:
        r = _ev([_e(LOC)], pred_extra={key: val})
    assert r["evidence"]["foreign_tenant_workspace"] == 1 and r["overall"]["found"] == 0


def test_claimed_matching_tenant_cannot_validate_without_trusted_context():
    r = _ev([_e(LOC, tenant_id="T1", workspace_id="W1")], ctx=None, pred_extra={"tenant_id": "T1"})
    assert r["context_state"] == "missing" and r["evidence"]["unverifiable"] == 1 and r["evidence"]["correct"] == 0


def test_missing_context_is_unverifiable_and_stays_in_denominator():
    for ctx in (None, {}, "garbage", {"r": None}, {"r": {}}, {"r": "garbage"}):
        r = _ev([_e(LOC)], ctx=ctx)
        assert r["evidence"]["unverifiable"] == 1 and r["evidence"]["correct"] == 0
        assert r["overall"]["found"] == 0 and r["overall"]["partial"] == 1 and r["overall"]["T"] == 1
    out = score({"FX1": label([MEAL])}, ART, {"r": {"FX1": {"candidates": [good_meal()]}}}, run_contexts={})
    assert out["runs"]["r"]["context_state"] == "missing" and out["runs"]["r"]["overall"]["T"] == 1


def test_altered_context_is_not_valid():
    forged = copy.deepcopy(CTX)
    forged["source_revision_ids"] = ["SR1", "SR-OTHER-TENANT"]  # body changed, sha256 not recomputed
    r = _ev([_e(LOC, source_revision_id="SR-OTHER-TENANT")], ctx={"r": forged})
    assert r["context_state"] == "tampered" and r["evidence"]["context_tampered"] == 1
    assert r["overall"]["found"] == 0 and r["evidence_ownership_row"]["context_tampered"] == 1
    # re-hashed forgery is caught by the runner-held hash
    rehashed = make_run_context("T1", "W1", ["SR1", "SR-OTHER-TENANT"], {"FX1": ["FX1"]},
                                artifact_content_hashes=HASH)
    r2 = _ev([_e(LOC, source_revision_id="SR-OTHER-TENANT")], ctx={"r": rehashed}, hashes={"r": CTX["sha256"]})
    assert r2["context_state"] == "tampered" and r2["overall"]["found"] == 0
    ok = _ev([_e(LOC)], ctx={"r": CTX}, hashes={"r": CTX["sha256"]})
    assert ok["context_state"] == "ok" and ok["overall"]["found"] == 1


def test_missing_evidence_metadata_is_unverifiable():
    for ev in ({"locator": json.dumps(LOC), "source_revision_id": "SR1"},        # no artifact revision
               {"locator": json.dumps(LOC), "artifact_revision_id": "FX1"},      # no source revision
               {"artifact_revision_id": "FX1", "source_revision_id": "SR1", "quote": "expense.meal_limit_jpy: 5000"}):
        r = _ev([ev])
        assert r["evidence"]["unverifiable"] == 1 and r["evidence"]["correct"] == 0
        assert r["overall"]["found"] == 0 and r["overall"]["partial"] == 1 and r["overall"]["T"] == 1


def test_context_rows_aggregate_over_runs():
    labels = {"FX1": label([MEAL])}
    c = good_meal()
    out = score(labels, ART, {"r1": {"FX1": {"candidates": [c]}}, "r2": {"FX1": {"candidates": [c]}}},
                run_contexts={"r1": CTX}, trusted_context_hashes={"r1": CTX["sha256"]})
    a = out["aggregate_over_runs"]
    assert a["evidence_ownership_row"]["valid"] == 1 and a["evidence_ownership_row"]["unverifiable"] == 1
    assert a["overall"]["found"] == 1 and a["overall"]["T"] == 2


def test_evidence_missing():
    r = _ev([])
    assert r["evidence"]["no_evidence"] == 1 and r["overall"]["partial"] == 1


# ------------------------------------------------------------------ fail closed, aggregation, rates
def test_failed_job_keeps_denominator_and_missed():
    out = score({"FX1": label([MEAL])}, ART, {"r1": {"FX1": {"candidates": [good_meal()], "error": "timeout"}}})
    r = out["runs"]["r1"]
    assert r["overall"]["T"] == 1 and r["overall"]["missed"] == 1 and r["other"]["failed_jobs"] == 1


def test_empty_prediction_is_all_missed_not_failure():
    r = run1([MEAL, APPR], [])
    assert r["overall"]["missed"] == 2 and r["other"]["failed_jobs"] == 0 and r["other"]["spurious"] == 0


@pytest.mark.parametrize("bad", ["string", 5, None, {"candidates": "nope"}, {"candidates": None}])
def test_malformed_prediction_reported(bad):
    r = score({"FX1": label([MEAL])}, ART, {"r1": {"FX1": bad}})["runs"]["r1"]
    assert r["other"]["malformed_predictions"] == 1 and r["overall"]["missed"] == 1 and r["other"]["failed_jobs"] == 1
    assert r["artifacts"]["FX1"]["malformed_prediction"]


def test_missing_prediction_and_missing_run_stay_in_sample():
    r = score({"FX1": label([MEAL])}, ART, {"r1": {}})["runs"]["r1"]
    assert r["other"]["failed_jobs"] == 1 and r["overall"]["missed"] == 1
    out = score({"FX1": label([MEAL, APPR])}, ART, {"r1": {"FX1": {"candidates": [good_meal()]}}},
                expected_runs=["r1", "r2"])
    r2 = out["runs"]["r2"]
    assert out["run_errors"] == {"r2": "run_missing"} and r2["overall"]["T"] == 2 and r2["overall"]["missed"] == 2
    assert r2["other"]["failed_artifacts"] == ["FX1"] and out["aggregate_over_runs"]["overall"]["T"] == 4
    assert score({"FX1": label([MEAL])}, ART, {"r1": "garbage"})["run_errors"] == {"r1": "run_not_object"}


@pytest.mark.parametrize("bad", [
    "text", {"subject": "a", "predicate": "b"}, {"subject": "a", "predicate": "b", "value": 1, "modality": "soft"},
    {"subject": "a", "predicate": "b", "value": 1, "modality": "shall"},
    {"subject": "a", "predicate": "b", "value": 1, "modality": "prohibited"},
    {"subject": "a", "predicate": "b", "value": 1, "modality": None},
    {"subject": 5, "predicate": "b", "value": 1, "modality": "must"},
    {"subject": "a", "predicate": "b", "value": 1, "modality": "must", "evidence": "x"}])
def test_malformed_candidate_reported_not_dropped_not_spurious(bad):
    r = run1([MEAL], [good_meal(), bad])
    assert r["other"]["malformed_candidates"] == 1 and r["other"]["spurious"] == 0
    assert r["overall"]["found"] == 1 and r["artifacts"]["FX1"]["malformed_candidates"][0]["reason"]


def test_unknown_applicability_counted_never_found():
    r = run1([MEAL], [good_meal(applicability="UNKNOWN")])
    assert r["other"]["unknown_rejected"] == 1 and r["overall"]["found"] == 0 and r["other"]["spurious"] == 0


def test_partial_never_counted_as_found_in_class_text_or_aggregate():  # M33
    labels = {"FX1": label([MEAL])}
    runs = {"r1": {"FX1": {"candidates": [good_meal(scope={"region": "Osaka"})]}},
            "r2": {"FX1": {"candidates": [good_meal()]}}}
    out = score(labels, ART, runs)
    r1 = out["runs"]["r1"]
    assert r1["classes"]["C1"]["found"] == 0 and r1["classes"]["C1"]["partial"] == 1
    assert r1["text"]["C1"] == "C1: 0 of 1" and r1["overall"]["found"] == 0
    a = out["aggregate_over_runs"]
    assert a["overall"]["found"] == 1 and a["overall"]["partial"] == 1 and a["text"]["overall"] == "overall: 1 of 2"
    assert a["classes"]["C1"]["found"] == 1


def test_rates_strict_and_lenient_and_aggregate_recomputed():
    labels = {"FX1": label([MEAL, APPR])}
    runs = {"r1": {"FX1": {"candidates": [good_meal(),
                                          cand("approval", "approver", "director", kp="approval.approver"),
                                          cand("queue", "depth_max", 99)]}},
            "r2": {"FX1": {"candidates": [good_meal(scope={"region": "Osaka"})]}}}
    out = score(labels, ART, runs)
    p = out["runs"]["r1"]["rates"]["overall"]
    assert p["predictions_one_to_one"] == 3 and p["precision_strict"] == pytest.approx(1 / 3)
    assert p["recall_strict"] == 0.5 and p["recall_lenient"] == 0.5
    q = out["runs"]["r2"]["rates"]["overall"]
    assert q["recall_strict"] == 0 and q["recall_lenient"] == 0.5 and q["precision_strict"] == 0
    a = out["aggregate_over_runs"]["rates"]["overall"]
    assert a["recall_strict"] == 0.25 and a["recall_lenient"] == 0.5 and a["predictions_one_to_one"] == 4
    assert "strict" in out["formula"] and out["runs"]["r1"]["rates"]["C1"]["recall_strict"] == 1.0


def test_multi_run_per_run_and_aggregate_and_passthrough():
    labels = {"FX1": label([MEAL, APPR])}
    runs = {
        "r1": {"FX1": {"candidates": [good_meal(), cand("approval", "approver", "manager", kp="approval.approver")],
                       "latency_s": 2.0, "cost_usd": 0.01, "model": "m"}},
        "r2": {"FX1": {"candidates": [good_meal(), cand("approval", "approver", "director", kp="approval.approver")],
                       "latency_s": 3.0, "cost_usd": 0.02, "model": "m"}},
    }
    out = score(labels, ART, runs)
    assert out["runs"]["r1"]["overall"]["found"] == 2 and out["runs"]["r2"]["overall"]["found"] == 1
    a = out["aggregate_over_runs"]
    assert a["overall"]["found"] == 3 and a["overall"]["T"] == 4 and a["text"]["overall"] == "overall: 3 of 4"
    assert a["evidence"]["correct"] == 3
    assert out["runs"]["r1"]["cost_sum"] == 0.01 and out["runs"]["r2"]["latency_s_sum"] == 3.0
    assert out["runs"]["r1"]["artifacts"]["FX1"]["passthrough"]["model"] == "m"


def test_labels_never_derived_from_predictions():
    labels = {"FX1": label([MEAL])}
    before = copy.deepcopy(labels)
    runs = {"r1": {"FX1": {"candidates": [good_meal()], "gold_rules": [{"id": "X"}]},
                   "ZZ9": {"candidates": [good_meal()]}}}
    out = score(labels, ART, runs)
    assert labels == before and out["n_artifacts"] == 1 and out["runs"]["r1"]["overall"]["T"] == 1
    assert out["runs"]["r1"]["unlabelled_prediction_artifacts"] == ["ZZ9"]


# ------------------------------------------------------------------ v0.4: content_hash row, pinning, run binding
def test_content_hash_row_valid_wrong_missing():
    ok = _ev([_e(LOC)])
    assert ok["evidence_hash_row"]["valid"] == 1 and ok["overall"]["found"] == 1
    bad = _ev([_e(LOC, content_hash="sha256:" + "0" * 64)])
    assert bad["evidence"]["hash_mismatch"] == 1 and bad["evidence_hash_row"]["hash_mismatch"] == 1
    assert bad["overall"]["found"] == 0 and bad["overall"]["partial"] == 1
    for h in (None, "unknown", ""):  # production falls back to "unknown" when the blob hash is absent
        d = _e(LOC)
        d.pop("content_hash")
        if h is not None:
            d["content_hash"] = h
        r = _ev([d])
        assert r["evidence_hash_row"]["unverifiable"] == 1 and r["evidence"]["correct"] == 0
        assert r["overall"]["found"] == 0 and r["overall"]["T"] == 1
    no_exp = make_run_context("T1", "W1", ["SR1"], {"FX1": ["FX1"]})  # context without content-hash expectation
    r = _ev([_e(LOC)], ctx={"r": no_exp}, hashes={"r": no_exp["sha256"]})
    assert r["evidence_hash_row"]["unverifiable"] == 1 and r["overall"]["found"] == 0


def test_context_without_independent_hash_is_unpinned_not_valid():
    r = _ev([_e(LOC)], ctx={"r": CTX}, hashes=None)  # wrapper does not auto-fill for an explicit context
    assert r["context_state"] == "unpinned" and r["evidence"]["unverifiable"] == 1 and r["overall"]["found"] == 0


def test_context_built_for_another_run_is_rejected():
    other = make_run_context("T1", "W1", ["SR1"], {"FX1": ["FX1"]}, run_id="run-B", artifact_content_hashes=HASH)
    r = _ev([_e(LOC)], ctx={"r": other}, hashes={"r": other["sha256"]})
    assert r["context_state"] == "wrong_run" and r["evidence"]["context_tampered"] == 1
    assert r["overall"]["found"] == 0
    mine = make_run_context("T1", "W1", ["SR1"], {"FX1": ["FX1"]}, run_id="r", artifact_content_hashes=HASH)
    assert _ev([_e(LOC)], ctx={"r": mine}, hashes={"r": mine["sha256"]})["overall"]["found"] == 1


def test_context_file_edited_and_rehashed_is_rejected_by_independent_manifest(tmp_path):
    from pitch.eval.run_context import (load_contexts, load_manifest_hashes, persist_context, record_manifest_hash)
    ctx = make_run_context("T1", "W1", ["SR1"], {"FX1": ["FX1"]}, run_id="r", artifact_content_hashes=HASH)
    persist_context(tmp_path / "ctx", "r", ctx)
    record_manifest_hash(tmp_path / "manifest.json", "r", ctx["sha256"])  # separate file, written at import time
    # attacker edits the context file to admit a foreign revision AND recomputes its embedded hash
    forged = make_run_context("T1", "W1", ["SR1", "SR-FOREIGN"], {"FX1": ["FX1"]}, run_id="r",
                              artifact_content_hashes=HASH)
    persist_context(tmp_path / "ctx", "r", forged)
    assert load_contexts(tmp_path / "ctx")["r"]["sha256"] == forged["sha256"]  # self-hash is consistent
    r = _ev([_e(LOC, source_revision_id="SR-FOREIGN")], ctx=load_contexts(tmp_path / "ctx"),
            hashes=load_manifest_hashes(tmp_path / "manifest.json"))
    assert r["context_state"] == "tampered" and r["evidence"]["context_tampered"] == 1 and r["overall"]["found"] == 0


# ---- builder against a recorded product-shaped store (no DB); the real-import test is in test_run_context_db.py
class FakeScopeError(Exception):
    pass


class FakeStore:
    """Product-shaped source_revision_view; refuses a revision foreign to (tenant, workspace) like PostgresV2Store."""
    def __init__(self, tenant, ws, rev, files):
        self.t, self.w, self.rev = tenant, ws, rev
        self.view = {"artifacts": [{"artifact_id": f"art-{i}", "logical_path": p,
                                    "blob_hash": "sha256:" + hashlib.sha256(b).hexdigest()}
                                   for i, (p, b) in enumerate(files.items())]}

    def source_revision_view(self, tenant_id, workspace_id, revision_id):
        if (tenant_id, workspace_id, revision_id) != (self.t, self.w, self.rev):
            raise FakeScopeError("source revision not found")
        return self.view


def test_builder_from_recorded_import_shape(tmp_path):
    from pitch.eval.run_context import build_run_context_from_import, trusted_hash_from_store
    files = {"FX1.yaml": YAML.encode(), "FX2.md": MD.encode()}
    st = FakeStore("T1", "W1", "SR1", files)
    paths = {"FX1": "FX1.yaml", "FX2": "FX2.md"}
    ctx, h = build_run_context_from_import(st, "T1", "W1", "SR1", "r", paths, imported_files=files)
    assert ctx["tenant_id"] == "T1" and ctx["workspace_id"] == "W1" and ctx["run_id"] == "r"
    assert ctx["source_revision_ids"] == ["SR1"] and ctx["artifact_revision_ids"]["FX1"] == ["art-0"]
    assert ctx["artifact_content_hashes"] == HASH and h == trusted_hash_from_store(st, "T1", "W1", "SR1", "r", paths)
    with pytest.raises(FakeScopeError):
        build_run_context_from_import(st, "T2", "W1", "SR1", "r", paths)   # other tenant
    with pytest.raises(FakeScopeError):
        build_run_context_from_import(st, "T1", "W2", "SR1", "r", paths)   # other workspace
    with pytest.raises(ValueError):
        build_run_context_from_import(st, "T1", "W1", "SR1", "r", {"FX1": "nope.yaml"})
    with pytest.raises(ValueError):  # imported bytes differ from the store blob_hash
        build_run_context_from_import(st, "T1", "W1", "SR1", "r", paths, imported_files={**files, "FX1.yaml": b"x"})


def test_product_result_shape_has_no_content_hash_policy_switch():
    """The product result dict drops EvidenceRef.content_hash (infra/worker/extraction.py serialises only locator,
    quote, parser_version) and puts revision ids at candidate level. Default `reported_only`: row reported, a present wrong
    hash is still rejected, absence does not gate strict. `required` gates it (unverifiable)."""
    d = _e(LOC)
    for k in ("content_hash", "artifact_revision_id", "source_revision_id"):
        d.pop(k)
    kw = {"artifact_revision_id": "FX1", "source_revision_id": "SR1"}  # candidate level, as the product emits
    c = good_meal()
    c.update(kw)
    c["evidence"] = [d]
    lab = {"FX1": label([MEAL])}
    ok = score(lab, ART, {"r": {"FX1": {"candidates": [c]}}})
    assert ok["content_hash_policy"] == "reported_only"
    r = ok["runs"]["r"]
    assert r["overall"]["found"] == 1 and r["evidence_hash_row"]["unverifiable"] == 1
    req = score(lab, ART, {"r": {"FX1": {"candidates": [c]}}}, content_hash_policy="required")["runs"]["r"]
    assert req["overall"]["found"] == 0 and req["overall"]["partial"] == 1
    with pytest.raises(ValueError):
        score(lab, ART, {"r": {}}, content_hash_policy="nope")
