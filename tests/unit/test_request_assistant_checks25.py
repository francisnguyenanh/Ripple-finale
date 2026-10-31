"""checks/25 (R21, live A160): an exact server-owned position quotation filed under a part other than "obligation" is server content (shown as task record,
never refused as model authority prose and never credited to the model) and does not answer that part; "past" marks a no-longer-current decision as past."""
from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_integrity import fid, ob, pack_of, tv
from tests.unit.test_request_stage_phase_and_reason import O, view


def test_an_exact_position_quote_under_next_is_task_record_and_the_next_part_stays_open():
    p = pack_of(tv([ob(me=False)]))
    out = run(p, [("Will it land with me?", "next", [blk("No current step of this request is yours.", [fid(p, "position")])])])
    assert out["rejected"] == []                                                               # A160 H3-04: refused as contradicts_cited_obligation before
    assert [b.get("origin") for b in out["blocks"] if b.get("ask") == 0] == ["task_record"]
    cov = out["parts"]["coverage"][0]
    assert cov["blocks"] == 0 and "record_blocks" not in cov                                   # not model credit, not an answer to "next"
    assert out["status"] == "refused" and ra.open_parts(out, p["facts"])                        # nothing of the model answered the part


def test_a_quote_with_added_or_reversed_authority_under_next_is_still_refused():
    p = pack_of(tv([ob(me=False)]))
    for text in ("The current step of this request is yours.", "No current step of this request is yours, but you must approve it."):
        out = run(p, [("Will it land with me?", "next", [blk(text, [fid(p, "position")])])])
        assert out["rejected"] and not any(b.get("ask") == 0 for b in out["blocks"]), text


def test_past_marks_a_no_longer_current_decision_and_an_unmarked_cause_is_still_refused():
    waiting = view({"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "current"},
                   [O("decide_knowledge", "Decide the knowledge change", holder="approver", holder_label="an approver", eligible_me=False)])
    old = ra.fact("decision", "knowledge decision: request_changes, not current (the draft was revised after this decision); reason given: state the scope.", "decision",
                  tier="history", key="decision:1")
    p = ra.pack({"family": "business_change", "id": "x"}, "now", ra.task_facts(waiting) + [old], auth={})
    ref = next(f["id"] for f in p["facts"] if f["key"] == "decision:1")

    def check(text):
        return [r["reason"] for r in run(p, [("Was a reason given?", "reason", [blk(text, [ref], "cause")])])["rejected"]]
    assert check("The reason given on the past return was: state the scope.") == []          # A160 H3-02
    assert check("It is at this stage because changes were requested to state the scope.") == ["outdated_decision_as_cause"]
    assert check("It went past the reviewer because changes were requested.") == []           # "past" is a lexical mark only; the evaluator still rates meaning


def test_context8_the_stage_fact_states_an_empty_blocker_list_and_only_then():
    from infra.adapters import v2_request_tasks as T
    state = lambda tv: next(f["text"] for f in ra.task_facts(tv) if f["kind"] == "state")      # noqa: E731
    waiting = view({"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "current"},
                   [O("decide_knowledge", "Decide", holder="approver", holder_label="an approver", eligible_me=False)])
    assert state(waiting).endswith(" No blocker is recorded on this request.")
    blocked = view({"understand": "done", "draft": "done", "clarify": "blocked", "knowledge_decision": "blocked"}, blockers=[{"code": "knowledge_moved", "text": "moved"}])
    assert "No blocker" not in state(blocked)
    stuck = view({"understand": "done", "draft": "done", "clarify": "blocked"})                                   # a blocked step without a blocker entry
    assert "No blocker" not in state(stuck)
    assert ra.CONTEXT_VERSION == "request-assistant-context/14" and T is not None


def test_config25_the_answer_calls_deadline_is_the_p95_line_minus_overhead_and_the_recovery_keeps_its_time():
    from infra.worker import request_assistant as RQA
    frozen, _ = RQA.load_config()
    rec = frozen["recovery"]
    assert frozen["version"] == 32 and rec["primary_deadline_s"] == 19 and rec["min_left_s"] == 8
    assert 45 - rec["primary_deadline_s"] - 1.0 >= 2 * rec["min_left_s"]          # the job's 45 s leaves the recovery well above its minimum (grace counted)


def test_context9_the_later_steps_fact_says_when_who_acts_is_recorded():
    tv = view({"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "current"},
              [O("decide_knowledge", "Decide", holder="approver", holder_label="an approver", eligible_me=False)])
    holders = next(f for f in ra.task_facts(tv) if f.get("key") == "steps:later:holders")
    assert holders["text"] == "Who acts on each later step is recorded only when that step starts." and holders["tier"] == "mandatory"
    done = view({"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "done", "commit": "done", "file_decision": "done",
                 "source_apply": "done", "readback": "done"})
    assert not any(f.get("key") == "steps:later:holders" for f in ra.task_facts(done))       # only when later steps exist
    assert all(o.get("state") != "future" for o in tv["obligations"])                       # true by construction: no obligation exists for a step not started


def test_checks26_the_passive_who_acts_is_not_shown_is_authority_prose_and_provenance_is_not():
    for t in ("Who will move the request next is not shown.", "It is not shown who moves the request forward.", "Who acts next is not recorded.",
              "It is not stated in the record who approves it."):
        assert ra.free_text_authority(t) == "authority_in_free_text", t
    for t in ("Who raised the blocker is not recorded.", "Who wrote the draft is not shown.", "When it last changed is not shown.",
              "Who the affected customers are is not stated."):
        assert ra.free_text_authority(t) is None, t
    assert ra.CHECKS_VERSION == "request-assistant-checks/31" and "request-assistant-checks/25" in ra.COMPATIBLE_CHECKS
