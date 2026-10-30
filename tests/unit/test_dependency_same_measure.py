"""The dependency prompt's "same measure" list (HK-AEV2 D-04, 2026-10-27): the model answered "nothing beyond the rules already linked" although a tier of the
same decision on the same measure was not linked. The server lists the unlinked rules that measure a field a changed rule measures, for the model to check; a
linked rule, the changed rule itself and a rule on another measure are never listed. Pure: no database, no model."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import business_impact as bim
from tests.unit.test_draft_server_holds import rule

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_an_unlinked_tier_on_the_same_measure_is_listed_and_others_are_not():
    rules = {r["rule_id"]: r for r in [
        rule("rule-director", "Overtime over 40 hours a month: operations director", "crew.overtime_hours_month", "gt", 40, "role-operations-director", unit="hours"),
        rule("rule-manager", "Overtime over 20 hours a month: depot manager", "crew.overtime_hours_month", "gt", 20, "role-depot-manager", unit="hours"),
        rule("rule-meter", "Meter gaps over 5 days: field team", "meter.gap_days", "gt", 5, "role-field-team", unit="days"),
        rule("rule-linked", "Overtime over 60 hours a month: HR review", "crew.overtime_hours_month", "gt", 60, "role-hr", unit="hours")]}
    same = bim.same_measure(["rule-director"], rules, ["rule-linked"])
    assert same == ["- id=rule-manager measures crew.overtime_hours_month like changed rule id=rule-director"]
    assert bim.same_measure(["rule-meter"], rules, []) == []                                      # nothing else measures meter gaps


def test_the_list_reaches_the_prompt_and_an_empty_list_says_none():
    frozen = json.loads((ROOT / "config/change_impact_deps.json").read_text(encoding="utf-8"))
    rules = [rule("rule-a", "A", "x.v", "gt", 1, "role-a"), rule("rule-b", "B", "x.v", "gt", 2, "role-b")]
    p = bim.build_dep_prompt(frozen, ["- changed rule id=rule-a"], rules, [], can_read_evidence=False, max_chars=20000,
                             same=bim.same_measure(["rule-a"], {r["rule_id"]: r for r in rules}, []))
    assert "- id=rule-b measures x.v like changed rule id=rule-a" in p and "every other rule below is NOT linked yet" in p and "{same}" not in p
    empty = bim.build_dep_prompt(frozen, ["- changed rule id=rule-a"], rules[:1], [], can_read_evidence=False, max_chars=20000)
    assert "measure the same thing as a changed rule" in empty and "{same}" not in empty
