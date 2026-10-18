"""ext/1 UI (X1a + X1b) in the React build (/app-next/), LOCAL ONLY on a fixture repository: the business-change review shows the code recommendations of a
confirmed change as recommendations (never a diff), says why no candidate can be built before the commit, and after the commit lets the requester build and
validate a candidate on the declared repository while the approver decides it with a reason bound to both hashes; publishing stays refused without a
pull_request grant. Buttons follow the server's `can` hint; the server re-checks every action. Real server, worker (drained here), store, Chromium; STUB
understanding of the change request only, 0 provider calls otherwise. Simulated roles, not human acceptance."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.fixtures import pw_browser  # noqa: E402,F401
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_chg02_impact import confirmed  # noqa: E402
from tests.final_loop.test_chg04_review import login  # noqa: E402
from tests.final_loop.test_fex_code_change import _committed, _config, fixture_repo  # noqa: E402,F401
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401
from tests.final_loop.test_qry04_browser import sign_in  # noqa: E402
from tests.final_loop.test_syn01_code import FREIGHT_REQ, OP_FREIGHT, code_env  # noqa: E402,F401
from tests.pitch_brief.conftest import drain  # noqa: E402

WAIT = 40000


def _until(q, page, testid, attr, value, tries=60):
    for _ in range(tries):
        drain(q.env)
        loc = page.get_by_test_id(testid)
        if loc.count() and loc.first.get_attribute(attr) == value:
            return
        time.sleep(0.5)
    expect(page.get_by_test_id(testid)).to_have_attribute(attr, value, timeout=1000)


def test_recommendations_then_candidate_validation_decision_and_refused_publish_in_the_review(code_env, fixture_repo, monkeypatch, pw_browser):
    q = code_env
    repo, target = fixture_repo
    cfg = _config(q.env.ws, repo, target)
    cfg["workspaces"][q.env.ws]["repos"]["fixture"]["display_name"] = "Harbor app (fixture)"
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(cfg))
    early, _ = confirmed(q, FREIGHT_REQ, [OP_FREIGHT])                                     # confirmed, not committed
    server = ServerThread().start()
    try:
        req = sign_in(pw_browser, server, q.env.ws, q.env.req, f"cred-r-{q.env.nonce}", page_hash=f"questions?chg={early}")
        expect(req.get_by_test_id("code-recs")).to_contain_text("no code has been changed or prepared", timeout=WAIT)
        expect(req.get_by_test_id("code-candidate-later")).to_contain_text("Commit the knowledge change first")
        assert req.get_by_test_id("code-candidate").count() == 0
        # the same request by the same person on the same knowledge is the same change (its id is derived from them): commit it now
        cid, _approver, _committed_out = _committed(q)
        assert cid == early
        login(q, f"appr-ui-{q.env.nonce}", ("change_approver",), cred="cv")
        req.goto("about:blank")
        req.goto(f"{server.url}/app-next/#/ws/{q.env.ws}/questions?chg={cid}")
        recs = req.get_by_test_id("code-recs")
        expect(recs.get_by_test_id("code-rec-item").first).to_be_visible(timeout=WAIT)
        assert "diff" not in recs.inner_text().lower()                                       # recommendations are never presented as a diff
        cand = req.get_by_test_id("code-candidate")
        expect(cand.get_by_test_id("code-boundary")).to_contain_text("nothing is ever merged")
        req.get_by_test_id("code-repo-select").select_option("fixture")
        expect(req.get_by_test_id("code-build")).to_be_enabled(timeout=WAIT)
        req.get_by_test_id("code-build").click()
        _until(q, req, "code-candidate-state", "data-status", "PROPOSED")
        expect(req.get_by_test_id("code-binding")).to_contain_text("Patch")
        expect(req.get_by_test_id("code-not-implemented")).to_be_visible()
        assert req.get_by_test_id("code-approve").count() == 0 and req.get_by_test_id("code-publish").count() == 0     # outside the requester's role: absent
        req.get_by_test_id("code-validate").click()
        _until(q, req, "code-validation", "data-status", "PASSED")
        assert not req._errors, req._errors
        appr = sign_in(pw_browser, server, q.env.ws, f"appr-ui-{q.env.nonce}", "cv", page_hash=f"questions?chg={cid}")
        approve = appr.get_by_test_id("code-approve")
        expect(approve).to_be_disabled(timeout=WAIT)
        expect(appr.get_by_test_id("code-approve-why")).to_have_text("Write your reason first.")                       # a disabled action shows why
        appr.get_by_test_id("code-reason").fill("Read the patch: the literal is the decided value.")
        expect(approve).to_be_enabled()
        approve.click()
        expect(appr.get_by_test_id("code-approval")).to_contain_text(f"appr-ui-{q.env.nonce}", timeout=WAIT)
        expect(appr.get_by_test_id("code-publish")).to_be_disabled()
        expect(appr.get_by_test_id("code-publish-why")).to_contain_text("does not allow opening a pull request")
        st = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
        assert st["approval"]["actor_id"] == f"appr-ui-{q.env.nonce}" and st["pull_request"] is None                     # nothing published
        assert not appr._errors, appr._errors
    finally:
        server.stop()
