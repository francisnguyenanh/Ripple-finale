"""Harbor S1 chain helpers: real import, real extraction jobs (stub provider OR the D0 executor), intents, briefs.

STUB PAYLOADS ARE AUTHORED FROM THE HARBOR DOCUMENTS (model-shaped candidates). No model is called: the provider
client is replaced by a counting stub. The quotes are verbatim substrings of the stored observations (the real
extraction resolver drops anything else, which a test below relies on).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tests.pitch_brief.conftest import EXTRACT_MODEL, GAP_MODEL, create_job, drain, import_harbor, job_body

P = "harbor_demo/"
APPROVAL, WORKFLOW = P + "config/approval.yaml", P + "config/workflow.yaml"
POLICY, FINGUIDE, MATRIX = P + "docs/procurement_policy.md", P + "docs/finance_desk_guide.md", P + "data/approval_matrix.xlsx"
EXTRACT_PATHS = (APPROVAL, WORKFLOW, POLICY, FINGUIDE, MATRIX)

S1 = {"subject": "Manager approval limit", "scope": {"order_type": "standard"}, "unit": "net goods amount",
      "currency": "JPY", "old_value": "1,000,000", "new_value": "500,000", "applies_to": ["standard", "emergency"]}

_MGR_QUOTE = ("Standard requisitions with a net goods value of **1,000,000 JPY or greater** require documented "
              "sign-off from an authorized **Manager**.")


def _steps_quote(store, tenant, ws, rev, art):
    for o in store.source_observations(tenant, ws, rev):
        if o["artifact_id"] == art and (o["locator"] or {}).get("key_path") == "steps":
            return "steps: " + json.dumps(o["raw_value"], sort_keys=True, default=str)
    raise AssertionError("steps leaf missing")


def stub_payloads(steps_quote, finguide_scope=None):
    def c(subject, predicate, value, unit, scope, quote, modality="must"):
        return {"subject": subject, "predicate": predicate, "value": value, "unit": unit, "modality": modality,
                "scope": scope, "exceptions": [], "outcome": None, "quotes": [quote],
                "rationale": "stub payload authored from the document"}
    return [
        ("routing_rules.standard_manager_threshold_jpy", [
            c("Manager approval", "standard_manager_approval_threshold", 1000000, "JPY", {"order_type": "standard"},
              "routing_rules.standard_manager_threshold_jpy: 1000000"),
            c("Finance review", "capex_finance_threshold", 2000000, "JPY", {"category": "CAPEX"},
              "routing_rules.capex_finance_threshold_jpy: 2000000")]),
        ("Harbor Procurement Workflow", [
            c("Manager approval step", "step_manager_approval_threshold", 1000000, "JPY",
              {"workflow": "Harbor Procurement Workflow"}, steps_quote)]),
        ("Corporate Procurement Policy", [
            c("Manager approval", "manager_approval_required_from", 1000000, "JPY", {"order_type": "standard"}, _MGR_QUOTE),
            c("Finance review", "capex_finance_review_threshold", 2000000, "JPY", {"category": "CAPEX"},
              "Capital equipment and long-term asset acquisitions of **2,000,000 JPY or greater** require formal "
              "review and authorization by the **Finance Department**."),
            c("Free freight", "free_freight_threshold", 500000, "JPY", {"purchase": "goods net amount"},
              "Purchases with a net goods amount of **500,000 JPY or greater** receive free freight delivery "
              "(0 JPY freight charge).")]),
        ("Finance Desk Guide", [
            c("Finance review", "capex_finance_review_threshold", 2000000, "JPY", finguide_scope or {"scope": "all purchasing commitments"},
              "all purchasing commitments of **2,000,000 JPY or higher** must be reviewed and authorized by the "
              "Finance Department prior to purchase order commitment.")]),
        ("RoutingRules!B3: 500000", [
            c("Manager approval", "approval_threshold", 500000, "JPY", {"role": "MANAGER"}, "RoutingRules!B3: 500000")]),
    ]


class StubClient:
    """Counting stand-in for the provider client: picks the payload by a marker in the prompt text."""
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        prompt = kw.get("contents") or ""
        cands = []
        for marker, items in self.payloads:
            if marker in prompt:
                cands = items
                break
        return SimpleNamespace(text=json.dumps({"candidates": cands}), model_version=EXTRACT_MODEL,
                               candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=30,
                                                              thoughts_token_count=None, total_token_count=70))


def build(env, monkeypatch, *, arm="stub", paths=EXTRACT_PATHS, finguide_scope=None):
    """import -> extraction job per artifact (stub provider) -> {arts, rev, jobs, cands, client}."""
    from infra.worker import extraction
    admin = env.admin_client()
    arts, rev = import_harbor(env, admin)
    drain(env)
    out = SimpleNamespace(arts=arts, rev=rev, admin=admin, jobs={}, stub=None, d0=None)
    if arm == "stub":
        out.stub = StubClient(stub_payloads(_steps_quote(env.store, env.tenant, env.ws, rev, arts[WORKFLOW]), finguide_scope))
        monkeypatch.setattr(extraction, "_client_for_model", lambda model: out.stub)
    requester = env.req_client()
    for path in paths:
        jid, r = create_job(requester, env, "extraction", {"artifact_id": arts[path], "source_role": "unknown"})
        assert r.status_code == 200, r.text
        # external (provider) jobs are PENDING_APPROVAL: approve through the real route (distinct approver) before the worker claims it
        ra = out.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws})
        assert ra.status_code == 200, ra.text
        out.jobs[path] = jid
        drain(env)       # concurrency limit 2 (candidate config): one extraction in flight at a time
    drain(env)
    out.bodies = {p: job_body(admin, env, j) for p, j in out.jobs.items()}
    for p, b in out.bodies.items():
        assert b["execution_status"] == "COMPLETED", (p, b)
    out.cands = {p: b["execution_result"]["candidates"] for p, b in out.bodies.items()}
    return out


def intent(env, admin, form, *, confirm=True):
    r = admin.post("/v2/intent/parse", json={"workspace_id": env.ws, "structured_form": form})
    assert r.status_code == 200, r.text
    parsed = r.json()
    if confirm and parsed["status"] == "PARSED":
        c = admin.post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": parsed["intent_id"]})
        assert c.status_code == 200, c.text
    return parsed


def run_brief(env, ch, intent_id, **extra):
    payload = {"intent_id": intent_id, "extraction_job_ids": list(ch.jobs.values()), **extra}
    jid, r = create_job(ch.admin, env, "change.brief", payload)
    assert r.status_code == 200, r.text
    drain(env)
    body = job_body(ch.admin, env, jid)
    return jid, body


def cand_id(ch, path, predicate):
    return next(c["candidate_id"] for c in ch.cands[path] if c["predicate"] == predicate)
