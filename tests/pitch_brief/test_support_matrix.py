"""The registered support matrix: every 'supported' claim names a test that exists, every code reference resolves against the
pinned Harbor copy, 'not-controlled' claims carry a trace with 0 matches, and the UI/API show it (also for other accounts)."""
from __future__ import annotations

import pathlib
import re

import pytest

from infra.v2_support import ROWS, build_matrix

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_every_named_test_exists_and_every_supported_claim_has_one():
    for rid, title, where, view, sim, apply_, analyse, refs, tests, trace in ROWS:
        for t in tests:
            path, name = t.split("::")
            assert re.search(r"def " + re.escape(name) + r"\b", (REPO / path).read_text(encoding="utf-8")), t
        if "supported" in (sim["state"], apply_["state"]) and rid != "wf":
            assert tests, f"{rid}: a supported simulate/apply claim must name its test"


def test_code_references_resolve_and_not_controlled_claims_are_traced():
    m = build_matrix()
    assert m["fixture_error"] is None
    for r in m["rows"]:
        assert len(r["code_evidence"]) == r["_refs"], r["id"]                 # every needle found in the pinned copy
        if "not-controlled" in (r["simulate"]["state"], r["apply"]["state"]):
            assert r["code_evidence"], r["id"]
    by = {r["id"]: r for r in m["rows"]}
    assert by["emerg"]["traces"][0]["matches"] == 0 and by["terms"]["traces"][0]["matches"] == 0 and by["wf"]["traces"][0]["matches"] == 0
    assert by["emerg"]["simulate"]["state"] == "not-controlled" and by["terms"]["simulate"]["state"] == "not-controlled"
    assert by["emerg"]["apply"]["state"] == "final" and by["py"]["apply"]["state"] == "final"
    assert "NOT a security boundary" in m["isolation"] and "SIMULATED" in m["ai"]
    # the CAPEX key IS read by the emergency path (the claim in its note is backed by a code line)
    assert any(e["file"] == "app/emergency.py" for e in by["capex"]["code_evidence"])


def test_route_requires_a_session_and_serves_members(env):
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app, base_url="https://judge.test")
    assert anon.get("/v2/support/matrix", params={"workspace_id": env.ws}).status_code in (401, 403)
    r = env.req_client().get("/v2/support/matrix", params={"workspace_id": env.ws})
    assert r.status_code == 200 and len(r.json()["rows"]) == len(ROWS)
