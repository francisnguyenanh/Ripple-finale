"""Browser: the three defects found on the cloud (2026-10-12) as a person sees them. Real server, real Chromium, stub provider.
1+2. a reply to an open point: the preview states "nothing is recorded until you confirm", shows the person's words exactly as typed and the files they named
     (with their role), the AI wording only as a supplement; "recorded" appears only after the server stored the decision.
3.   a revised draft still lists what the earlier draft left open (Business and Technical view), with "kept from the earlier draft"."""
from __future__ import annotations

import re
import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from infra.worker import change_draft, intent_interpret  # noqa: E402
from tests.ai10ui.contract import select_workspace, tech_url, tid, UI_BASE  # noqa: E402
from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.pitch_brief.chain import build  # noqa: E402
from tests.pitch_brief.conftest import create_job, drain, job_body  # noqa: E402
from tests.pitch_brief.test_change_draft import Model, _res, run  # noqa: E402
from tests.pitch_brief.test_intent_chat import Interp  # noqa: E402
from tests.pitch_brief.test_ui_flow import _page  # noqa: E402

WAIT = 40000
QUESTION = "Should the other files also be updated?"


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _open(page, url):
    page.goto("about:blank")
    page.goto(tech_url(url))


def _first_draft(env, ch, monkeypatch):
    def first(obj, prompt):
        obj["questions_for_human"] = [QUESTION]
        for it in obj["items"]:
            if it["action"] == "manual":
                it["action"] = "ambiguity"
    return run(env, ch, monkeypatch, Model(mutate=first))


def test_preview_words_files_and_the_recorded_state_in_the_browser(env, ch, monkeypatch, pw_browser):
    jid, body = _first_draft(env, ch, monkeypatch)
    intent_id = _res(body)["intent"]["intent_id"]
    arts = env.store.source_revision_view(env.tenant, env.ws, env.store.head(env.tenant, env.ws).source_revision_id)["artifacts"]
    names = [a["logical_path"].split("/")[-1] for a in arts]
    typed = f"Keep this change limited to {names[0]}. Do not update {names[1]} in this request. Keep it unresolved."
    reply = {"status": "READY", "message": "Your decision has been recorded.", "questions": [], "assumptions": [],
             "fields": {"answer": "Leave the second file out of this request.", "kind": "decision", "wish_or_fact": "wish", "rationale": None, "quote": "Do not update"}}
    model = Interp(reply)
    monkeypatch.setattr(intent_interpret, "_client_for_model", lambda m: model)
    monkeypatch.setattr(intent_interpret, "_assert_global_endpoint", lambda: None)
    server = ServerThread().start()
    try:
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(admin, env.ws)
        _open(admin, f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={jid}")
        gap = admin.locator("[data-testid=gap-item][data-kind=question]")
        gap.get_by_test_id("gap-reply-chat").click()
        tid(admin, "gr-input").fill(typed)
        tid(admin, "gr-start").click()
        expect(tid(admin, "gr-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        drain(env)
        expect(tid(admin, "gr-understanding")).to_have_attribute("data-state", "READY", timeout=WAIT)
        turn = admin.locator("[data-testid=chat-turn][data-role=assistant]").last
        expect(turn).to_contain_text("Nothing is recorded until you confirm")
        assert "has been recorded" not in turn.inner_text()                                  # the model's own sentence never states the state
        expect(tid(admin, "gr-field-typed")).to_contain_text(typed)                           # exactly as typed
        files = admin.locator("[data-testid=gr-file]")
        assert files.count() == 2
        expect(files.nth(0)).to_have_attribute("data-role", "in_scope")
        expect(files.nth(1)).to_have_attribute("data-role", "not_changed")
        expect(files.nth(1)).to_contain_text("NOT resolved")                                  # "not changed" is never "resolved"
        expect(tid(admin, "gr-field-answer")).to_contain_text("Leave the second file out")    # the AI wording is there, labelled, beside the words
        assert "AI wording" in tid(admin, "gr-field-answer").locator("xpath=..").inner_text()
        assert "Recorded by the server" not in admin.locator("[data-testid=chat-turns]").inner_text()
        tid(admin, "gr-confirm").click()
        expect(tid(admin, "gr-run")).to_be_visible(timeout=WAIT)
        assert "Recorded by the server" not in admin.locator("[data-testid=chat-turns]").inner_text()      # confirmed is still not recorded
        tid(admin, "gr-run").click()
        # after the server stored the decision the open point shows who decided and the person's own words; the chat panel gives way to it
        confirmation = admin.locator("[data-testid=gap-item][data-kind=question]").get_by_test_id("gap-confirmation")
        expect(confirmation).to_contain_text(env.admin, timeout=WAIT)
        expect(confirmation).to_contain_text(typed)                                           # the stored decision text IS the typed text
        api = env.admin_client()
        cid = api.get("/v2/chat", params={"workspace_id": env.ws}).json()["conversations"][0]["conversation_id"]
        turns = [t["text"] for t in api.get(f"/v2/chat/{cid}", params={"workspace_id": env.ws}).json()["turns"] if t["role"] == "system"]
        assert any(t.startswith("Recorded by the server") for t in turns), turns             # said by the server, after the save
        assert not admin._errors, admin._errors
    finally:
        server.stop()


def test_a_revised_draft_still_shows_what_the_earlier_draft_left_open_in_both_views(env, ch, monkeypatch, pw_browser):
    d1, body = _first_draft(env, ch, monkeypatch)
    r1 = _res(body)
    intent_id = r1["intent"]["intent_id"]
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit this to the YAML edit."}).status_code == 200

    def second(obj, prompt):
        obj["questions_for_human"] = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"], it["rationale"] = "exclude", "limited to the one file"
    model = Model(mutate=second)
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    d2, r = create_job(env.req_client(), env, "change.draft", {"intent_id": intent_id, "extraction_job_ids": list(ch.jobs.values()), "revises_draft_job_id": d1})
    assert r.status_code == 200, r.text
    drain(env)
    r2 = _res(job_body(ch.admin, env, d2))
    assert r2["carried_unresolved"]
    server = ServerThread().start()
    try:
        for who, cred in ((env.req, f"cred-r-{env.nonce}"), (env.admin, f"cred-a-{env.nonce}")):
            page = _page(pw_browser, server, (who, cred))
            select_workspace(page, env.ws)
            for mode in ("", "&mode=technical"):
                _open(page, f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={d2}{mode}")
                expect(tid(page, "draft-status")).to_be_visible(timeout=WAIT)
                carried = page.locator("[data-testid=draft-item-carried]")
                expect(carried.first).to_be_visible(timeout=WAIT)
                assert carried.count() >= len(r2["carried_unresolved"]) - len(r2["unaddressed"])
                expect(carried.first).to_contain_text("kept from the earlier draft")
                expect(carried.first).to_contain_text(re.compile("Still unresolved|Potentially affected"))      # the server decides which kind of open; never "resolved"
                gaps = page.locator("[data-testid=gap-item][data-kind=item]")
                texts = gaps.all_inner_texts()
                assert any("kept from the earlier draft" in g for g in texts), texts
                assert gaps.locator("[data-testid=gap-carried]").count() >= 1
            assert not page._errors, page._errors
    finally:
        server.stop()


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _raw_texts(res):
    out = [u["text"] for u in res.get("unaddressed") or [] if u.get("text")]
    out += [(x.get("citation") or {}).get("quote") for x in res.get("items") or [] if (x.get("citation") or {}).get("quote")]
    return [s for s in out if len(s) > 12]


def test_members_without_evidence_read_see_no_raw_source_passage_in_either_view(env, ch, monkeypatch, pw_browser):
    from infra.adapters.v2_identity import PostgresIdentityStore
    jid, body = _first_draft(env, ch, monkeypatch)
    res = _res(body)
    raw = _raw_texts(res)
    assert raw
    ids = PostgresIdentityStore(env.dsn)
    acct = f"apv-{env.nonce}"
    ids.create_account(acct, f"cred-apv-{env.nonce}")
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, acct, ("change_approver",))
    server = ServerThread().start()
    try:
        for who, cred, sees in ((env.req, f"cred-r-{env.nonce}", False), (acct, f"cred-apv-{env.nonce}", False), (env.admin, f"cred-a-{env.nonce}", True)):
            page = _page(pw_browser, server, (who, cred))
            leaks: list = []

            def _watch(resp, leaks=leaks):
                try:
                    if "/v2/" in resp.url and any(r in v for v in _strings(resp.json()) for r in raw):
                        leaks.append(resp.url)
                except Exception:           # noqa: BLE001 - a response that cannot be read is not a leak
                    pass
            page.on("response", _watch)
            select_workspace(page, env.ws)
            for mode in ("", "&mode=technical"):
                _open(page, f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={res['intent']['intent_id']}&draft={jid}{mode}")
                expect(tid(page, "draft-status")).to_be_visible(timeout=WAIT)
                page.wait_for_timeout(1500)
                text = page.inner_text("body")
                hits = [r for r in raw if r in text]
                if sees:
                    assert hits, "the member WITH evidence.read should still see the source passages"
                else:
                    assert not hits, f"{who} sees raw source text in the page ({mode or 'business'}): {hits[0][:50]!r}; responses carrying it: {sorted(set(leaks))}"
                    assert tid(page, "source-text-hidden").count() >= 1
            assert not page._errors, page._errors
    finally:
        server.stop()


def test_a_refused_record_is_shown_as_refused_never_as_recorded(env, ch, monkeypatch, pw_browser):
    jid, body = _first_draft(env, ch, monkeypatch)
    intent_id = _res(body)["intent"]["intent_id"]
    reply = {"status": "READY", "message": "ok", "questions": [], "assumptions": [],
             "fields": {"answer": "Leave the second file out.", "kind": "decision", "wish_or_fact": "wish", "rationale": None, "quote": "Do not"}}
    model = Interp(reply)
    monkeypatch.setattr(intent_interpret, "_client_for_model", lambda m: model)
    monkeypatch.setattr(intent_interpret, "_assert_global_endpoint", lambda: None)
    server = ServerThread().start()
    try:
        admin = _page(pw_browser, server, (env.admin, f"cred-a-{env.nonce}"))
        select_workspace(admin, env.ws)
        _open(admin, f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={jid}")
        gap = admin.locator("[data-testid=gap-item][data-kind=question]")
        gap.get_by_test_id("gap-reply-chat").click()
        tid(admin, "gr-input").fill("Do not update the second file. Keep it unresolved.")
        tid(admin, "gr-start").click()
        expect(tid(admin, "gr-panel")).to_have_attribute("data-state", "OPEN-ai", timeout=WAIT)
        drain(env)
        expect(tid(admin, "gr-understanding")).to_have_attribute("data-state", "READY", timeout=WAIT)
        tid(admin, "gr-confirm").click()
        expect(tid(admin, "gr-run")).to_be_visible(timeout=WAIT)
        turns = admin.locator("[data-testid=chat-turns]")
        expect(turns).to_contain_text("Not done at this step: the decision is not recorded and no source file was changed")
        admin.route("**/v2/change/gap/confirm", lambda route: route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "the workspace changed while you were deciding"})))
        tid(admin, "gr-run").click()
        admin.wait_for_timeout(1500)
        assert "Recorded by the server" not in turns.inner_text()
        assert "Decision recorded" not in admin.inner_text("body") and tid(admin, "gr-run").count() == 1      # still offered: nothing was recorded
        assert not admin._errors or all("409" in e for e in admin._errors), admin._errors
    finally:
        server.stop()


def test_approver_and_reviewer_see_what_is_left_for_people_in_the_proposal_of_a_revised_draft(env, ch, monkeypatch, pw_browser):
    """The proposal of a REVISED draft keeps every item the earlier drafts left open, and the people who review and approve it see the same list (read-only), in both views."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.pitch_brief.test_draft_to_proposal import _reports
    from tests.pitch_brief.test_change_draft import APPROVAL
    d1, body = _first_draft(env, ch, monkeypatch)
    r1 = _res(body)
    intent_id = r1["intent"]["intent_id"]
    q = next(x for x in ch.admin.get("/v2/change/gaps", params={"workspace_id": env.ws, "draft_job_id": d1}).json()["gaps"] if x["kind"] == "question")
    assert ch.admin.post("/v2/change/gap/confirm", json={"workspace_id": env.ws, "draft_job_id": d1, "gap_key": q["key"], "answer": "Limit this to the YAML edit."}).status_code == 200

    def second(obj, prompt):
        obj["questions_for_human"] = []
        for it in obj["items"]:
            if it["action"] in ("manual", "ambiguity"):
                it["action"] = "exclude"
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: Model(mutate=second))
    d2, r = create_job(env.req_client(), env, "change.draft", {"intent_id": intent_id, "extraction_job_ids": list(ch.jobs.values()), "revises_draft_job_id": d1})
    assert r.status_code == 200, r.text
    drain(env)
    res2 = _res(job_body(ch.admin, env, d2))
    ij, sj = _reports(env, ch, intent_id)
    pj, r = create_job(env.req_client(), env, "proposal", {"intent_id": intent_id, "impact_job_id": ij, "simulation_job_id": sj, "target_artifact_id": ch.arts[APPROVAL],
                                                           "edit_kind": "yaml_typed_edit", "draft_job_id": d2})
    assert r.status_code == 200, r.text
    drain(env)
    prop = _res(job_body(ch.admin, env, pj))
    left = prop["draft_binding"]["left_for_humans"]
    carried_ref = {c["ref"] for c in res2["carried_unresolved"]}
    assert carried_ref and carried_ref <= {x["ref"] for x in left if x.get("carried_from")}
    ids = PostgresIdentityStore(env.dsn)
    members = {}
    for name, role in (("apv", "change_approver"), ("rev", "reviewer")):
        acct = f"{name}-{env.nonce}"
        ids.create_account(acct, f"cred-{name}-{env.nonce}")
        ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, acct, (role,))
        members[name] = (acct, f"cred-{name}-{env.nonce}")
    server = ServerThread().start()
    try:
        for who, cred in (members["apv"], members["rev"], (env.req, f"cred-r-{env.nonce}"), (env.admin, f"cred-a-{env.nonce}")):
            page = _page(pw_browser, server, (who, cred))
            select_workspace(page, env.ws)
            for mode in ("", "&mode=technical"):
                _open(page, f"{server.url}{UI_BASE}#/ws/{env.ws}/changes?intent={intent_id}&draft={d2}&impact={ij}&simulation={sj}&proposal={pj}{mode}")
                page.get_by_test_id("flow-proposal").wait_for(state="attached", timeout=WAIT)
                page.wait_for_timeout(2500)
                items = [t for t in page.locator("[data-testid=left-for-humans]").all_text_contents()]
                assert len(items) == len(left), (who, mode, len(items), len(left))
                assert sum("kept from the earlier draft" in t for t in items) >= len(carried_ref), items
            assert not page._errors, page._errors
    finally:
        server.stop()
