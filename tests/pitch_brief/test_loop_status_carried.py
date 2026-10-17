"""The loop status of a REVISED draft with carried open items, through the whole chain (revise -> proposal -> contract -> review -> approval -> apply -> re-extraction -> loop status),
on the disposable test database and the Harbor fixture (a test workspace of its own; nothing of the cloud pitch-ws1 is touched). Stub provider. The point: after an apply the
items that earlier drafts left open are still listed as residual, with their lineage and disposition, are never turned into "resolved" by the apply of the one supported edit, and the
role without evidence.read receives no source passage."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infra.worker import change_draft, extraction
from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_change_draft import Model, _res, run
from tests.pitch_brief.test_draft_to_proposal import _grant, _reports, _to_approval
from tests.pitch_brief.test_loop_feedback import PostApplyExtraction
from tests.pitch_brief.test_redaction_and_state_text import _leaks, _raw_strings


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_loop_status_of_a_revised_draft_keeps_the_carried_items_open_with_their_lineage(env, ch, monkeypatch):
    def first(obj, prompt):
        obj["questions_for_human"] = ["Q?"]
        for it in obj["items"]:
            if it["action"] == "manual":
                it["action"] = "ambiguity"
    d1, body = run(env, ch, monkeypatch, Model(mutate=first))
    r1 = _res(body)
    iid = r1["intent"]["intent_id"]
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit this to the YAML edit."}).status_code == 200

    def second(obj, prompt):
        obj["questions_for_human"] = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"] = "exclude"
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: Model(mutate=second))
    req = env.req_client()
    d2, r = create_job(req, env, "change.draft", {"intent_id": iid, "extraction_job_ids": list(ch.jobs.values()), "revises_draft_job_id": d1})
    assert r.status_code == 200, r.text
    drain(env)
    res2 = _res(job_body(ch.admin, env, d2))
    carried = {c["ref"] for c in res2["carried_unresolved"]}
    assert carried
    ij, sj = _reports(env, ch, iid)
    pj, r = create_job(req, env, "proposal", {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj, "target_artifact_id": ch.arts[APPROVAL],
                                              "edit_kind": "yaml_typed_edit", "draft_job_id": d2})
    assert r.status_code == 200, r.text
    drain(env)
    _res(job_body(ch.admin, env, pj))
    c = SimpleNamespace(req=req, proposal=pj, intent=iid, draft=d2, impact=ij, simulation=sj)
    op, review, aj, appr = _to_approval(env, ch, c)
    approver = _grant(env, "change_approver")
    rd = approver.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "ok", "expected_approval_digest": appr["approval_digest"]})
    assert rd.status_code == 200, rd.text
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: PostApplyExtraction())
    apj, r = create_job(req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 200, r.text
    drain(env)
    drain(env)
    st = ch.admin.get("/v2/change/loop-status", params={"workspace_id": env.ws, "draft_job_id": d2, "apply_job_id": apj})
    assert st.status_code == 200, st.text
    s = st.json()
    assert s["stages"]["verification"] == "PASSED" and s["status"] == "PARTIAL"                       # the supported edit passed; the loop is NOT complete
    carried_residual = [x for x in s["residual_counterevidence"] if x.get("carried_from")]
    assert {x["ref"] for x in carried_residual} >= carried
    assert all(x["carried_from"]["draft_job_id"] == d1 and x["disposition"] in ("unresolved", "potentially_affected") for x in carried_residual)       # lineage and "unresolved" survive the apply
    assert all(x["current_state"] != "at_new_value" or x["draft_status"] == "NO_OP" for x in carried_residual)
    assert any(r.startswith("residual_sources") for r in s["reasons"])
    # the role without evidence.read receives the same list and no source passage
    raw = _raw_strings(res2)
    s_req = req.get("/v2/change/loop-status", params={"workspace_id": env.ws, "draft_job_id": d2, "apply_job_id": apj})
    assert s_req.status_code == 200 and not _leaks(s_req.json(), raw)
    assert {x["ref"] for x in s_req.json()["residual_counterevidence"] if x.get("carried_from")} >= carried
