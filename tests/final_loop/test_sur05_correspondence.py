"""SUR-05 proposed old/new correspondence of a source refresh, LOCAL: the Harbor import, knowledge bound to the real artifacts, a PREPARED refresh through the real route (a
rewritten move, a byte-identical move without proof, an edited YAML value), a SYNTHETIC model (stub through the production pinned-call path), the real worker, budget and
ledger. Covers kept / refused proposals (verbatim quotes, bound rule ids, offered paths, numbers), the server's proof facts (identical bytes -> content_hash possible, otherwise
only a person's human_confirmed proof), the refresh left unchanged, idempotent requests, malformed output, a no-op refresh and permissions. 0 real provider calls; model
quality is NOT measured here."""
from __future__ import annotations

import base64
import copy
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.worker import explain
from tests.final_loop.test_kno01_business_store import bind, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno03_graph_gaps import seed
from tests.final_loop.test_qry02_retrieval import KNOWLEDGE
from tests.final_loop.test_qry03_qa import use
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.conftest import PREFIX, REAL_BUDGET_PATH, drain, harbor_files

GUIDE, HANDBOOK = f"{PREFIX}/docs/finance_desk_guide.md", f"{PREFIX}/docs/desk/finance_handbook.md"
WORKFLOW, WORKFLOW2 = f"{PREFIX}/config/workflow.yaml", f"{PREFIX}/config/workflow_v2.yaml"
YAML = f"{PREFIX}/config/approval.yaml"
FIXTURE_ARTIFACTS = {"doc-procurement-policy": "docs/procurement_policy.md", "doc-manager-guide": "docs/finance_desk_guide.md", "cfg-approval": "config/approval.yaml",
                     "code-routing": "app/calculations.py"}


@pytest.fixture
def corr_env(env, ws, monkeypatch, tmp_path):                 # noqa: F811
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("role_model_ids:\n", "role_model_ids:\n  source.correspondence: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_corr.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    arts = {a["logical_path"]: a["artifact_id"] for a in env.store.source_revision_view(env.tenant, env.ws, ws.source_revision_id)["artifacts"]}
    real = {fid: arts[f"{PREFIX}/{p}"] for fid, p in FIXTURE_ARTIFACTS.items()}
    rules = bind(KNOWLEDGE["observed_rules"], ws.source_revision_id)
    for r in rules:                                                                                   # the fixture cites synthetic artifact ids: point them at the import
        for b in r["source_bindings"] + [p for p in r["provenance"] if p["kind"] == "source_evidence"]:
            b["artifact_id"] = real.get(b["artifact_id"], b["artifact_id"])
    seed(env, ws, rules=rules)
    bound_to_guide = sorted(r["rule_id"] for r in rules if any(b["artifact_id"] == real["doc-manager-guide"] for b in r["source_bindings"]))
    return SimpleNamespace(env=env, admin=env.admin_client(), req=env.req_client(), monkeypatch=monkeypatch, bound_to_guide=bound_to_guide)


def prepare(q, mutate=True):
    files, original = [], dict(harbor_files())
    for p, content in original.items():
        if mutate and p == GUIDE:
            p, content = HANDBOOK, b"Moved handbook for the finance desk.\n\n" + content.replace(b"\n\n", b"\n\nIn short: ", 1)
        elif mutate and p == WORKFLOW:
            p = WORKFLOW2                                                                             # identical bytes, new path, no proof
        elif mutate and p == YAML:
            content = content.replace(b"standard_manager_threshold_jpy: 1000000", b"standard_manager_threshold_jpy: 1500000")
        files.append({"path": p, "content_b64": base64.b64encode(content).decode()})
    r = q.admin.post("/v2/sources/refresh/prepare", json={"workspace_id": q.env.ws, "idempotency_key": uuid4().hex, "files": files})
    assert r.status_code == 200, r.text
    return r.json()


def blocks(prompt: str, label: str) -> dict:
    out = {}
    for m in re.finditer(r"=== " + label + r" path=(\S+)[^\n]*\n((?:  bound rule id=[^\n]*\n)*)(.*?)\n=== end \1", prompt, re.S):
        out[m.group(1)] = {"rules": re.findall(r"bound rule id=(\S+):", m.group(2)), "lines": [l.strip() for l in m.group(3).splitlines() if len(l.strip()) > 20]}
    return out


def pair(old, new, relation, rule_ids, oq, nq, rationale="The cited lines carry the same content.", confidence="medium"):
    return {"old_path": old, "new_path": new, "relation": relation, "rule_ids": rule_ids, "old_quote": oq, "new_quote": nq, "rationale": rationale, "confidence": confidence}


def corr_model(prompt: str):
    old, new = blocks(prompt, "OLD"), blocks(prompt, "NEW")
    assert GUIDE in old and WORKFLOW in old and YAML in old and HANDBOOK in new and WORKFLOW2 in new and YAML in new
    g_old, g_new = old[GUIDE]["lines"][1], new[HANDBOOK]["lines"][0]
    y_old = next(l for l in old[YAML]["lines"] if "standard_manager_threshold_jpy" in l)
    y_new = next(l for l in new[YAML]["lines"] if "standard_manager_threshold_jpy" in l)
    w = old[WORKFLOW]["lines"][0]
    return {"status": "pairs", "notes": ["The procurement policy did not change."], "pairs": [
        pair(GUIDE, HANDBOOK, "moved_rewritten", old[GUIDE]["rules"], g_old, g_new),
        pair(WORKFLOW, WORKFLOW2, "moved_same_content", [], w, w),
        pair(YAML, YAML, "obligation_changed", old[YAML]["rules"], y_old, y_new, "The manager threshold moves from 1000000 to 1500000."),
        pair(GUIDE, HANDBOOK, "split", [], g_old, "A sentence that is not in the new file at all."),
        pair(YAML, YAML, "moved_rewritten", ["rule-not-bound"], y_old, y_new),
        pair(f"{PREFIX}/docs/procurement_policy.md", HANDBOOK, "merged", [], "x" * 30, g_new),
        pair(GUIDE, HANDBOOK, "no_counterpart", [], g_old, ""),
        pair(YAML, YAML, "split", [], y_old, y_new, "It costs 42 approvals.")]}


def post(q, client, rid, **body):
    return client.post(f"/v2/sources/refresh/{rid}/correspondence", json={"workspace_id": q.env.ws, **body})


def get(q, client, rid):
    return client.get(f"/v2/sources/refresh/{rid}/correspondence", params={"workspace_id": q.env.ws})


def test_correspondence_proposals_are_checked_carry_the_proof_facts_and_change_nothing(corr_env):
    q = corr_env
    model = use(q, corr_model)
    p = prepare(q)
    rid = p["refresh_id"]
    assert get(q, q.admin, rid).json()["status"] == "not_requested"
    r = post(q, q.admin, rid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("running", "waiting_for_approval") and model.calls == 0
    drain(q.env)
    got = get(q, q.admin, rid).json()
    assert got["status"] == "done", got
    by = {(x["old_path"], x["relation"]): x for x in got["proposals"]}
    moved = by[(GUIDE, "moved_rewritten")]
    assert moved["is_rename_proof"] is False and moved["bytes_identical"] is False and moved["proof_possible"] == "human_confirmed_with_reason"
    assert moved["rule_ids"] == q.bound_to_guide and moved["status"] == "PROPOSED"
    same = by[(WORKFLOW, "moved_same_content")]
    assert same["bytes_identical"] is True and same["proof_possible"] == "content_hash" and same["is_rename_proof"] is False
    assert by[(YAML, "obligation_changed")]["proof_possible"] == "none_needed"
    assert sorted(x["reason"] for x in got["rejected"]) == ["figure_not_in_quotes_or_rules:42", "new_quote_not_in_excerpt", "no_counterpart_with_new_path",
                                                             "old_path_not_offered", "rule_not_bound_to_old_artifact"]
    assert got["unmatched_old"] == [] and got["coverage"]["withheld"] == [] and got["ai_outcome"]["contribution"] == "retained"
    # the refresh itself is untouched: no rename proof, same digest, still PREPARED
    st = q.admin.get(f"/v2/sources/refresh/{rid}", params={"workspace_id": q.env.ws}).json()
    assert st["status"] == "PREPARED" and st["refresh_digest"] == p["refresh_digest"] and st.get("rename_proofs", []) == []
    again = post(q, q.admin, rid)
    drain(q.env)
    assert again.json()["job_id"] == r.json()["job_id"] and model.calls == 1


def test_malformed_output_noop_refresh_and_permissions(corr_env):
    q = corr_env
    use(q, lambda prompt: {"status": "pairs", "pairs": "not a list", "notes": []})
    rid = prepare(q)["refresh_id"]
    post(q, q.admin, rid, attempt=2)
    drain(q.env)
    bad = get(q, q.admin, rid).json()
    assert bad["status"] == "failed" and bad["reason_code"] == "schema_violation" and "Nothing is concluded" in bad["reason"]
    noop = prepare(q, mutate=False)
    assert post(q, q.admin, noop["refresh_id"]).json()["code"] == "refresh_not_open"
    assert post(q, q.admin, "rfr-unknown").status_code == 404 and post(q, q.admin, rid, attempt=0).status_code == 400
    viewer = login(q, f"viewer-corr-{q.env.nonce}", ("viewer",))
    assert post(q, viewer, rid).status_code == 403 and get(q, viewer, rid).status_code == 403
    assert post(q, q.req, rid).status_code == 403                                                       # a requester cannot import sources
