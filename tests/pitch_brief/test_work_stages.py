"""My work after the apply: a change that is applied but not decided is NOT 'done'. Pure function of the stored chain (no database)."""
from __future__ import annotations

import pytest

from infra.v2_work_routes import _stage

DONE = {"execution_status": "COMPLETED"}
PROP = {"execution_status": "COMPLETED", "job_id": "p"}


def chain(state):
    return {"contracts": [1], "review": DONE, "approval": DONE, "last": {"action": "approve"}, "apply": DONE,
            "change": None if state is None else {"state": state, "completion": None}}


@pytest.mark.parametrize("state, stage, person", [
    ("REANALYSING", "VERIFYING", None), ("SOURCE_PUBLISHED", "VERIFYING", None), ("STAGED", "VERIFYING", None),
    ("MACHINE_VERIFIED", "AWAITING_RESULT", "change.accept_result"), ("AWAITING_ACCEPTANCE", "AWAITING_RESULT", "change.accept_result"),
    ("FAILED", "NEEDS_ATTENTION", "change.accept_result"), ("RECOVERY_REQUIRED", "NEEDS_ATTENTION", "change.accept_result"), ("STALE", "NEEDS_ATTENTION", "change.accept_result"),
    ("COMPLETED", "COMPLETED", None), (None, "APPLIED", None)])
def test_after_the_apply_my_work_says_where_the_change_stands_and_who_decides(state, stage, person):
    st = _stage(PROP, chain(state), [])
    assert st["stage"] == stage
    assert (st["waits"] or {}).get("perm") == person
    if stage == "AWAITING_RESULT":
        assert "not complete yet" in st["label"]                                    # never reads as finished
