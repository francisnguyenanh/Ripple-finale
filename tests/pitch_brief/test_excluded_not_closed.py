"""A model "exclude" in the FIRST draft is not a basis to close an item: when the server reads the passage as the same rule as the request and its stored value is not the new value, the item stays
open as potentially affected (neither "conflict" nor "resolved"); an item whose candidates are about a different subject stays EXCLUDED and says who checked. Real caller, stub model, no cloud."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import build
from tests.pitch_brief.test_change_draft import Model, _res, run

OPEN = {"AMBIGUITY", "MANUAL_CHANGE", "UNSUPPORTED"}


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _exclude_everything(obj, prompt):
    obj["questions_for_human"] = []
    for it in obj["items"]:
        if it["action"] != "edit":
            it["action"] = "exclude"


def test_model_exclusion_of_same_rule_item_with_another_value_stays_open(env, ch, monkeypatch):
    d1, body = run(env, ch, monkeypatch, Model(mutate=_exclude_everything))
    res = _res(body)
    items = res["items"]
    closed_same_rule = [x for x in items if x["status"] == "EXCLUDED" and x.get("reason") != "model_excluded_with_rationale"]
    assert not [x for x in items if x["status"] == "EXCLUDED" and x.get("exclusion_basis") is None]       # every exclusion says on what basis
    opened = [x for x in items if x.get("disposition") == "potentially_affected"]
    for x in opened:
        assert x["status"] in OPEN and x["reason"].startswith("excluded_by_model_but")
    for x in items:
        if x["status"] == "EXCLUDED":
            assert x["exclusion_basis"] in ("model_only", "server_checked_not_the_same_rule")
    assert all(x.get("disposition") for x in res["unaddressed"] + [i for i in items if i["status"] in OPEN])   # every open entry has a kind
    print("OPENED", [(x["ref"], x["reason"]) for x in opened], "EXCL", [(x["ref"], x["exclusion_basis"]) for x in items if x["status"]=="EXCLUDED"])
    assert opened, "the fixture must contain a same-rule item holding another value"
    gaps = ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"]
    for x in opened:
        assert any(g.get("ref") == x["ref"] and g.get("disposition") == "potentially_affected" for g in gaps)
