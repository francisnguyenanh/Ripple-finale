"""SUR-05 (gap closed 2026-10-31): the server-side refusals of a correspondence proposal that had no dedicated test: the pair cap, a duplicate pair, a rationale that is
not plain text, an over-long rationale, a mislabelled no_counterpart, and the schema refusal of too many pairs. Pure functions, no I/O, no provider."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import source_correspondence as sc

LIMITS = {"max_pairs": 2, "max_rationale_chars": 40, "max_notes": 3}
RULE = {"rule_id": "rule-a", "title": "Manager review above 1000000 JPY"}
OLD = [sc.side("docs/old.md", "Purchases above 1000000 JPY need a manager.", hash_="sha256:aa", max_chars=200, rules=[RULE])]
NEW = [sc.side("docs/new.md", "A manager reviews purchases above 1000000 JPY.", hash_="sha256:bb", max_chars=200)]


def pair(**over):
    base = {"old_path": "docs/old.md", "new_path": "docs/new.md", "relation": "moved_rewritten", "rule_ids": ["rule-a"], "old_quote": "above 1000000 JPY need a manager",
            "new_quote": "A manager reviews purchases above 1000000 JPY", "rationale": "The same obligation, reworded.", "confidence": "high"}
    base.update(over)
    return base


def reasons(pairs):
    out = sc.check({"status": "pairs", "pairs": pairs, "notes": []}, OLD, NEW, LIMITS)
    return [r["reason"] for r in out["rejected"]], len(out["kept"])


def test_pair_cap_refuses_the_third_distinct_pair_and_keeps_the_first_two():
    pairs = [pair(), pair(relation="split"), pair(relation="merged")]
    assert reasons(pairs) == (["over_pair_cap"], 2)


def test_duplicate_pair_is_refused_after_the_first():
    assert reasons([pair(), pair(rationale="Said twice.")]) == (["duplicate"], 1)


@pytest.mark.parametrize("rationale", ["<b>bold</b>", "see `code`", "a | table", "**strong**", "# heading", "[x](y)", "https://example.test", "", "   "])
def test_markup_links_and_empty_rationales_are_not_plain_text(rationale):
    assert reasons([pair(rationale=rationale)]) == (["not_plain_text"], 0)


def test_over_long_rationale_is_refused_whole_never_trimmed():
    assert reasons([pair(rationale="x" * 41)]) == (["not_plain_text"], 0)
    assert reasons([pair(rationale="x" * 40)]) == ([], 1)


def test_no_counterpart_with_a_new_path_and_a_missing_new_path_are_refused():
    assert reasons([pair(relation="no_counterpart")]) == (["no_counterpart_with_new_path"], 0)
    assert reasons([pair(new_path="")]) == (["new_path_not_offered"], 0)
    assert reasons([pair(relation="no_counterpart", new_path="", new_quote="")]) == ([], 1)


def test_schema_refuses_too_many_pairs_before_any_check():
    with pytest.raises(ValueError, match="too many pairs"):
        sc.validate_model_object({"status": "pairs", "pairs": [pair()] * 7, "notes": []}, LIMITS)
    assert sc.validate_model_object({"status": "pairs", "pairs": [pair()] * 6, "notes": []}, LIMITS)["status"] == "pairs"
