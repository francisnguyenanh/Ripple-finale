"""Source CONTENT follows the same policy as rule evidence. A member without `evidence.read` cannot read file bytes (export), parsed values
(evidence), passages (retrieval) or evidence checks (validate) - the server refuses; the observation LIST (kind + locator, no value) stays
a structure view. Readers: admin, knowledge_owner, reviewer. Not readers: viewer, requester, approver, restore_operator."""
import base64, json

import pytest

from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.test_role_model import member


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


READERS = ("reviewer", "knowledge_owner", "admin")
OTHERS = ("viewer", "requester", "change_approver", "restore_operator")


def _client(env, role):
    return env.admin_client() if role == "admin" else member(env, role)


def test_source_content_needs_evidence_read_on_every_route(env, ch):
    rev = env.store.head(env.tenant, env.ws).source_revision_id
    art = ch.arts[APPROVAL]
    ws = {"workspace_id": env.ws}
    quotes = [e["quote"] for cs in ch.cands.values() for c in cs for e in c.get("evidence", []) if len(e.get("quote", "")) > 25]
    first = env.admin_client().get(f"/v2/sources/revisions/{rev}/artifacts/{art}/observations", params=ws).json()["observations"][0]["observation_id"]
    for role in READERS + OTHERS:
        c = _client(env, role)
        o = c.get(f"/v2/sources/revisions/{rev}/artifacts/{art}/observations", params=ws)
        assert o.status_code == 200 and set(o.json()["observations"][0]) == {"kind", "locator", "observation_id"}, role       # structure only, never a value
        calls = {
            "evidence": c.get(f"/v2/sources/revisions/{rev}/evidence/{first}", params=ws),
            "export": c.get(f"/v2/sources/revisions/{rev}/artifacts/{art}/export", params=ws),
            "retrieval": c.post("/v2/retrieval:query", json={**ws, "query": "manager approval threshold", "top_k": 3}),
            "validate": c.post(f"/v2/sources/revisions/{rev}/evidence:validate", json={**ws, "refs": []}),
        }
        for name, r in calls.items():
            if role in READERS:
                assert r.status_code == 200, (role, name, r.status_code)
            else:
                assert r.status_code == 403, (role, name, r.status_code)
                assert not any(q in r.text for q in quotes) and "raw_value" not in r.text and "content_b64" not in r.text, (role, name)
    got = base64.b64decode(env.admin_client().get(f"/v2/sources/revisions/{rev}/artifacts/{art}/export", params=ws).json()["content_b64"]).decode()
    assert "standard_manager_threshold_jpy" in got                                              # the readers' positive control


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def test_ai_outputs_carry_no_source_quote_for_a_non_reader_and_the_stored_output_is_untouched(env, ch, monkeypatch):
    """Draft citations and answer evidence spans: a member without evidence.read gets the business reading, file, location and grounding status, but
    never the verbatim source text (server-side, per reader; the stored output keeps the original)."""
    from infra.worker import gap_answer
    from tests.pitch_brief.test_change_draft import Model, run
    from tests.pitch_brief.test_loop_feedback import GapStub
    from tests.pitch_brief.conftest import create_job, drain, job_body
    jid, _ = run(env, ch, monkeypatch, Model())
    monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: GapStub("routing_rules.standard_manager_threshold_jpy: 1000000", "1000000"))
    gj, r = create_job(env.req_client(), env, "gap.answer", {"question": "What is the Manager approval limit?", "question_id": "qb", "artifact_ids": [ch.arts[APPROVAL]]})
    assert r.status_code == 200
    drain(env)
    ws = {"workspace_id": env.ws}
    adm = env.admin_client()
    for job, key in ((jid, "citation"), (gj, "evidence_spans")):
        full = adm.get(f"/v2/jobs/{job}", params=ws).json()["execution_result"]
        assert any(k == "quote" and v for k, v in _walk(full)), (job, "reader sees the cited text")
        for role in ("requester", "change_approver", "viewer", "restore_operator"):
            c = env.req_client() if role == "requester" else member(env, role)
            body = c.get(f"/v2/jobs/{job}", params=ws).json()
            res = body["execution_result"]
            assert not any(k in ("quote", "quotes") and v for k, v in _walk(res)), (job, role)
            assert any(k == "quotes_redacted" and v for k, v in _walk(res)) and res.get("evidence_redacted") is True, (job, role)
            assert "routing_rules.standard_manager_threshold_jpy: 1000000" not in json.dumps(body), (job, role)       # not in payload either
            assert any(k in ("artifact_path", "logical_path") and v for k, v in _walk(res)), (job, role, "file/location metadata stays")
    # the stored output and its history are untouched
    with env.store._tx() as cur:
        cur.execute("SELECT execution_result::text FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, gj))
        assert "routing_rules.standard_manager_threshold_jpy: 1000000" in cur.fetchone()[0]
