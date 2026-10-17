"""D0 unit tests: synthetic fixtures only, no DB, no network."""
import ast
import json
import pathlib
import socket

import pytest

from core.change_intelligence.v2.source_ingest_adapters import parse_markdown, parse_yaml
from pitch.d0 import extract_from_observations
from pitch.d0.guard import ProviderCallAttempted, no_provider_calls

YAML = b"""
approval:
  standard:
    manager_threshold_jpy: 500000
    currency: JPY
    exceptions: [capex, emergency]
  require_second_approver: true
  approver_role: finance_director
retention_days: 30
session:
  timeout_minutes: 15
name: demo
"""


def run(content=YAML, parser=parse_yaml):
    res = parser("a.yaml", content)
    return extract_from_observations(res.observations, artifact_id="a1", source_revision_id="r1",
                                     blob_hash="h")


def by_pred(out):
    return {c["predicate"]: c for c in out["candidates"]}


def test_yaml_rules_extracted_with_shape():
    out = run()
    c = by_pred(out)
    thr = c["manager_threshold"]
    assert thr["value"] == 500000 and thr["unit"] == "JPY" and thr["modality"] == "must"
    assert thr["subject"] == "approval.standard" and thr["scope"]["currency"] == "JPY"
    assert thr["exceptions"] == ["capex", "emergency"]
    assert thr["applicability"] == "RECOGNIZED"
    assert thr["evidence"][0]["quote"] == "approval.standard.manager_threshold_jpy: 500000"
    assert c["retention"]["value"] == 30 and c["retention"]["unit"] == "days"
    assert c["timeout"]["unit"] == "minutes"
    assert c["require_second_approver"]["value"] is True
    assert c["approver_role"]["outcome"] == "finance_director"
    assert "name" not in c
    assert set(out) >= {"candidates", "rejected", "model_run_id", "schema_version", "executor_id"}
    assert out["provider_calls"] == 0 and out["live_call_provenance"] == "d0_deterministic"


def test_same_keys_as_production_candidate():
    keys = {"candidate_id", "subject", "predicate", "value", "unit", "applicability", "applicability_reason",
            "modality", "scope", "exceptions", "outcome", "source_revision_id", "artifact_revision_id",
            "model_run_id", "ancestry", "rationale", "evidence"}
    for c in run()["candidates"]:
        assert set(c) == keys


def test_deterministic_byte_identical():
    assert json.dumps(run(), sort_keys=True) == json.dumps(run(), sort_keys=True)


def test_prose_is_unknown_scope():
    md = b"Expense claims must not exceed 200,000 JPY per month. Hello world."
    out = run(md, parse_markdown)
    assert len(out["candidates"]) == 1
    c = out["candidates"][0]
    assert c["applicability"] == "UNKNOWN" and c["value"] == 200000 and c["modality"] == "must"
    assert c["scope"] == {"unresolved": True}


def test_no_rules_gives_empty_not_error():
    assert run(b"title: x\nowner_name: y\n")["candidates"] == []


def test_distinct_parents_not_merged():
    y = b"a:\n  limit: 1\nb:\n  limit: 2\n"
    assert len(run(y)["candidates"]) == 2


def test_guard_blocks_provider_and_network():
    import infra.worker.live_analysis as la
    original = la._client_for_model
    with no_provider_calls() as counter:
        with pytest.raises(ProviderCallAttempted):
            la._client_for_model("gemini-3.8-flash")
        with pytest.raises(ProviderCallAttempted):
            socket.create_connection(("127.0.0.1", 9))
        run()  # D0 itself completes inside the guard
    assert len(counter) == 2
    assert la._client_for_model is original  # restored


def test_d0_sources_import_no_provider_modules():
    banned = ("ai_module", "google", "requests", "httpx", "urllib", "socket", "infra.worker.live_analysis",
              "infra.worker.extraction", "anthropic", "openai")
    for f in pathlib.Path(__file__).resolve().parents[2].joinpath("pitch", "d0").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                if f.name == "guard.py" and n == "socket":
                    continue
                assert not any(n == b or n.startswith(b + ".") for b in banned), (f.name, n)
