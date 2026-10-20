"""DOC-04 browser journey: the Knowledge Documents page generates a document only when a topic is opened, shows it through the safe renderer with its freshness,
keeps the open document across a reload (URL state), labels a document of an older knowledge version as stale and generates a current one only on an explicit
action, and refuses the Technical document to an account without evidence rights without showing anything in its place. Reuses the CHG-04 browser fixture
(live worker renders the documents; no model). Simulated roles; not human acceptance."""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.final_loop.test_chg04_browser import ui  # noqa: E402,F401  (ui is a fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import WAIT, sign_in  # noqa: E402


def listed(admin, env):
    return admin.get("/v2/business/documents", params={"workspace_id": env.ws}).json()["documents"]


def test_open_generates_once_reload_keeps_it_stale_is_labelled_and_technical_obeys_rights(ui, pw_browser):
    env, admin, model, server, approver, viewer = ui
    assert listed(admin, env) == []                                                                       # accepting knowledge generated nothing
    owner = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash="documents")
    expect(owner.get_by_test_id("docs-topics")).to_be_visible(timeout=WAIT)
    expect(owner.get_by_test_id("docs-empty")).to_be_visible()
    assert listed(admin, env) == []                                                                       # opening the page generated nothing either
    expect(owner.get_by_test_id("docs-summary")).to_contain_text("Not generated yet")
    owner.get_by_test_id("docs-topic").filter(has_text="whole business knowledge").click()
    expect(owner.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "available", timeout=WAIT)
    expect(owner.get_by_test_id("docs-content")).to_contain_text("Purchases of 1,000,000 JPY", timeout=WAIT)
    expect(owner.get_by_test_id("docs-freshness")).to_contain_text("Current")
    assert owner.locator("[data-testid=docs-content] script, [data-testid=docs-content] img, [data-testid=docs-content] iframe").count() == 0
    assert "doc=" in owner.url
    docs = listed(admin, env)
    assert len(docs) == 1 and docs[0]["mode"] == "business" and docs[0]["freshness"] == "available"
    aid = docs[0]["artifact_id"]
    with owner.expect_download() as dl:                                                                   # DOC-05: the downloaded file is the viewed bytes
        owner.get_by_test_id("docs-download").click()
    saved = dl.value
    assert saved.suggested_filename.startswith("knowledge-business-rev1-") and saved.suggested_filename.endswith(".md")
    body = open(saved.path(), "rb").read()
    shown = admin.get(f"/v2/business/documents/{aid}/content", params={"workspace_id": env.ws})
    assert body == shown.content and hashlib.sha256(body).hexdigest() == hashlib.sha256(shown.content).hexdigest()
    with owner.expect_download() as dz:                                                                   # the bundle of this version (admin: both modes); the technical one is generated on demand
        owner.get_by_test_id("docs-export").click()
    zbytes = open(dz.value.path(), "rb").read()
    names = sorted(zipfile.ZipFile(io.BytesIO(zbytes)).namelist())
    assert "MANIFEST.json" in names and len(names) == 3 and dz.value.suggested_filename.startswith("knowledge-rev1-")
    assert len(listed(admin, env)) == 2                                                                   # business (viewed) + technical (for the bundle); nothing else
    owner.reload()                                                                                        # URL state: the same document reopens, no new artifact
    expect(owner.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "available", timeout=WAIT)
    expect(owner.get_by_test_id("docs-content")).to_contain_text("Purchases of 1,000,000 JPY", timeout=WAIT)
    owner.get_by_test_id("docs-topic").filter(has_text="whole business knowledge").click()                # opening the same topic again: the same artifact, nothing new
    expect(owner.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "available", timeout=WAIT)
    assert aid in [d["artifact_id"] for d in listed(admin, env)] and len(listed(admin, env)) == 2
    with env.store._tx() as cur:                                                                          # the knowledge moves (a decision is revoked): the document becomes stale, nothing regenerates
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (env.tenant,))
        decision = cur.fetchone()[0]
    assert admin.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    owner.reload()
    expect(owner.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "stale", timeout=WAIT)
    expect(owner.get_by_test_id("docs-freshness")).to_contain_text("Older version")
    expect(owner.get_by_test_id("docs-version")).to_contain_text("older saved knowledge version")      # UX-12: from the document's pinned version
    expect(owner.get_by_test_id("docs-version")).to_have_attribute("data-pinned", "older")
    expect(owner.get_by_test_id("docs-content")).to_contain_text("Purchases of 1,000,000 JPY")             # the old bytes are still shown, labelled
    assert len(listed(admin, env)) == 2                                                                   # nothing regenerated by the knowledge change
    owner.get_by_test_id("docs-refresh").click()                                                          # explicit: a new artifact for the current version
    expect(owner.get_by_test_id("docs-reader")).to_have_attribute("data-freshness", "available", timeout=WAIT)
    expect(owner.get_by_test_id("docs-freshness")).to_contain_text("Current", timeout=WAIT)
    expect(owner.get_by_test_id("docs-version")).to_have_attribute("data-pinned", "current")
    expect(owner.get_by_test_id("docs-version")).to_contain_text("(the current one)")
    ids = {d["artifact_id"] for d in listed(admin, env)}
    assert len(ids) == 3 and aid in ids
    v = sign_in(pw_browser, server, env.ws, viewer, "cv", page_hash="documents?mode=technical")           # a viewer may read Business, not Technical
    expect(v.get_by_test_id("docs-topics")).to_be_visible(timeout=WAIT)
    v.get_by_test_id("docs-topic").filter(has_text="whole business knowledge").click()
    expect(v.get_by_test_id("docs-error")).to_contain_text("needs rights", timeout=WAIT)
    assert v.get_by_test_id("docs-content").count() == 0
    assert len(listed(admin, env)) == 3 and model.calls == 0                                             # nothing generated for the refused request; no model anywhere
    for page in (owner, v):
        assert not page._errors, page._errors


def test_topic_filter_narrows_the_loaded_topics_without_a_request_and_clearing_restores_them(ui, pw_browser):
    """UX-12 acceptance: with more than six topics a "Find a topic" filter narrows the loaded list; typing sends no request, a miss says so with the
    loaded count, clearing lists everything again. The fixture has fewer topics, so the real graph response is extended in the browser with eight
    SYNTHETIC rule nodes (titles "Synthetic topic N"); none of them is opened, so no document is requested for them."""
    import json as _json
    env, admin, model, server, _approver, _viewer = ui

    def extend(route):
        resp = route.fetch()
        body = resp.json()
        body["nodes"] = list(body.get("nodes") or []) + [{"id": f"rule-synthetic-{n}", "type": "rule", "title": f"Synthetic topic {n}", "authority": "OBSERVED"} for n in range(8)]
        route.fulfill(response=resp, body=_json.dumps(body), content_type="application/json")

    owner = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}", page_hash="overview")
    owner.route("**/v2/business/graph*", extend)
    calls = []
    owner.on("request", lambda r: calls.append((r.method, r.url)) if "/v2/" in r.url else None)
    owner.goto(f"{server.url}/app-next/#/ws/{env.ws}/documents")
    box = owner.get_by_test_id("docs-topic-filter")
    expect(box).to_be_visible(timeout=WAIT)
    topics = owner.get_by_test_id("docs-topics")
    before = len(calls)
    box.fill("Synthetic topic 3")
    expect(topics).to_contain_text("Synthetic topic 3")
    expect(topics).not_to_contain_text("Synthetic topic 4")
    box.fill("no such topic at all")
    expect(owner.get_by_test_id("docs-topic-none")).to_contain_text("topics are loaded")
    box.fill("")
    for n in (0, 7):
        expect(topics).to_contain_text(f"Synthetic topic {n}")
    owner.wait_for_timeout(300)
    assert not [c for c in calls[before:] if "/v2/business/documents" in c[1] or c[0] != "GET"], calls[before:]      # filtering requested nothing
    assert not owner._errors, owner._errors
