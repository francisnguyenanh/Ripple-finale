"""SUR-04 semantic drift advisory checks (pure; no DB, no provider, no git)."""
import pytest

from core.change_intelligence.v2 import semantic_drift as sd

DIFF = """diff --git a/src/routing.py b/src/routing.py
index 1..2 100644
--- a/src/routing.py
+++ b/src/routing.py
@@ -1,4 +1,4 @@
 def approver(amount):
-    if amount >= 1000000:
+    if amount > 1000000:
         return "manager"
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
--- a/src/old.py
+++ /dev/null
@@ -1 +0,0 @@
-if exempt: skip_review()
"""
LIMITS = {"max_findings": 3, "max_statement_chars": 200, "max_notes": 1}
RULES = {"r1": {"rule_id": "r1", "title": "Manager reviews 1,000,000 JPY or more", "condition": {"field": "amount", "op": "gte", "value": 1000000, "unit": "JPY"},
                "actor_ref": "role-manager", "outcome": {"kind": "review_required"}, "exceptions": []}}


def f(**kw):
    base = {"kind": "operator_change", "rule_id": "r1", "path": "src/routing.py", "witness_removed": "if amount >= 1000000:", "witness_added": "if amount > 1000000:",
            "statement": "Exactly 1000000 is no longer reviewed.", "confidence": "high"}
    return {**base, **kw}


def run(*findings):
    return sd.check(sd.validate_model_object({"status": "findings", "findings": list(findings), "notes": ["n1", "n2"]}, LIMITS), sd.index_diff(DIFF), RULES, LIMITS)


def test_index_keeps_changed_lines_per_path_including_a_deleted_file_under_its_old_path():
    idx = sd.index_diff(DIFF)
    assert idx == {"src/routing.py": {"added": ["if amount > 1000000:"], "removed": ["if amount >= 1000000:"]},
                   "src/old.py": {"added": [], "removed": ["if exempt: skip_review()"]}}                       # a removed check in a deleted file stays witnessable
    gone = run(f(kind="exception_removed", path="src/old.py", witness_removed="if exempt: skip_review()", witness_added="", statement="The exemption check was deleted."))
    assert gone["kept"][0]["kind"] == "exception_removed"
    assert sd.comparators("if a >= 1 and b gte 2") == [">=", "gte"] and sd.numbers_in("1_000_000 and 1,000,000") == {"1000000"}


def test_a_witnessed_comparator_change_is_kept_advisory_and_unwitnessed_claims_are_refused():
    out = run(f(), f(witness_added="if amount > 1:"), f(path="src/other.py"), f(rule_id="r9"), f(witness_removed="", witness_added=""),
              f(kind="exception_removed", witness_removed="", statement="An exception went."), f(statement="It costs 7 million."))
    assert len(out["kept"]) == 1 and out["kept"][0]["operator_witness"] == "confirmed" and out["kept"][0]["status"] == "advisory"
    assert [r["reason"] for r in out["rejected"]] == ["witness_added_not_in_diff", "path_not_in_diff", "rule_not_offered", "no_witness",
                                                      "exception_removed_needs_removed_line", "figure_not_in_witness_or_rule:7"]
    assert out["never_a_pass"] is True and out["notes"] == ["n1"]
    same = run(f(), f(), f(witness_removed="if amount >= 1000000:", witness_added="", kind="condition_change", statement="Condition differs."))
    assert [r["reason"] for r in same["rejected"]] == ["duplicate"] and len(same["kept"]) == 2
    no_op = run(f(witness_removed="if amount >= 1000000:", witness_added="if amount > 1000000:"))
    assert no_op["kept"][0]["operator_witness"] == "confirmed"
    assert run()["conclusion"] == "no_finding_kept"                                                        # nothing kept is not a pass either


@pytest.mark.parametrize("obj", [{"status": "findings", "findings": []}, {"status": "ok", "findings": [], "notes": []},
                                 {"status": "findings", "findings": [{"kind": "x"}], "notes": []}, {"status": "findings", "findings": [f(confidence="sure")], "notes": []}])
def test_malformed_answers_are_refused(obj):
    with pytest.raises(ValueError):
        sd.validate_model_object(obj, LIMITS)
