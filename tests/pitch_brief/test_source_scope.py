"""The supported source formats of this submission and the explicit refusal of everything else (for example Word, PDF, PowerPoint, job scripts).
Real routes, real store and worker. There is no adapter registry and no parser for those formats: an unsupported file is never read, staged or analysed;
the person is told, per file, that it is `unsupported_format` and the batch cannot be committed until each such file is acknowledged as skipped."""
from __future__ import annotations

import base64
from uuid import uuid4

from core.change_intelligence.v2.source_admission import SUPPORTED_MEDIA_TYPES, admit_files

LEGACY = [
    ("legacy/PAYCALC.cbl", b"       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PAYCALC.\n"),
    ("legacy/EMP.cpy", b"       01 EMP-REC.\n           05 EMP-ID PIC X(8).\n"),
    ("legacy/NIGHTLY.jcl", b"//NIGHTLY JOB (ACCT),'RUN'\n//STEP1 EXEC PGM=PAYCALC\n"),
    ("docs/manual.docx", b"PK\x03\x04 not really a document"),
    ("docs/spec.pdf", b"%PDF-1.4\n% not really a pdf\n"),
    ("docs/deck.pptx", b"PK\x03\x04 not really a deck"),
]
OK = ("config/approval.yaml", b"routing_rules:\n  standard_manager_threshold_jpy: 500000\n")


def test_the_supported_formats_are_exactly_the_documented_ones():
    assert set(SUPPORTED_MEDIA_TYPES) == {".md", ".yml", ".yaml", ".py", ".xlsx", ".csv", ".jsonl"}


def test_every_out_of_scope_format_is_reported_per_file_as_unsupported_and_needs_an_explicit_skip():
    report = admit_files([OK, *LEGACY])
    by_path = {i.logical_path: i for i in report.items}
    assert by_path[OK[0]].disposition == "admitted"
    for path, _ in LEGACY:
        item = by_path[path]
        assert item.disposition == "skip_required" and item.reason == "unsupported_format", (path, item.disposition, item.reason)
    preview = report.preview()
    assert preview["admission_complete"] is False
    assert sorted(preview["acknowledgement_required"]) == sorted(p for p, _ in LEGACY)


def _body(env, files):
    return {"workspace_id": env.ws, "idempotency_key": uuid4().hex,
            "files": [{"path": p, "content_b64": base64.b64encode(b).decode()} for p, b in files]}


def test_an_import_with_out_of_scope_files_cannot_be_committed_without_acknowledging_each_and_never_ingests_them(env):
    from infra.worker.job_worker import claim_and_admit_one
    client = env.admin_client()
    r = client.post("/v2/sources/imports", json=_body(env, [OK, *LEGACY]))
    assert r.status_code == 200, r.text
    dto = r.json()
    assert sorted(dto["acknowledgement_required"]) == sorted(p for p, _ in LEGACY) and dto["admission_complete"] is False
    commit = f"/v2/sources/imports/{dto['import_id']}/commit"
    # nothing, or only some of the files acknowledged: refused, nothing staged
    for acked in ([], [LEGACY[0][0]]):
        refused = client.post(commit, json={"workspace_id": env.ws, "expected_version": 0, "acknowledged_skips": acked})
        assert refused.status_code in (400, 409, 422), (acked, refused.status_code, refused.text)
    assert claim_and_admit_one(env.store, env.tenant, env.ws, "ingest") is None, "a refused commit queues no ingest"
    # every out-of-scope file acknowledged as skipped: the batch is committed, and only the supported file reaches the source revision
    ok = client.post(commit, json={"workspace_id": env.ws, "expected_version": 0, "acknowledged_skips": [p for p, _ in LEGACY]})
    assert ok.status_code == 200, ok.text
    assert claim_and_admit_one(env.store, env.tenant, env.ws, "ingest") is not None
    head = env.store.head(env.tenant, env.ws)
    paths = {a["logical_path"] for a in env.store.source_revision_view(env.tenant, env.ws, head.source_revision_id)["artifacts"]}
    assert paths == {OK[0]}, paths


def test_a_judge_cannot_preview_or_commit_an_import_on_the_real_routes_while_an_importer_can_preview_without_storing_anything(env):
    """Judges hold no `source.import`: the server refuses the preview and the commit (the page hides the panel, tests/pitch_brief/test_ui_judge_profile.py). The unsupported-format preview is therefore checked
    with an account that has the permission, and a preview writes no source revision."""
    from tests.pitch_brief.test_role_model import member
    head_before = env.store.head(env.tenant, env.ws).source_revision_id
    for role in ("judge", "viewer"):
        who = member(env, role)
        r = who.post("/v2/sources/imports", json=_body(env, [OK, *LEGACY]))
        assert r.status_code == 403, (role, r.status_code, r.text)
        c = who.post("/v2/sources/imports/anything/commit", json={"workspace_id": env.ws, "expected_version": 0, "acknowledged_skips": []})
        assert c.status_code in (403, 404), (role, c.status_code)
    importer = member(env, "requester")
    r = importer.post("/v2/sources/imports", json=_body(env, [OK, *LEGACY]))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["acknowledgement_required"]) == sorted(p for p, _ in LEGACY) and r.json()["admission_complete"] is False
    assert env.store.head(env.tenant, env.ws).source_revision_id == head_before, "a preview stores no source revision"
