"""checks/31 (2026-10-30; acceptance v8 observation H8-33 / H8-38, not a gate). Pure: no DB, no model. A kept limitation whose text restates a pack fact
verbatim (case, spacing and the final period aside) is shown with that fact as its provenance (`limitation_support`, basis `fact_restated`); a limitation that
only shares words with a fact, adds a claim to it, or is refused carries no such support; the model's words are unchanged; an honest-absence support keeps its
own basis. Expected outcomes written by hand from the fixture records."""
from __future__ import annotations

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_context13 import WAIT
from tests.unit.test_request_assistant_integrity import fid


def _support(out):
    return {s["text"]: s for s in out.get("limitation_support") or []}


def _fact(pack, key):
    return next(f for f in pack["facts"] if f.get("key") == key)


def test_a_verbatim_restatement_of_a_pack_fact_carries_that_fact():
    later = _fact(WAIT, "steps:later:holders")
    text = later["text"]
    out = run(WAIT, [("what changed", "change", [blk("The current step has not started yet.", [fid(WAIT, "state")])])], question="Who acts later?", limitations=[text])
    assert text in out["limitations"], out["rejected"]
    sup = _support(out)
    assert sup[text]["basis"] == "fact_restated" and sup[text]["fact_refs"] == [later["id"]] and sup[text]["text"] == text
    upper = text.upper().rstrip(".") + "  "
    out = run(WAIT, [("what changed", "change", [blk("The current step has not started yet.", [fid(WAIT, "state")])])], question="Who acts later?", limitations=[upper])
    kept = [x for x in out["limitations"] if x.strip().lower().rstrip(".") == text.lower().rstrip(".")]
    assert kept and _support(out)[kept[0]]["fact_refs"] == [later["id"]], "case, spacing and the final period do not matter; the model's own words are kept"


def test_sharing_words_with_a_fact_or_adding_a_claim_is_not_a_restatement():
    later = _fact(WAIT, "steps:later:holders")
    partial = later["text"].rstrip(".").split(" is ")[0] + " is unknown to me."
    added = later["text"].rstrip(".") + ", so nobody is responsible."
    out = run(WAIT, [("what changed", "change", [blk("The current step has not started yet.", [fid(WAIT, "state")])])], question="Who acts later?", limitations=[partial, added])
    sup = _support(out)
    assert all(s.get("basis") != "fact_restated" for s in sup.values()), sup


def test_a_refused_limitation_carries_nothing_and_an_honest_absence_keeps_its_own_basis():
    out = run(WAIT, [("what changed", "change", [blk("The current step has not started yet.", [fid(WAIT, "state")])])], question="Who acts later?",
              limitations=["The request is blocked and nobody will approve it."])
    assert out["rejected"] and _support(out) == {}
    later = "The facts do not record who acts on later steps until each step starts."
    out = run(WAIT, [("what changed", "change", [blk("The current step has not started yet.", [fid(WAIT, "state")])])], question="Who acts later?", limitations=[later])
    sup = _support(out)
    assert later in sup and sup[later]["basis"] == "later_step_holders"


def test_fact_restatement_helper_and_version():
    facts = [{"id": "F1", "text": "Who acts on each later step is recorded only when that step starts."}, {"id": "F2", "text": "Other."}]
    assert ra.fact_restatement("who acts on each later step is recorded only when that step starts", facts) == ["F1"]
    assert ra.fact_restatement("Who acts on each later step is recorded only when that step starts, usually.", facts) == []
    assert ra.fact_restatement("", facts) == []
    assert ra.CHECKS_VERSION == "request-assistant-checks/31" and "request-assistant-checks/30" in ra.COMPATIBLE_CHECKS
