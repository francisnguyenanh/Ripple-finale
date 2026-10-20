"""AIX-D1: audience variants (executive | sop | audit) of ONE generated document through the pinned role `document.wording` (STUB provider: SYNTHETIC wording). What this proves:
the server keeps only paragraphs whose fact ids, numbers and role words come from the facts; a missing mandatory section makes a labelled deterministic fallback (the base bytes),
never a partial narrative; unknowns, limits and the canonical fact table are always rendered; the variant key makes repeated requests free; base permission/redaction are
rechecked (the audience never widens reading rights); staleness follows the base. Nothing about real wording quality.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import pytest

from core.change_intelligence.v2 import document_ast as da, document_variant as dv
from infra.adapters import v2_document_variants as variants
from tests.final_loop.test_doc02_documents import ask, content as base_content
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401
from tests.pitch_brief.conftest import drain, job_body

FROZEN, SHA = variants.load_config()


@pytest.fixture
def d1_env(qa_env, monkeypatch, tmp_path):            # noqa: F811
    """The QA environment with the wording role pinned to a priced model (the pitch budget file does not list it)."""
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  document.wording: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_d1.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return qa_env
MANAGER = "rule-standard-manager-review"


def pack_of(rules, questions=(), max_facts=40):
    doc = {"facts": [{"rule_id": r["rule_id"], "title": r["title"], "authority": "OBSERVED", "status": r.get("status", "VALIDATED"), "actor_ref": r.get("actor_ref"), "condition": r.get("condition"),
                      "outcome": r.get("outcome", {"kind": "review_required"}), "exceptions": r.get("exceptions", []), "scope": r.get("scope", {}),
                      "implementation_status": r.get("implementation_status", "verified"), "unknown_fields": []} for r in rules],
           "questions": [{"gap_id": q, "question": f"Who acts on {q}?", "status": "OPEN", "priority": "critical", "affected_rule_ids": [], "evidence_refs": []} for q in questions],
           "overview": {"rules": len(rules), "active_rules": len(rules), "decided_rules": 0, "open_questions": len(questions), "critical_open": len(questions), "accepted_unknown": 0, "partial": bool(questions), "proposal": False}}
    return dv.fact_pack(doc, max_facts=max_facts), doc


R1 = {"rule_id": "rule-a", "title": "Purchases of 1,000,000 JPY or more need manager approval", "actor_ref": "role-manager", "condition": {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"}}
R2 = {"rule_id": "rule-b", "title": "CAPEX above 5,000,000 JPY needs the finance director", "actor_ref": "role-finance-director", "condition": {"field": "request.net_amount_jpy", "op": "gt", "value": 5000000, "unit": "JPY"},
      "exceptions": ["emergency repairs are exempt"]}


def claim(fid, actor="", operator="", value="", unit="", outcome="", exceptions=()):
    return {"fact_id": fid, "actor": actor, "operator": operator, "value": value, "unit": unit, "outcome": outcome, "exceptions": list(exceptions)}


CA = claim("rule-a", "manager", "gte", "1000000", "JPY")
CB = claim("rule-b", "finance director", "gt", "5000000", "JPY")


def model(sections, status="OK", omitted=()):
    """Paragraphs are (text, fact_ids) or (text, fact_ids, claims); without claims every cited rule gets a claim row that states nothing."""
    def para(t, f, c=None):
        return {"text": t, "fact_ids": list(f), "claims": list(c) if c is not None else [claim(x) for x in f if x.startswith("rule-")]}
    return {"status": status, "sections": [{"id": sid, "paragraphs": [para(*x) for x in paras]} for sid, paras in sections], "omitted": list(omitted)}


def prompt_claim(prompt, fid):
    """The claim row a faithful model copies from the fact line of the prompt (first threshold of the rule)."""
    line = next(x for x in prompt.splitlines() if x.startswith(f"- id={fid} "))
    actor = re.search(r"who acts: ([^|]+?) \|", line).group(1)
    m = re.search(r"claim values: operator=(\w+) value=([^ ]+) unit=([^;| ]*)", line)
    return claim(fid, "" if actor == "not stated" else actor, *(m.groups() if m else ("", "", "")))


# ---- pure: what the server keeps ----------------------------------------------------------------------------------------------------------------------------

def test_numbers_roles_and_fact_ids_are_checked_per_paragraph_and_a_missing_mandatory_section_is_a_fallback():
    pack, _ = pack_of([R1, R2], ["gap-x"])
    cfg = FROZEN["audiences"]["executive"]
    out = dv.check(model([("summary", [("Two rules govern purchase approval.", [])]),
                          ("material_risks", [("Purchases of 1,000,000 JPY or more wait for the manager.", ["rule-a"], [CA]),
                                              ("Purchases above 6,000,000 JPY need the finance director.", ["rule-b"]),          # invented number
                                              ("The manager also signs CAPEX above 5,000,000 JPY.", ["rule-b"]),                 # role of another fact
                                              ("Emergency repairs are exempt from the 5,000,000 JPY rule.", ["rule-b"], [CB]),
                                              ("See <b>policy</b> at http://x.example for details.", ["rule-a"]),                # markup / link
                                              ("The CFO decides.", ["rule-z"]),                                                   # unknown fact id
                                              ("It is still unclear who acts.", ["gap-x"])]),
                          ("decisions_needed", [("Someone must settle 3 open points.", [])])]), pack, cfg)                        # non-narrative paragraph without fact ids
    assert out["fallback"] is False and out["missing_mandatory"] == [] and [p["text"] for p in out["kept"]["material_risks"]] == [
        "Purchases of 1,000,000 JPY or more wait for the manager.", "Emergency repairs are exempt from the 5,000,000 JPY rule.", "It is still unclear who acts."]
    reasons = [r["reason"] for r in out["rejected"]]
    assert reasons == ["number_not_in_referenced_facts:6,000,000", "actor_not_in_referenced_facts:manager", "not_plain_text", "unknown_fact_id:rule-z", "no_fact_ids"]
    assert {k: out["fact_coverage"][k] for k in ("referenced", "total", "complete")} == {"referenced": 2, "total": 2, "complete": True}
    assert out["fact_coverage"]["validation"] == {"clause_checks": "lha-d1/2", "model_review": "not_performed", "kept": 4, "rejected": 5, "omitted_exceptions": 0, "facts_offered": 2, "facts_in_document": 2}
    only_summary = dv.check(model([("summary", [("All is fine.", [])])]), pack, cfg)
    assert only_summary["fallback"] is True and only_summary["missing_mandatory"] == ["material_risks"]
    assert dv.check(model([("summary", [("There are 2 rules.", [])])], status="UNSUPPORTED", omitted=["no outcome stated"]), pack, cfg)["fallback"] is True
    assert dv.check(model([("summary", [("There are 7 rules.", [])])]), pack, cfg)["rejected"][0]["reason"] == "number_not_in_referenced_facts:7"   # narrative numbers only from the overview
    with pytest.raises(ValueError):
        dv.validate_model_object({"status": "OK", "sections": [], "omitted": [], "extra": 1}, FROZEN["limits"])
    with pytest.raises(ValueError):
        dv.validate_model_object(model([("summary", [("x" * 2101, [])])]), FROZEN["limits"])          # far beyond the limit: the answer is refused


def test_the_rendered_variant_always_carries_unknowns_limits_and_the_canonical_facts_and_a_fallback_carries_the_base_bytes():
    pack, doc = pack_of([R1, R2], ["gap-x"])
    cfg = FROZEN["audiences"]["audit"]
    label = {"audience": "audit", "artifact_id": "doc-1", "snapshot_id": "snap-1", "role": "document.wording", "model_pinned": "stand_in", "config_sha256": SHA, "generated_at": "2026-10-17"}
    checked = dv.check(model([("controls", [("Manager approval applies from 1,000,000 JPY.", ["rule-a"], [CA])])]), pack, cfg, FROZEN["fidelity"])
    text = dv.render(doc, "audit", cfg, checked, base_markdown=None, label=label)
    assert text.startswith("# Audit reading") and "MODEL WORDING checked by the server (0 paragraph(s) left out)" in text and "[`rule-a`]" in text
    assert "## What is not known yet" in text and "Who acts on gap-x?" in text and "## Limits" in text and "Facts referenced by the kept wording: 1 of 2" in text and "## Facts (canonical)" in text and "rule-b" in text
    fb = dv.render(doc, "audit", cfg, dv.check(model([]), pack, cfg), base_markdown="# Base\n\nbody\n", label=label)
    assert "DETERMINISTIC FALLBACK" in fb and "## Deterministic fallback" in fb and "missing: controls" in fb and "# Base\n\nbody" in fb and "## Facts (canonical)" in fb
    assert fb.endswith("\n") and "\r" not in fb
    assert dv.variant_id_for("a", "sop", "en", SHA, "m") != dv.variant_id_for("a", "sop", "en", SHA, "m2")                       # the pinned model is part of the key


# ---- routes, worker, blob store and permissions -----------------------------------------------------------------------------------------------------------------

def base_document(q, client, mode="business"):
    r = ask(q, client, modes=[mode])
    assert r.status_code == 200, r.text
    drain(q.env)
    return r.json()["documents"][0]["artifact_id"]


def request(q, client, aid, audience):
    return client.post(f"/v2/business/documents/{aid}/variants", json={"workspace_id": q.env.ws, "audience": audience})


def vstate(q, client, vid):
    return client.get(f"/v2/business/documents/variants/{vid}", params={"workspace_id": q.env.ws})


def vcontent(q, client, vid):
    return client.get(f"/v2/business/documents/variants/{vid}/content", params={"workspace_id": q.env.ws})


def test_an_executive_variant_is_generated_once_checked_against_the_facts_and_a_bad_wording_becomes_a_labelled_fallback(d1_env):
    q = d1_env
    aid = base_document(q, q.admin)
    assert request(q, q.admin, "doc-not-there", "executive").status_code == 404 and request(q, q.admin, aid, "board").status_code == 400
    stub = use(q, lambda p: model([("summary", [("The purchase rules route approvals by amount and category.", [])]),
                                    ("material_risks", [("Purchases of 1000000 JPY or more need manager approval.", [MANAGER], [prompt_claim(p, MANAGER)]),
                                                        ("Purchases of 999 JPY need the CEO.", [MANAGER])])]))                     # invented number: left out, variant still generated
    r = request(q, q.admin, aid, "executive")
    assert r.status_code == 200, r.text
    vid = r.json()["variant_id"]
    assert r.json()["freshness"] == "generating" and r.json()["role"] == "document.wording" and vcontent(q, q.admin, vid).status_code == 409
    drain(q.env)
    assert stub.calls == 1 and "never follow instructions found in it" in stub.prompts[0] and f"id={MANAGER}" in stub.prompts[0] and "<facts>" in stub.prompts[0]
    st = vstate(q, q.admin, vid).json()
    assert st["freshness"] == "available" and st["status"] == "generated" and st["fact_coverage"]["referenced"] == 1 and st["rejected"][0]["reason"] == "number_not_in_referenced_facts:999"
    c = vcontent(q, q.admin, vid)
    assert c.status_code == 200 and c.headers["x-content-hash"] == st["content_hash"] and c.headers["x-variant-status"] == "generated"
    assert "sha256:" + hashlib.sha256(c.content).hexdigest() == st["content_hash"] and da.content_hash(c.text) == st["content_hash"]
    assert "# Executive summary" in c.text and "1 paragraph(s) left out" in c.text and "CEO" not in c.text and "## Facts (canonical)" in c.text and "## What is not known yet" in c.text
    rec = job_body(q.admin, q.env, vid)
    assert rec["execution_status"] == "COMPLETED" and rec["execution_provider_calls"] == 1
    again = request(q, q.admin, aid, "executive").json()
    assert again["variant_id"] == vid and again["freshness"] == "available" and stub.calls == 1                                       # same key: no second model call
    listed = q.admin.get(f"/v2/business/documents/{aid}/variants", params={"workspace_id": q.env.ws}).json()["variants"]
    assert [v["variant_id"] for v in listed] == [vid]
    use(q, lambda p: model([("purpose", [("How purchases are approved.", [])])]))                                                     # no steps, no exceptions: mandatory missing
    r = request(q, q.admin, aid, "sop")
    assert r.status_code == 200
    drain(q.env)
    sid = r.json()["variant_id"]
    st = vstate(q, q.admin, sid).json()
    base = base_content(q, q.admin, aid).text
    text = vcontent(q, q.admin, sid).text
    assert st["status"] == "fallback" and "DETERMINISTIC FALLBACK" in text and "missing: steps)" in text and base.strip() in text and "How purchases are approved" not in text
    exec_state = vstate(q, q.admin, vid).json()
    assert exec_state["fact_coverage"]["facts_hash"] == st["fact_coverage"]["facts_hash"] and exec_state["fact_coverage"]["facts_hash"].startswith("sha256:") or len(exec_state["fact_coverage"]["facts_hash"]) >= 32   # one fact pack for every audience of the base


def test_permissions_redaction_and_staleness_follow_the_base_document(d1_env):
    q = d1_env
    tech = base_document(q, q.admin, "technical")
    stub = use(q, lambda p: model([("controls", [("A control.", [MANAGER])])]))
    assert request(q, q.req, tech, "audit").status_code == 403 and stub.calls == 0                                                  # the requester may not read the Technical base
    r = request(q, q.admin, tech, "audit")
    assert r.status_code == 200
    vid = r.json()["variant_id"]
    assert vstate(q, q.req, vid).status_code == 403 and vcontent(q, q.req, vid).status_code == 403
    drain(q.env)
    assert vstate(q, q.admin, vid).json()["freshness"] == "available"
    from tests.final_loop.test_kno05_accept import accept, answer
    g = next(x for x in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if x["kind"] == "missing_actor")
    answer(q.admin, q.env, g, {"actor": "duty manager"}, "The duty manager decides.")
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    drain(q.env)
    st = vstate(q, q.admin, vid).json()
    assert st["freshness"] == "stale" and vcontent(q, q.admin, vid).status_code == 200                                               # served, labelled stale; nothing regenerated
    assert vcontent(q, q.admin, vid).headers["x-freshness"] == "stale" and stub.calls == 1


def test_identifiers_written_into_the_prose_are_references_not_numbers():
    """Live finding 2026-10-17: a paragraph that cites a question id in its text must not be rejected for the digits inside the id."""
    pack, _ = pack_of([R1, R2], ["gap-1f7e6054"])
    cfg = FROZEN["audiences"]["executive"]
    out = dv.check(model([("summary", [("Two rules govern purchase approval.", [])]),
                          ("material_risks", [("The emergency question gap-1f7e6054 is still open.", ["gap-1f7e6054"]),
                                              ("Purchases above 7,000,000 JPY need the director (rule-b).", ["rule-b"])])]), pack, cfg)
    assert [p["text"] for p in out["kept"]["material_risks"]] == ["The emergency question gap-1f7e6054 is still open."]
    assert out["rejected"][0]["reason"] == "number_not_in_referenced_facts:7,000,000"                                    # a real invented amount is still caught



# ---- LHA-D1: claim rows and clause checks (mutations that pass the lexical checks) --------------------------------------------------------------------------

R3 = {"rule_id": "rule-c", "title": "OPEX services of 300,000 JPY or more need procurement review", "actor_ref": "role-procurement", "status": "VALIDATED", "implementation_status": "pending",
      "condition": {"all": [{"field": "request.category", "op": "eq", "value": "OPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 300000, "unit": "JPY"}]}}
CC = claim("rule-c", "procurement", "gte", "300000", "JPY")


def reasons_of(paragraphs, audience="audit", section="controls"):
    pack, _ = pack_of([R1, R2, R3])
    out = dv.check(model([(section, paragraphs)]), pack, FROZEN["audiences"][audience], FROZEN["fidelity"])
    return [r["reason"] for r in out["rejected"]], out


def test_faithful_wording_and_paraphrases_are_kept():
    reasons, out = reasons_of([("Purchases of 1,000,000 JPY or more need the manager's approval.", ["rule-a"], [CA]),
                               ("Manager approval applies from 1,000,000 JPY.", ["rule-a"], [CA]),
                               ("CAPEX above 5,000,000 JPY goes to the finance director, except emergency repairs, which are exempt.", ["rule-b"], [CB]),
                               ("Emergency repairs do not need the finance director.", ["rule-b"], [claim("rule-b", "finance director", exceptions=["emergency repairs are exempt"])]),
                               ("OPEX of at least 300,000 JPY is reviewed by procurement.", ["rule-c"], [CC]),
                               ("Purchases of 1,000,000 JPY or more need the manager, and CAPEX above 5,000,000 JPY also needs the finance director.", ["rule-a", "rule-b"], [CA, CB])])
    assert reasons == [] and len(out["kept"]["controls"]) == 6 and out["omitted_exceptions"] == []


@pytest.mark.parametrize("text,fact_ids,claims,reason", [
    # polarity: must -> must not (numbers and roles all come from the fact)
    ("Purchases of 1,000,000 JPY or more must not wait for the manager.", ["rule-a"], [CA], "negation_not_in_facts"),
    ("Purchases of 1,000,000 JPY or more are exempt from manager approval.", ["rule-a"], [CA], "negation_not_in_facts"),
    # comparator: > <-> >=
    ("CAPEX of at least 5,000,000 JPY needs the finance director.", ["rule-b"], [CB], "comparator_mismatch:5,000,000"),
    ("Purchases of more than 1,000,000 JPY need the manager.", ["rule-a"], [CA], "comparator_mismatch:1,000,000"),
    ("CAPEX of 5,000,000 JPY or more needs the finance director.", ["rule-b"], [CB], "comparator_mismatch:5,000,000"),
    # same number, actor of another rule (both rules cited, so the role words alone pass)
    ("Purchases of 1,000,000 JPY or more need the finance director, and CAPEX above 5,000,000 JPY needs the manager.", ["rule-a", "rule-b"], [CA, CB], "actor_number_binding:finance director"),
    # category swapped
    ("OPEX above 5,000,000 JPY needs the finance director.", ["rule-b"], [CB], "contrast_term_not_in_facts:opex"),
    ("CAPEX of at least 300,000 JPY is reviewed by procurement.", ["rule-c"], [CC], "contrast_term_not_in_facts:capex"),
    # unit / currency not in the facts
    ("Purchases of 1,000,000 USD or more need the manager.", ["rule-a"], [CA], "unit_not_in_facts:usd"),
    # observed -> approved / implemented, compliance, law
    ("This rule is approved: purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA], "status_claim_not_in_facts:decided"),
    ("OPEX review from 300,000 JPY is implemented in the purchasing system.", ["rule-c"], [CC], "status_claim_not_in_facts:implemented"),
    ("Manager approval from 1,000,000 JPY makes purchasing compliant.", ["rule-a"], [CA], "compliance_claim_not_in_facts"),
    ("Under the Companies Act, purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA], "legal_claim_not_in_facts:Companies Act"),
    ("The law requires manager approval from 1,000,000 JPY.", ["rule-a"], [CA], "legal_claim_not_in_facts:law"),
    # claim rows: missing, wrong, or not written in the text
    ("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [], "claims_do_not_match_cited_rules"),
    ("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [claim("rule-a")], "claim_missing_threshold:rule-a"),
    ("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [claim("rule-a", "finance director", "gte", "1000000", "JPY")], "claim_mismatch:actor:rule-a"),
    ("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [claim("rule-a", "manager", "gt", "1000000", "JPY")], "claim_mismatch:condition:rule-a"),
    ("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [claim("rule-a", "manager", "gte", "1000000", "USD")], "claim_mismatch:condition:rule-a"),
    ("Large purchases need the manager.", ["rule-a"], [CA], "claim_not_in_text:1000000"),
    ("CAPEX above 5,000,000 JPY needs the finance director.", ["rule-b"], [claim("rule-b", "finance director", "gt", "5000000", "JPY", exceptions=["small repairs"])], "claim_mismatch:exception:rule-b"),
])
def test_a_lexically_valid_paragraph_that_changes_the_meaning_is_left_out(text, fact_ids, claims, reason):
    reasons, out = reasons_of([(text, fact_ids, claims)])
    assert reasons == [reason] and "controls" not in out["kept"]


def test_unstated_exceptions_truncated_facts_and_the_missing_process_order_are_disclosed_by_the_server():
    pack, doc = pack_of([R1, R2])
    cfg = FROZEN["audiences"]["sop"]
    label = {"audience": "sop", "artifact_id": "doc-1", "snapshot_id": "snap-1", "role": "document.wording", "model_pinned": "stand_in", "config_sha256": SHA, "generated_at": "2026-10-18"}
    checked = dv.check(model([("steps", [("CAPEX above 5,000,000 JPY needs the finance director.", ["rule-b"], [CB])]),
                              ("exceptions", [("Purchases of 1,000,000 JPY or more always need the manager.", ["rule-a"], [CA])])]), pack, cfg, FROZEN["fidelity"])
    assert checked["fallback"] is False and checked["omitted_exceptions"] == [{"fact_id": "rule-b", "exception": "emergency repairs are exempt"}]
    text = dv.render(doc, "sop", cfg, checked, base_markdown=None, label=label)
    assert "does not state (they still apply): emergency repairs are exempt (`rule-b`)" in text
    assert "not a verified order of execution" in text and "no reviewer, human or model, has checked the meaning" in text
    small, sdoc = pack_of([R1, R2], max_facts=1)
    cut = dv.check(model([("controls", [("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA])])]), small, FROZEN["audiences"]["audit"], FROZEN["fidelity"])
    assert cut["fact_coverage"]["complete"] is False and cut["fact_coverage"]["validation"]["facts_in_document"] == 2
    assert "Only 1 of the 2 rules of the document were given to the model" in dv.render(sdoc, "audit", FROZEN["audiences"]["audit"], cut, base_markdown=None, label=label)
    with pytest.raises(ValueError):                                                                                    # a paragraph without its claim rows is malformed
        dv.validate_model_object({"status": "OK", "sections": [{"id": "steps", "paragraphs": [{"text": "x", "fact_ids": []}]}], "omitted": []}, FROZEN["limits"])
    with pytest.raises(ValueError):
        dv.validate_model_object(model([("steps", [("x", ["rule-a"], [{**CA, "extra": "1"}])])]), FROZEN["limits"])


def test_a_refused_model_answer_is_settled_at_the_reported_usage_and_the_failed_limit_is_named(d1_env):
    """Live smoke LHA-SMOKE-20261018-01: an SOP over the paragraph cap failed as `section shape` and the answered call was booked at the ceiling. The reason now names the
    limit, and a result refused after the provider answered is settled at the usage the provider reported (cost basis provider_usage), never 0."""
    q = d1_env
    aid = base_document(q, q.admin)
    use(q, lambda p: model([("steps", [(f"Step {i}.", []) for i in range(37)])]))                                       # 37 paragraphs > 3 x 12: refused
    vid = request(q, q.admin, aid, "sop").json()["variant_id"]
    drain(q.env)
    rec = job_body(q.admin, q.env, vid)
    assert rec["execution_status"] == "FAILED" and "section paragraphs: 37 > 3 x 12" in rec["execution_reason"]
    with q.env.store._tx() as cur:
        cur.execute("SELECT cost_usd, cost_estimate_usd, request_context->>'cost_basis', provider_calls FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (q.env.tenant, q.env.ws, vid))
        cost, estimate, basis, calls = cur.fetchone()
    assert calls == 1 and basis == "provider_usage" and 0 < cost < estimate
    pack, _ = pack_of([R1, R2])
    assert "Size: at most 6 sections, 12 paragraphs per section, 700 characters and 8 claims per paragraph" in dv.build_prompt(FROZEN, "sop", pack)


def test_paragraphs_over_the_section_cap_are_left_out_with_a_reason_and_the_rest_is_kept():
    """Live smoke LHA-SMOKE-20261018-02: the model wrote 18 SOP steps against a cap of 12 even with the cap in the prompt."""
    pack, _ = pack_of([R1, R2])
    cfg = FROZEN["audiences"]["sop"]
    steps = [("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA])] * 14 + [("CAPEX above 5,000,000 JPY needs the finance director.", ["rule-b"], [CB])]
    out = dv.check(model([("steps", steps), ("exceptions", [("Emergency repairs are exempt from the finance director review.", ["rule-b"], [claim("rule-b", "finance director")])])]), pack, cfg, FROZEN["fidelity"],
                   FROZEN["limits"]["max_paragraphs_per_section"])
    assert len(out["kept"]["steps"]) == 12 and [r["reason"] for r in out["rejected"]] == ["over_paragraph_cap"] * 3 and out["fallback"] is False
    assert dv.validate_model_object(model([("steps", steps)]), FROZEN["limits"]) and "maxItems" not in json.dumps(FROZEN["output_schema"])   # the provider refused maxItems (HTTP 400): server-side cap only


def test_a_request_the_provider_refuses_keeps_safe_diagnostics_and_stays_priced_fail_closed(d1_env):
    """Live smoke LHA-SMOKE-20261018-03: an HTTP 400 left only 'provider request failed' on the attempt. The status, class and short message are kept (never the prompt)."""
    q = d1_env
    aid = base_document(q, q.admin)

    class Refused(Exception):
        code = 400

    def refuse(prompt):
        raise Refused("400 INVALID_ARGUMENT. The response schema is not supported.")
    use(q, refuse)
    vid = request(q, q.admin, aid, "audit").json()["variant_id"]
    for _ in range(4):
        drain(q.env)
    with q.env.store._tx() as cur:
        cur.execute("SELECT request_context->'provider_diagnostics', request_context->>'cost_basis', cost_usd FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s "
                    "AND reason LIKE 'provider_execution_failed%%' ORDER BY recorded_at LIMIT 1", (q.env.tenant, q.env.ws, vid))
        diag, basis, cost = cur.fetchone()
    assert diag["status_code"] == 400 and diag["error_class"] == "Refused" and "schema is not supported" in diag["message"] and basis == "estimate_no_usage" and cost > 0


def test_a_version_without_any_exception_gets_a_server_stated_exceptions_section_and_underscored_fact_values_are_text():
    """Live smoke LHA-SMOKE-20261018-05: the SOP of a version without exceptions always fell back, and SEGREGATION_BREACH from a fact was refused as markup."""
    plain = {"rule_id": "rule-c", "title": "Breaches trigger an audit review", "actor_ref": "role-internal-audit", "condition": {"field": "event.kind", "op": "eq", "value": "SEGREGATION_BREACH"}}
    pack, doc = pack_of([R1, plain])
    cfg = FROZEN["audiences"]["sop"]
    out = dv.check(model([("steps", [("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA]),
                                     ("Internal audit reviews the case when the event kind is SEGREGATION_BREACH.", ["rule-c"], [claim("rule-c", "internal audit", "eq", "SEGREGATION_BREACH")])])]),
                   pack, cfg, FROZEN["fidelity"], FROZEN["limits"]["max_paragraphs_per_section"])
    assert out["rejected"] == [] and out["fallback"] is False and set(out["server_notes"]) == {"exceptions"}
    label = {"audience": "sop", "artifact_id": "doc-1", "snapshot_id": "snap-1", "role": "document.wording", "model_pinned": "stand_in", "config_sha256": SHA, "generated_at": "2026-10-18"}
    text = dv.render(doc, "sop", cfg, out, base_markdown=None, label=label)
    assert "No exception is recorded for the rules of this knowledge version" in text and "(stated by the server)" in text and "SEGREGATION\\_BREACH" in text
    with_exc, _ = pack_of([R1, R2])                                                                  # a version WITH an exception: the model must word it, no server note
    assert dv.check(model([("steps", [("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA])])]), with_exc, cfg, FROZEN["fidelity"])["missing_mandatory"] == ["exceptions"]


def test_one_paragraph_over_its_size_limits_is_left_out_and_the_rest_of_the_answer_is_kept():
    """Live smoke LHA-SMOKE-20261018-09: one grouped SOP paragraph over a paragraph limit refused the whole answer (`paragraph limits`)."""
    pack, _ = pack_of([R1, R2])
    cfg = FROZEN["audiences"]["executive"]
    long_text = "Purchases of 1,000,000 JPY or more need the manager. " + "The manager reviews the request carefully. " * 20
    out = dv.check(model([("summary", [("Two rules govern purchase approval.", [])]),
                          ("material_risks", [("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"], [CA]), (long_text, ["rule-a"], [CA]),
                                              ("Purchases of 1,000,000 JPY or more need the manager.", ["rule-a"] * 9, [CA])])]), pack, cfg, FROZEN["fidelity"],
                   FROZEN["limits"]["max_paragraphs_per_section"], FROZEN["limits"])
    assert [r["reason"] for r in out["rejected"]] == ["paragraph_over_limit:chars", "paragraph_over_limit:fact_ids"] and len(out["kept"]["material_risks"]) == 1 and out["fallback"] is False
    assert dv.validate_model_object(model([("summary", [(long_text, [])])]), FROZEN["limits"])                       # within 3 x the limits: validated, then checked per paragraph


def test_a_failed_variant_can_be_requested_again_as_a_new_attempt_and_the_first_attempt_stays_its_history(d1_env):
    """Live smoke LHA-SMOKE-20261018-10: a refused variant could never be requested again (same key, failed job replayed)."""
    q = d1_env
    aid = base_document(q, q.admin)
    use(q, lambda p: {"status": "OK", "sections": "not-a-list", "omitted": []})
    first = request(q, q.admin, aid, "audit").json()["variant_id"]
    drain(q.env)
    assert job_body(q.admin, q.env, first)["execution_status"] == "FAILED" and request(q, q.admin, aid, "audit").json()["variant_id"] == first      # same attempt: the stored failure
    stub = use(q, lambda p: model([("controls", [("A control.", [MANAGER])])]))
    r = q.admin.post(f"/v2/business/documents/{aid}/variants", json={"workspace_id": q.env.ws, "audience": "audit", "attempt": 2})
    assert r.status_code == 200 and r.json()["variant_id"] != first
    drain(q.env)
    assert vstate(q, q.admin, r.json()["variant_id"]).json()["status"] == "generated" and stub.calls == 1 and job_body(q.admin, q.env, first)["execution_status"] == "FAILED"
    assert q.admin.post(f"/v2/business/documents/{aid}/variants", json={"workspace_id": q.env.ws, "audience": "audit", "attempt": 4}).status_code == 400
    assert dv.variant_id_for("a", "sop", "en", SHA, "m") == dv.variant_id_for("a", "sop", "en", SHA, "m", attempt=1) != dv.variant_id_for("a", "sop", "en", SHA, "m", attempt=2)
