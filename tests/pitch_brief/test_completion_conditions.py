"""Pure checks of what lets "Accept result" be pressed (no database): the named conditions of `gate_reasons`, and which AI roles carry a thinking limit.
  * all conditions met -> no reason (the button is offered)
  * a serious finding on a touched place, or a reading of the changed file nobody has checked -> a named reason
  * findings that merely need review do NOT stop the decision (they stay visible and count in the knowledge status)
  * the thinking limit exists for the draft call only; no other role's call carries it"""
from __future__ import annotations

import pathlib
import re

from infra.adapters import v2_change as C


def _facts(**over):
    f = {"published": True, "stage_conflict": [], "report": {"report_digest": "d"}, "open_gaps": [], "findings_view": {"unchecked_related": [], "needs_review": [], "blocking": []}}
    f.update(over)
    return f


def _analysis(**over):
    a = {"stale": False, "machine_ok": True, "manual_open": [], "reanalysis": {"status": "COMPLETED"},
         "knowledge": {"accepted": True, "retrieval_index_status": "READY", "km_index_status": "READY"}}
    a.update(over)
    return a


def test_all_conditions_met_gives_no_reason():
    assert C.gate_reasons(_facts(), _analysis()) == []


def test_a_serious_finding_on_a_touched_place_is_a_named_reason():
    assert "critical_gap_open" in C.gate_reasons(_facts(open_gaps=["finding-1"]), _analysis())


def test_an_unchecked_reading_of_the_changed_file_is_a_named_reason():
    f = _facts(findings_view={"unchecked_related": [{"candidate_id": "c"}], "needs_review": [], "blocking": []})
    assert C.gate_reasons(f, _analysis()) == ["findings_not_rechecked"]


def test_findings_that_only_need_review_do_not_stop_the_decision():
    f = _facts(findings_view={"unchecked_related": [], "needs_review": [{"finding_id": "x"}] * 29, "blocking": []})
    assert C.gate_reasons(f, _analysis()) == []                                  # they are shown and counted in the knowledge status instead


def test_the_other_gates_still_hold_independently():
    assert "knowledge_not_accepted" in C.gate_reasons(_facts(), _analysis(knowledge={"accepted": False, "retrieval_index_status": "READY", "km_index_status": "READY"}))
    assert "machine_verification_not_pass" in C.gate_reasons(_facts(), _analysis(machine_ok=False))
    assert "reanalysis_incomplete" in C.gate_reasons(_facts(), _analysis(reanalysis={"status": "RUNNING"}))


def test_only_the_draft_call_carries_a_thinking_limit():
    root = pathlib.Path(__file__).resolve().parents[2] / "infra" / "worker"
    carrying = sorted(p.name for p in root.glob("*.py") if re.search(r"thinking_config", p.read_text(encoding="utf-8")))
    assert carrying == ["change_draft.py"], carrying                              # a limit on another role would change its answers without anyone deciding it


# ---- findings that MAY affect the change are named by the person who accepts the result (no database)
class _Cur:
    """Scripted cursor: a proposal that edits one key, and its impact analysis (one direct reading, one file an AI judged as depending on it, one text-scan hint)."""

    def __init__(self, impact):
        self.impact, self.row = impact, None

    def execute(self, sql, params=()):
        if "kind='proposal'" in sql:
            self.row = ({"target_artifact_id": "A"}, {"target_artifact_id": "A", "edits": [{"path": "rules.limit"}], "impact_job_id": "I"})
        elif "kind='impact'" in sql:
            self.row = (self.impact,)
        else:
            self.row = None

    def fetchone(self):
        return self.row


def _claim(cid, art, key, applicability="UNKNOWN"):
    return {"job": "j-" + art, "artifact": art, "pin": "r0", "valid": True,
            "c": {"candidate_id": cid, "subject": key, "applicability": applicability, "evidence": [{"locator": {"type": "key_path", "key_path": key}, "quote": "q"}]}}


def _finding(fid, cid, art, severity="HIGH"):
    return {"finding_id": fid, "kind": "GAP", "reason": "missing_scope", "severity": severity, "authority_required": True, "involved_claim_ids": [cid], "state": "current", "resolved": False,
            "runs": 1, "claims": [{"candidate_id": cid, "artifact_id": art, "subject": cid, "logical_path": art + ".py"}], "plain": "p", "todo": "t"}


def test_the_relation_to_a_change_tells_blocking_possible_effect_and_undetermined_apart():
    from infra import v2_current_findings as CF
    m = CF.Model()
    m.claims = {"edit": _claim("edit", "A", "rules.limit"), "dep": _claim("dep", "B", "other.key"), "far": _claim("far", "C", "far.key"), "farlow": _claim("farlow", "C", "far.low")}
    m.current = [_finding("f-edit", "edit", "A"), _finding("f-dep", "dep", "B"), _finding("f-far", "far", "C"), _finding("f-dep-low", "dep", "B", severity="LOW")]
    impact = {"direct": [], "indirect": [], "indirect_model": [{"artifact_id": "B", "evidence_quote": "rules.limit"}]}
    rel = CF.classify_for_change(_Cur(impact), "t", "w", m, "P", ["A"])
    assert [v["finding_id"] for v in rel["blocking"]] == ["f-edit"]                                  # on the edited place: blocks
    assert sorted(v["finding_id"] for v in rel["possible_effect"]) == ["f-dep", "f-dep-low"]       # evidence it may depend on the change: named by the decider, whatever severity (modality) the AI gave
    assert [v["finding_id"] for v in rel["needs_review"]] == ["f-far"]                                # no evidence either way: shown, counted, never called unrelated


def _need_facts(possible, same=()):
    f = _facts(findings_view={"unchecked_related": [], "needs_review": [], "blocking": [], "possible_effect": possible, "same_concept": list(same)})
    f["decision_basis"] = "dbasis-now"
    return f


def _G(f, d, basis="dbasis-now", raw=None):
    return C.gate_reasons(f, _analysis(), dispositions=C.normalise_dispositions(d), basis_given=basis, duplicates=C.duplicate_ids(raw if raw is not None else d))


def test_each_possible_effect_item_needs_its_own_disposition_and_a_reason_where_a_limit_is_accepted():
    f = _need_facts([{"finding_id": "a"}, {"finding_id": "b"}], [{"item_id": "reading:x"}])
    full = [{"item_id": "a", "disposition": "accepted_limit", "reason": "accepted for this request: documented"}, {"item_id": "b", "disposition": "unhandled"},
            {"item_id": "reading:x", "disposition": "judged_unrelated", "reason": "a different control, owned elsewhere"}]
    assert "items_without_disposition" in _G(f, None)                                                  # a single tick can not stand for the decisions
    assert "items_without_disposition" in _G(f, full[:2])                                              # every item, including a reading elsewhere
    assert _G(f, full) == []
    assert "disposition_reason_required" in _G(f, [{**full[0], "reason": "ok"}] + full[1:])           # input validation only: it never replaces the other conditions
    assert "items_without_disposition" in _G(f, [{**full[0], "disposition": "whatever"}] + full[1:])
    assert "items_without_disposition" not in C.gate_reasons(f, _analysis(), for_view=True)           # the page offers the decision; the server insists on the item-by-item answers


def test_the_server_refuses_forged_unknown_duplicate_and_stale_decisions():
    f = _need_facts([{"finding_id": "a"}])
    ok = [{"item_id": "a", "disposition": "unhandled"}]
    assert _G(f, ok) == []
    assert "disposition_unknown_item" in _G(f, ok + [{"item_id": "other-workspace-finding", "disposition": "unhandled"}])   # an id outside this request is never ignored
    assert "disposition_duplicate_item" in _G(f, ok, raw=ok + [{"item_id": "a", "disposition": "accepted_limit", "reason": "x" * 12}])
    assert "decision_basis_stale" in _G(f, ok, basis="dbasis-earlier")                                  # the state the person reviewed is not the state in force
    assert "decision_basis_stale" in _G(f, ok, basis=None)
    assert "disposition_unknown_item" in _G(_need_facts([]), ok)                                       # nothing needs a decision: a forged entry is refused, not dropped


def test_a_critical_item_cannot_be_waved_through_with_a_reason():
    f = _need_facts([{"finding_id": "a", "severity": "CRITICAL"}])
    f["findings_view"]["possible_effect"][0].update({"claims": [], "kind": "GAP"})
    assert "disposition_not_allowed_critical" in _G(f, [{"item_id": "a", "disposition": "accepted_limit", "reason": "accepted for this request: documented"}])
    assert "disposition_not_allowed_critical" in _G(f, [{"item_id": "a", "disposition": "judged_unrelated", "reason": "accepted for this request: documented"}])
    assert _G(f, [{"item_id": "a", "disposition": "unhandled"}]) == []                                # narrow scope: allowed, and the blockers still apply on their own


def test_a_narrow_scope_never_passes_a_mandatory_blocker():
    f = _need_facts([{"finding_id": "a"}])
    f["open_gaps"] = ["g1"]
    assert "critical_gap_open" in _G(f, [{"item_id": "a", "disposition": "unhandled"}])


def test_leaving_an_item_unhandled_is_allowed_but_the_scope_is_narrow():
    f = _need_facts([{"finding_id": "a"}])
    given = C.normalise_dispositions([{"item_id": "a", "disposition": "unhandled"}])
    assert _G(f, [{"item_id": "a", "disposition": "unhandled"}]) == []
    need = C.items_needing_disposition(f)
    assert C.acceptance_scope_of(need, given) == "NARROW"
    full = C.normalise_dispositions([{"item_id": "a", "disposition": "accepted_limit", "reason": "accepted for this request, reviewed"}])
    assert C.acceptance_scope_of(need, full) == "FULL"


def test_the_same_kind_of_limit_elsewhere_is_found_by_shared_concept_not_by_an_edge_or_a_shared_number():
    from infra import v2_current_findings as CF
    m = CF.Model()
    m.paths = {"A": "x/config.yaml", "G": "x/guide.md", "P": "x/policy.md", "U": "x/unrelated.md", "S": "x/same_value.md"}
    mk = lambda cid, art, subj, val, quote, scope=None: {"job": "j", "artifact": art, "pin": "r", "valid": True, "c": {"candidate_id": cid, "subject": subj, "predicate": "p", "value": val, "scope": scope or {}, "evidence": [{"locator": {"key_path": cid}, "quote": quote}]}}
    m.claims = {"edit": mk("edit", "A", "rules.capex_finance_threshold_jpy", 1500000, "rules.capex_finance_threshold_jpy: 1500000"),
                "guide": mk("guide", "G", "purchasing commitments", 2000000, "all purchasing commitments of 2,000,000 JPY or higher must be reviewed by the Finance Department"),
                "policy": mk("policy", "P", "capital equipment", "Finance Department", "Capital equipment of 2,000,000 JPY requires formal review", {"type": "CAPEX"}),
                "other": mk("other", "U", "freight charge", 2000000, "Freight is 2,000,000 JPY for heavy parcels"),                      # same number, different concept
                "done": mk("done", "S", "capex finance review", 1000000, "CAPEX finance review above 1,000,000 JPY")}                     # already states the new value
    class Cur:
        def execute(self, sql, params=()):
            self.row = (({"target_artifact_id": "A"}, {"target_artifact_id": "A", "intent_id": "i", "edits": [{"path": "rules.capex_finance_threshold_jpy", "before": 1500000, "after": 1000000}], "impact_job_id": None})
                        if "kind='proposal'" in sql else ("CAPEX Finance review threshold",) if "v2_intents" in sql else None)
        def fetchone(self):
            return self.row
    rel = CF.classify_for_change(Cur(), "t", "w", m, "P", ["A"])
    found = {x["candidate_id"]: x for x in rel["same_concept"]}
    assert set(found) == {"guide", "policy"}                                                          # concept + a number, nothing else; the matching number alone ("other") is not a reason; the new value is already consistent
    assert found["guide"]["stance"] == "different_value" and {"finance", "review"} <= set(found["guide"]["shared_words"])


def test_on_the_edited_place_the_ai_assigned_severity_cannot_lower_the_bar():
    from infra import v2_current_findings as CF
    m = CF.Model()
    mk = lambda cid, mod: {"job": "j", "artifact": "A", "pin": "r", "valid": True, "c": {"candidate_id": cid, "subject": "rules.limit", "modality": mod, "applicability": "UNKNOWN", "evidence": [{"locator": {"key_path": "rules.limit"}, "quote": "q"}]}}
    m.claims = {"c1": mk("c1", "observed")}
    low = {**_finding("f-low", "c1", "A", severity="LOW"), "authority_required": False}
    m.current = [low]
    rel = CF.classify_for_change(_Cur({"direct": [], "indirect": [], "indirect_model": []}), "t", "w", m, "P", ["A"])
    assert [v["finding_id"] for v in rel["blocking"]] == ["f-low"]                                  # a LOW reading of the rule being edited still blocks until a person decides


def test_changing_the_modality_cannot_make_a_mandatory_check_vanish():
    """modality must -> HIGH, may -> MEDIUM, anything else (e.g. observed) -> LOW: that is only the SEVERITY label. What the AI reads as `observed` instead of `must` stays visible and decidable."""
    from infra.worker.findings import _severity_and_authority as sev
    assert sev(["must"])[0] == "HIGH" and sev(["observed"])[0] == "LOW" and sev([])[0] == "LOW"
    from infra import v2_current_findings as CF
    for modality_severity in ("HIGH", "LOW"):
        m = CF.Model()
        m.claims = {"edit": _claim("edit", "A", "rules.limit"), "dep": _claim("dep", "B", "other.key")}
        m.current = [_finding("f-edit", "edit", "A", severity=modality_severity), _finding("f-dep", "dep", "B", severity=modality_severity)]
        impact = {"direct": [], "indirect": [], "indirect_model": [{"artifact_id": "B", "evidence_quote": "rules.limit"}]}
        rel = CF.classify_for_change(_Cur(impact), "t", "w", m, "P", ["A"])
        assert [v["finding_id"] for v in rel["blocking"]] == ["f-edit"]                                 # the edited place blocks at either severity
        assert [v["finding_id"] for v in rel["possible_effect"]] == ["f-dep"]                           # evidence of a possible effect needs a person's decision at either severity


def test_the_decision_card_names_the_question_the_options_the_deciders_and_hides_source_text_from_roles_without_the_right():
    v = {**_finding("f1", "c1", "A", severity="HIGH"), "claims": [{"candidate_id": "c1", "artifact_id": "A", "subject": "routing_rules.capex_finance_threshold_jpy", "logical_path": "x/approval.yaml"}]}
    ev = [{"file": "approval.yaml", "rule": "routing_rules.capex_finance_threshold_jpy", "locator": "routing_rules.capex_finance_threshold_jpy", "quote": "capex_finance_threshold_jpy: 1500000"}]
    admin = C.decision_card(v, ev, {"can_read_evidence": True, "can_resolve": True})
    approver = C.decision_card(v, ev, {"can_read_evidence": False, "can_resolve": False})
    assert "Which orders" in admin["question"] and "approval.yaml" in admin["question"]
    assert admin["evidence"] == ev and not admin["evidence_hidden"] and admin["you_can_decide"] is True
    assert approver["evidence"] == [] and approver["evidence_hidden"] is True and approver["you_can_decide"] is False         # the passage is withheld from a role without evidence.read
    opts = {o["key"]: o for o in admin["options"]}
    assert opts["state_scope"]["edits_source"] is False and opts["edit_source"]["edits_source"] is True and "leave_open" in opts   # knowledge-only vs source edit is stated per option
    assert "admin" in admin["deciders"] and "reviewer" not in admin["deciders"] and "change_approver" not in admin["deciders"]  # who holds conflict.resolve
