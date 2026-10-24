"""SUR-03 scope check (server, exact): an edit that removes or renames a quoted code literal (a reason code, a config key) that neither the change nor its
recommendations name is refused whole as an unrequested change; adding a literal and changing a named one stay allowed. Pure function; the executor wiring is
covered by tests/final_loop/test_sur03_code_proposal.py."""
from core.change_intelligence.v2 import code_proposal as cp

LIMITS = {"max_edits": 8, "max_replace_chars": 2000, "max_text_chars": 500, "max_explanation_chars": 1500, "max_tests": 6, "max_notes": 4}
FILE = 'def check(amount):\n    if amount <= 0:\n        return "NONPOSITIVE_AMOUNT"\n    if amount > 1000000:\n        return "OVER_LIMIT"\n    return None\n'


def _model(*edits):
    return {"status": "proposal", "explanation": "x", "edits": [dict(e, rationale="r") for e in edits], "suggested_tests": [], "notes": []}


def _check(model, named):
    return cp.check(model, {"src/rules.py": FILE}, ["rec-1"], allowlist=["src/"], protected=[], limits=LIMITS, named=named)


def test_a_rename_nobody_asked_for_is_refused_and_the_requested_edit_is_kept():
    out = _check(_model({"path": "src/rules.py", "ref": "rec-1", "find": "if amount > 1000000:", "replace": "if amount > 2000000:"},
                        {"path": "src/rules.py", "ref": "rec-1", "find": 'return "NONPOSITIVE_AMOUNT"', "replace": 'return "INVALID_AMOUNT"'}),
                 named="- change the limit from 1000000 to 2000000")
    assert [k["path"] for k in out["kept_edits"]] == ["src/rules.py"]
    assert [r["reason"] for r in out["rejected"]] == ["unrequested_literal_change:NONPOSITIVE_AMOUNT"]
    assert "2000000" in out["diff"] and "INVALID_AMOUNT" not in out["diff"]


def test_adding_a_code_or_changing_a_named_one_is_allowed():
    out = _check(_model({"path": "src/rules.py", "ref": "rec-1", "find": '        return "OVER_LIMIT"\n',
                         "replace": '        return "OVER_LIMIT"\n    if amount > 5000000:\n        return "HARD_LIMIT"\n'}), named="add an upper bound")
    assert out["rejected"] == [] and len(out["kept_edits"]) == 1
    out = _check(_model({"path": "src/rules.py", "ref": "rec-1", "find": 'return "OVER_LIMIT"', "replace": 'return "ABOVE_LIMIT"'}),
                 named="rename the reason code OVER_LIMIT to ABOVE_LIMIT")
    assert out["rejected"] == []
