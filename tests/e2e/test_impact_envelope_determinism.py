# -*- coding: utf-8 -*-
"""Same snapshot + same intent + same scenario -> same impact envelope.

The promise is NOT that the model words things the same way. It is that the
set of affected artifacts and every deterministic gate result are the same.
`test_replay_determinism.py` replays the observe/what-if/lineage script; this
file covers what that script does not reach: prepare, review, decide, apply
and verify, i.e. every gate a ChangeSet passes through.

Three comparisons, each over three runs:

  1. three fresh registries                        -> identical, ids included
     (and two subprocesses with different PYTHONHASHSEED -> identical)
  2. three sessions side by side in one registry   -> identical envelope;
     ids differ ONLY because they are session-scoped by design
  3. three different model wordings of one request -> identical envelope

The declared nondeterminism is the model's free text (`text`,
`rationale`). Grounding re-derives target and scope from the human's words,
so neither can move the envelope. A control run with a different value
proves the comparison is not vacuous.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from unittest.mock import patch

import pytest

from app.api.router import Api, Request
from app.api.sessions import SessionRegistry
from core.change_intelligence.session import InMemoryBaseline
from reference_system.reset.baseline import build_baseline
from tests.intent_support import model_result

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
ACTOR = "judge@example.com"
WHEN = "2026-09-20T14:00:00Z"
RUNS = 3

#: Free text a model may phrase differently on every call (the declared
#: nondeterminism). Grounded fields are not in this list.
WORDINGS = (
    dict(text="request", rationale="requested"),
    dict(text="Lower the approval threshold", rationale="The user asks to lower the threshold for all orders."),
    dict(text="approval threshold -> 500000", rationale="Threshold reduction; JPY; every order."),
)


def _registry():
    built = build_baseline(SEED)
    return SessionRegistry(baseline=InMemoryBaseline.from_artifacts(built["artifacts"]))


def _call(api, operation, session_id="", **payload):
    reply = api.call(Request(operation, session_id=session_id, actor=ACTOR,
                             timestamp=WHEN, payload=payload))
    assert reply.ok, (operation, reply.error)
    return reply.data


def _journey(api, value=500000, wording=WORDINGS[0]):
    """findings -> intent -> what-if -> impact -> prepare -> review -> decide -> apply -> verify."""
    sid = _call(api, "session.create")["session_id"]
    out = {"findings": _call(api, "findings.list", sid)}
    text = f"For all orders lower approval threshold to JPY {value}"
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt",
               return_value=model_result(proposed_value=value, **wording)):
        out["candidate"] = _call(api, "intent.parse", sid, text=text)
    out["intent"] = _call(api, "intent.create", sid, candidate_id=out["candidate"]["candidate_id"],
                          content_hash=out["candidate"]["content_hash"])
    out["whatif"] = _call(api, "whatif.simulate", sid, target="approval_threshold", proposed_value=value)
    shadow = out["whatif"]["shadow_id"]
    out["impact"] = _call(api, "impact.show", sid, shadow_id=shadow)
    out["prepare"] = _call(api, "changeset.prepare", sid, intent_id=out["intent"]["intent_id"], shadow_id=shadow)
    cid = out["prepare"]["changeset_id"]
    out["review"] = _call(api, "changeset.review", sid, changeset_id=cid)
    out["decide"] = _call(api, "changeset.decide", sid, changeset_id=cid, decision="approve",
                          reason="evidence is clear")
    out["apply"] = _call(api, "changeset.apply", sid, changeset_id=cid)
    out["verify"] = _call(api, "verification.run", sid, changeset_id=cid)
    out["observe"] = _call(api, "state.observe", sid)
    return out


def envelope(out):
    """The session-independent result: what is affected and what every gate said."""
    impact = out["impact"]
    whatif = out["whatif"]
    diff = whatif["operational_diff"]
    return {
        "findings": sorted(
            (f["target"], f["kind"], tuple(sorted((r["artifact"], str(r["value"]), r["locator"])
                                                  for r in f["representations"])))
            for f in out["findings"]["findings"]),
        "intent_validation": out["candidate"]["validation"],
        "grounded": {k: out["candidate"][k] for k in ("target", "desired_value", "unit", "scope")
                     if k in out["candidate"]},
        "impact": {tier: sorted((a["identifier"], a["kind"], a["evidence"]) for a in impact[tier])
                   for tier in ("direct", "indirect", "unknown")},
        "simulation": (whatif["current_value"], whatif["proposed_value"], diff["total"], diff["changed"],
                       tuple(s["case_id"] for s in diff["shifts"]), whatif["risk_level"],
                       whatif["has_inconsistency"]),
        "readiness": out["prepare"]["readiness"],
        "actions": {k: out["prepare"][k] for k in ("required", "recommended", "unsupported")},
        "review_blocks": out["review"]["blocks"],
        "decide": out["decide"]["state"],
        "apply": (out["apply"]["state"], out["apply"]["written"], out["apply"]["human_actions"]),
        "verify": {k: out["verify"][k] for k in ("alignment", "passed", "unresolved", "blocked_by",
                                                  "next_actions", "lineage_version")},
        "changed_in_session": out["observe"]["changed_in_session"],
    }


def _dump(value):
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


@pytest.fixture(scope="module")
def fresh_runs():
    return [_journey(Api(sessions=_registry())) for _ in range(RUNS)]


def test_three_fresh_registries_give_the_same_envelope(fresh_runs):
    first = _dump(envelope(fresh_runs[0]))
    assert all(_dump(envelope(run)) == first for run in fresh_runs[1:])


def test_three_fresh_registries_reproduce_every_id_and_snapshot(fresh_runs):
    """With fresh registries even session-scoped ids reproduce: nothing random is minted."""
    first = _dump(fresh_runs[0])
    assert all(_dump(run) == first for run in fresh_runs[1:])


def test_the_envelope_is_the_expected_one_so_the_comparison_means_something(fresh_runs):
    env = envelope(fresh_runs[0])
    assert env["readiness"] == "NEEDS_REVIEW"
    assert env["decide"] == "APPROVED" and env["apply"][0] == "APPLIED"
    assert env["verify"]["passed"] is True and env["verify"]["alignment"] == "ALIGNED"
    assert env["simulation"][2:4] == (220, 47)
    assert {a for a, *_ in env["impact"]["direct"]} >= {
        "workflow/approval_routes.yaml", "data/approval_matrix.xlsx", "application/config.py"}
    assert env["apply"][1] == ["data/approval_matrix.xlsx", "workflow/approval_routes.yaml"]
    assert any(kind == "CONFLICT" for _, kind, _ in env["findings"])


def test_three_sessions_in_one_registry_give_the_same_envelope():
    api = Api(sessions=_registry())
    runs = [_journey(api) for _ in range(RUNS)]
    first = _dump(envelope(runs[0]))
    assert all(_dump(envelope(run)) == first for run in runs[1:])
    # ...and they really were different sessions: the ids are session-scoped.
    assert len({run["prepare"]["changeset_id"] for run in runs}) == RUNS
    assert len({run["apply"]["snapshot_id"] for run in runs}) == RUNS


def test_different_model_wordings_give_the_same_envelope():
    runs = [_journey(Api(sessions=_registry()), wording=w) for w in WORDINGS]
    first = _dump(envelope(runs[0]))
    assert all(_dump(envelope(run)) == first for run in runs[1:])


def test_a_different_value_gives_a_different_envelope(fresh_runs):
    """Control: the comparison can fail."""
    other = envelope(_journey(Api(sessions=_registry()), value=300000))
    base = envelope(fresh_runs[0])
    assert other["simulation"] != base["simulation"]
    assert other["actions"] != base["actions"]


def test_other_processes_with_other_hash_seeds_give_the_same_result(fresh_runs):
    """Set/dict iteration order must not leak into any reply: a new interpreter
    with a different PYTHONHASHSEED reproduces the whole journey byte for byte."""
    code = ("import json; from app.api.router import Api; "
            "from tests.e2e.test_impact_envelope_determinism import _journey, _registry, _dump; "
            "print(_dump(_journey(Api(sessions=_registry()))))")
    root = pathlib.Path(__file__).resolve().parents[2]
    expected = _dump(fresh_runs[0])
    for seed in ("1", "4242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        done = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=300)
        assert done.returncode == 0, done.stderr[-2000:]
        assert done.stdout.strip().splitlines()[-1] == expected, f"PYTHONHASHSEED={seed}"
