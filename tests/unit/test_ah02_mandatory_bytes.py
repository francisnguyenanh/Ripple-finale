"""AH-02.3: mandatory facts whose total text is one character above the pack budget are refused before any provider call; exactly at the budget they are kept
whole; optional history never displaces them."""
import pytest

from core.change_intelligence.v2 import request_assistant as ra

REF = {"family": "business_change", "id": "chg-x"}


def _mandatory(total_chars):
    per = ra.MAX_FACT_CHARS - 10
    n, rest = divmod(total_chars, per)
    texts = ["B" * per for _ in range(n)] + (["B" * rest] if rest else [])
    assert len(texts) <= ra.MAX_FACTS
    return [ra.fact("blocker", t, "task_view", tier="mandatory", key=f"blocker:{i:03d}") for i, t in enumerate(texts)]


def test_mandatory_bytes_one_above_the_budget_are_refused_and_at_the_budget_kept():
    with pytest.raises(ra.ContextOverflow) as e:
        ra.pack(REF, "now", _mandatory(ra.MAX_PACK_CHARS + 1), auth={})
    assert e.value.reason == "mandatory_facts_exceed_bounds"
    history = [ra.fact("review_note", f"A reviewer's note (comment): {i}", "review_note", tier="history", key=f"review_note:{i}") for i in range(2000)]
    p = ra.pack(REF, "now", _mandatory(ra.MAX_PACK_CHARS) + history, auth={})
    kept = [f for f in p["facts"] if f["kind"] == "blocker"]
    assert sum(len(f["text"]) for f in kept) == ra.MAX_PACK_CHARS and p["coverage"]["tiers"]["mandatory"]["omitted"] == 0
    assert p["coverage"]["tiers"]["history"]["omitted"] == 2000                     # no room left: optional history is the part omitted, and counted
