"""Record quotations are server contributions, never model authority or usefulness."""
import pytest
from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import run, blk
from tests.unit.test_request_assistant_integrity import pack_of, tv, ob, fid


def test_exact_position_quote_is_record_content_and_does_not_trigger_completion():
    p = pack_of(tv([ob(me=False)]))
    text = "No current step of this request is yours."
    out = run(p, [("What do I owe?", "obligation", [blk(text, [fid(p, "position")])])])
    assert out["rejected"] == []
    assert out["status"] == "answered"
    assert not ra.open_parts(out, p["facts"])
    copies = [b for b in out["blocks"] if b.get("ask") == 0]
    assert copies and all(b.get("origin") == "task_record" for b in copies)
    assert out["parts"]["coverage"][0]["blocks"] == 0  # model-only remains zero
    assert out["parts"]["coverage"][0]["record_blocks"] == 1


@pytest.mark.parametrize("text", [
    "The current step of this request is yours.",
    "No current step of this request is yours, but you must approve it.",
    "No current step of this request is yours. You must approve it.",
    "No current step of this request is yours because you must approve it.",
])
def test_extra_or_reversed_authority_is_not_a_record_quote(text):
    p = pack_of(tv([ob(me=False)]))
    out = run(p, [("What do I owe?", "obligation", [blk(text, [fid(p, "position")])])])
    assert out["rejected"]
    assert not any(b.get("ask") == 0 for b in out["blocks"])


def test_negative_quote_must_match_this_readers_current_record_and_reference():
    for p, kind in [(pack_of(tv([ob(me=True)])), "position"),
                    (pack_of(tv([ob(me=False)])), "state")]:
        out = run(p, [("What do I owe?", "obligation", [blk("No current step of this request is yours.", [fid(p, kind)])])])
        assert out["rejected"]


def test_record_copy_cannot_answer_an_explanation_part():
    p = pack_of(tv([ob(me=False)]))
    out = run(p, [("Why?", "reason", [blk("No current step of this request is yours.", [fid(p, "position")])])])
    assert out["parts"]["coverage"][0]["blocks"] == 0
    assert ra.open_parts(out, p["facts"])


def test_full_position_fact_is_record_content_not_model_prose():
    p = pack_of(tv([ob(me=False)]))
    f = next(f for f in p["facts"] if f["kind"] == "position")
    out = run(p, [("What do I owe?", "obligation", [blk(f["text"], [f["id"]])])])
    assert out["rejected"] == []
    assert out["status"] == "answered"
    assert all(b.get("origin") == "task_record" for b in out["blocks"])


def test_record_quote_does_not_hide_an_unanswered_explanation_and_merge_can_fill_it():
    p = pack_of(tv([ob(me=False)]), extra=[ra.fact("request", "The confirmed request: raise the freight threshold.", "review", key="request:confirmed")])
    first = run(p, [("What do I owe?", "obligation", [blk("No current step of this request is yours.", [fid(p, "position")])]),
                    ("What is proposed?", "change", [])])
    assert first["status"] == "partial"
    opened = ra.open_parts(first, p["facts"])
    assert len(opened) == 1 and opened[0]["part"] == "change"
    second = run(p, [("What is proposed?", "change", [blk("The request proposes to raise the freight threshold.", [next(f["id"] for f in p["facts"] if f["key"] == "request:confirmed")])])])
    merged = ra.merge_supplement(first, second, opened, "completed", p["facts"])
    assert merged["status"] == "answered"
    assert merged["parts"]["supplement"]["blocks_added"] == 1
    assert sum(b.get("origin") != "task_record" for b in merged["blocks"]) == 1
    assert not any(l.startswith("Not answered") for l in merged["limitations"])
    unavailable = ra.merge_supplement(first, None, opened, "timeout", p["facts"])
    assert unavailable["status"] == "partial"
    assert any(l.startswith("Not answered") for l in unavailable["limitations"])


def test_exact_positive_position_uses_server_provenance_and_unknown_refs_still_fail():
    p = pack_of(tv([ob(me=True)]))
    f = next(f for f in p["facts"] if f["kind"] == "position")
    kept = run(p, [("What do I owe?", "obligation", [blk(f["text"], [f["id"]])])])
    assert kept["status"] == "answered" and kept["parts"]["coverage"][0]["blocks"] == 0
    denied = run(p, [("What do I owe?", "obligation", [blk(f["text"], [f["id"], "F9999"])])])
    assert denied["rejected"] and not any(b.get("ask") == 0 for b in denied["blocks"])


def test_complete_position_head_is_also_a_canonical_record_segment():
    for mine in (False, True):
        p = pack_of(tv([ob(me=mine)]))
        f = next(f for f in p["facts"] if f["kind"] == "position")
        head = f["text"].split(" Decisions and actions", 1)[0]
        out = run(p, [("What do I owe?", "obligation", [blk(head, [f["id"]])])])
        assert out["rejected"] == []
        assert out["status"] == "answered"
        assert out["parts"]["coverage"][0]["blocks"] == 0
        assert out["parts"]["coverage"][0]["record_blocks"] == 1


def test_pure_execution_decline_is_policy_owned_not_a_claim_about_missing_controls():
    p = pack_of(tv([ob(me=True)]))
    bad = "The facts do not show a mechanism to send the change back or flag it for revision."
    out = run(p, [("Send it back", "action", [])], limitations=[bad], outcome="partial")
    assert out["status"] == "refused"
    assert bad not in out["limitations"]
    assert out["policy_refusal"]["origin"] == "assistant_policy"
    assert out["policy_refusal"]["model_limitations_withheld"] == 1
    assert out["limitations"] == [ra.ACTION_REFUSAL]
    assert out["parts"]["coverage"][0]["blocks"] == 0
    assert not ra.open_parts(out, p["facts"])


def test_policy_decline_does_not_replace_information_or_a_how_question():
    p = pack_of(tv([ob(me=True)]))
    for asks in ([("How do I send it back?", "how", [])],
                 [("Send it back", "action", []), ("Why?", "reason", [])]):
        out = run(p, asks, limitations=["The reason is not recorded."], outcome="partial")
        assert "policy_refusal" not in out
        assert "The reason is not recorded." in out["limitations"]
        assert ra.open_parts(out, p["facts"])
