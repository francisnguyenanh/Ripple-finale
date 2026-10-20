"""EUI-03: bounded candidate byte diff of the automatic file edits of a confirmed business change, through the real route, store and policy.

The diff is computed in memory from the draft's pinned bytes with the apply job's own scalar-edit function: nothing is staged, written or applied. File contents
need evidence rights; a stranger sees nothing. Stub flows only (no model call)."""
from __future__ import annotations

import psycopg2

from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj, understand
from tests.final_loop.test_chg04_review import login, prepared, review
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)


def diff(q, client, cid):
    return client.get(f"/v2/business/changes/{cid}/file-diff", params={"workspace_id": q.env.ws})


def state(q):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        out = []
        for sql in ("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s", "SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s",
                    "SELECT count(*) FROM v2_business_change_decisions WHERE tenant_id=%s AND workspace_id=%s"):
            cur.execute(sql, (q.env.tenant, q.env.ws))
            out.append(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        out.append(cur.fetchone()[0])
        return out


def test_the_candidate_diff_comes_from_the_pinned_bytes_changes_only_the_edited_scalar_and_writes_nothing(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    tech = review(q, approver, cid, "technical").json()
    auto = [f for f in tech["draft"]["technical"]["file_actions"] if f["action"] == "AI_EDIT_SUPPORTED"] if "file_actions" in tech["draft"].get("technical", {}) else None
    before = state(q)
    r = diff(q, approver, cid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert state(q) == before                                                                 # no job, audit, decision or source revision
    assert body["coverage"]["auto_files"] == len(body["files"]) == 1 and body["coverage"]["files_not_shown"] == 0 and body["coverage"]["unresolved_items"] >= 1
    f = body["files"][0]
    assert f["state"] == "candidate" and f["before_hash"] and f["after_hash"].startswith("sha256:") and f["path"].endswith((".yaml", ".yml"))
    removed = [x["text"] for x in f["lines"] if x["kind"] == "remove"]
    added = [x["text"] for x in f["lines"] if x["kind"] == "add"]
    assert len(removed) == len(added) == 1 and removed[0] != added[0]                       # exactly one scalar line changes
    assert removed[0].split(":")[0] == added[0].split(":")[0]                               # same key, only its value differs
    assert not f["truncated"] and "Nothing was staged" in body["statement"]
    if auto is not None:
        assert f["refs"] == [a["ref"] for a in auto]
    assert diff(q, approver, cid).json() == body                                              # deterministic


def test_file_contents_need_evidence_rights_and_an_unconfirmed_or_foreign_request_shows_nothing(qa_env):
    q = qa_env
    got, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]))
    assert diff(q, q.req, got["change_id"]).status_code in (403, 409)                         # not confirmed yet (or no evidence right): no bytes either way
    cid, _approver = prepared(q)
    viewer = login(q, f"viewer-d-{q.env.nonce}", ("viewer",))
    assert diff(q, viewer, cid).status_code == 404                                            # a stranger does not learn the request exists
    plain = login(q, f"approver-noev-{q.env.nonce}", ("change_approver",))
    r = diff(q, plain, cid)
    files_shown = review(q, plain, cid, "technical").json()["draft"].get("files_withheld", 0) == 0
    assert (r.status_code == 200) == files_shown, (r.status_code, files_shown)                # the same evidence rule as the review's file details
    if r.status_code == 403:
        assert r.json()["code"] == "evidence_rights_required" and "lines" not in r.text
    assert diff(q, plain, "chg-nonexistent").status_code == 404 and diff(q, plain, "x").status_code == 404


def test_the_citation_excerpt_reads_the_pinned_yaml_through_the_blob_seam_not_the_route_store(qa_env):
    """Regression: the route store has no blob port, so the citation-source YAML excerpt was always empty. The route now reads through the source blob seam."""
    from infra import v2_request_assistant_routes as rar
    from infra.adapters import v2_business_review, v2_request_threads as rt
    from infra.v2_policy_routes import _v2_store
    q = qa_env
    cid, approver = prepared(q)
    draft = v2_business_review.current_draft(_v2_store(), q.env.tenant, q.env.ws, cid)[0]
    item = next(i for i in draft["file_actions"] if i["action"] == "AI_EDIT_SUPPORTED")
    edit, pin = item["proposal_edit"], draft["pin"]["source_revision_id"]
    removed = next(x["text"] for x in diff(q, approver, cid).json()["files"][0]["lines"] if x["kind"] == "remove")
    route_store = _v2_store()
    assert rt._yaml_excerpt(route_store, q.env.tenant, q.env.ws, pin, item["resolved_artifact_id"], item["path"], edit["key_path"]) is None   # the old wiring: no bytes
    got = rt._yaml_excerpt(rar._with_blobs(route_store), q.env.tenant, q.env.ws, pin, item["resolved_artifact_id"], item["path"], edit["key_path"])
    assert got == f"{edit['key_path']}: {edit['expected_current']}" or got.split(": ", 1)[1].replace(",", "") == str(edit["expected_current"]).replace(",", "")
    assert removed.strip().split(":")[0] == edit["key_path"].split(".")[-1]                   # the same key the byte diff changes
