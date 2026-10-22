"""Source -> knowledge through the REAL callers (journey finding JOURNEY-V1-J1, 2026-10-21): import with the kinds of source confirmed, one extraction job per
file through the route and the worker, then "Read the business rules from the sources" (POST /v2/business/analyse).

Provider: MOCK extraction (author-written model-shaped payloads keyed by the file, no model call). It proves the mapping and the links, not what a model
extracts. Cases: a code route compared with a configuration attribute gets its CAPEX condition from the verbatim code line and its value from the
configuration of the same revision (traced link + binding), its actor only from the code's own role check; configuration constants become supporting facts,
not rules or questions; a renamed key asks a question instead of guessing; a refresh with a new threshold re-reads the linked value."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from uuid import uuid4

from infra.adapters.v2_business import business_view
from infra.worker import extraction
from tests.pitch_brief.conftest import EXTRACT_MODEL, create_job, drain

YAML = "config/approval.yaml"
CODE = "app/routing.py"
ROLES = "app/approval.py"
POLICY = "docs/policy.md"
YAML_TEXT = "routing_rules:\n  standard_manager_threshold_jpy: 1000000\n  capex_finance_threshold_jpy: {capex}\nmatching_rules:\n  payment_term_days: 30\n"
CODE_TEXT = '''def route_request(request, config):
    """Routing."""
    if request.category == CategoryType.CAPEX and request.net_amount_jpy >= config.capex_finance_threshold_jpy:
        return RouteDecision(request.request_id, RouteType.NEEDS_FINANCE, "CAPEX_HIGH_VALUE")
    if request.currency != "JPY":
        return RouteDecision(request.request_id, RouteType.REJECTED, "UNSUPPORTED_CURRENCY")
    return RouteDecision(request.request_id, RouteType.AUTO, "AUTO_APPROVED")
'''
ROLES_TEXT = '''def required_role(route):
    """Who approves a route."""
    if route.route == RouteType.NEEDS_FINANCE:
        return Role.FINANCE
    return None
'''
POLICY_TEXT = "# Policy\n\nCapital expenditure of JPY 5,000,000 or more is referred to the Finance Controller.\n\nExceptions are recorded in the register.\n"
CAPEX_LINE = "if request.category == CategoryType.CAPEX and request.net_amount_jpy >= config.capex_finance_threshold_jpy:"


def cand(subject, predicate, quote, **over):
    base = {"subject": subject, "predicate": predicate, "value": None, "unit": None, "operator": None, "modality": "observed", "scope": {"process": "standard"},
            "exceptions": [], "outcome": None, "actor": None, "condition": None, "quotes": [quote], "rationale": "mock payload written from the file"}
    base.update(over)
    return base


def payload_for(prompt: str, capex_key: str):
    if "routing_rules." in prompt:                                                       # the YAML: every key as a constant (what the model returned live)
        out = []
        for line in prompt.split("<source>", 1)[1].splitlines():
            if line.startswith("[chunk") and ": " in line:
                loc, val = line.split("] ", 1)[1].split(": ", 1)
                out.append(cand(loc, "is", f"{loc}: {val}", value=int(val) if val.isdigit() else val, operator="=="))
        return out
    if "def route_request" in prompt:
        return [cand("request.net_amount_jpy", "routes CAPEX", CAPEX_LINE, operator=">=", value=f"config.{capex_key}", unit="JPY", outcome="RouteType.NEEDS_FINANCE",
                     scope={"category": "CategoryType.CAPEX"},
                     condition={"all": [{"field": "request.category", "op": "eq", "value": "CategoryType.CAPEX"},
                                        {"field": "request.net_amount_jpy", "op": "gte", "value": f"config.{capex_key}"}]}),
                cand("request.currency", "must equal", 'if request.currency != "JPY":', operator="!=", value="JPY")]           # no actor, no outcome: incomplete
    if "def required_role" in prompt:
        return [cand("route", "requires role", "if route.route == RouteType.NEEDS_FINANCE:", actor="Role.FINANCE", outcome="approval by the required role",
                     condition={"field": "route.route", "op": "eq", "value": "RouteType.NEEDS_FINANCE"})]
    if "Capital expenditure" in prompt:
        return [cand("capital expenditure", "is referred to", "Capital expenditure of JPY 5,000,000 or more is referred to the Finance Controller.", modality="must",
                     operator=">=", value=5000000, unit="JPY", actor="Finance Controller", outcome="referred to the Finance Controller", scope={"category": "CAPEX"},
                     condition={"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 5000000, "unit": "JPY"}]}),
                cand("exceptions", "are recorded", "Exceptions are recorded in the register.", modality="must")]            # no actor, no outcome: an incomplete statement
    return []


class Stub:
    def __init__(self, capex_key):
        self.capex_key, self.calls = capex_key, 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        body = json.dumps({"candidates": payload_for(kw.get("contents") or "", self.capex_key)})
        return SimpleNamespace(text=body, model_version=EXTRACT_MODEL, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=30, thoughts_token_count=None, total_token_count=70))


def drain_all(env, rounds=20):
    """The worker loop: a refresh schedules re-extraction of changed files; a concurrency refusal is transient (the event is redelivered)."""
    from infra.adapters.v2_postgres import ConcurrencyLimitExceeded
    done = 0
    for _ in range(rounds):
        try:
            n = drain(env)
        except ConcurrencyLimitExceeded:
            n = 1
        done += n
        if n == 0:
            break
    return done


def files(capex=2000000, yaml_text=None):
    return {YAML: (yaml_text or YAML_TEXT).format(capex=capex), CODE: CODE_TEXT, ROLES: ROLES_TEXT, POLICY: POLICY_TEXT}


KINDS = {YAML: "implementation", CODE: "implementation", ROLES: "implementation", POLICY: "normative"}


def extract_and_read(env, monkeypatch, *, capex_key="capex_finance_threshold_jpy", yaml_text=None, analyse=True):
    admin, req = env.admin_client(), env.req_client()
    body = {"workspace_id": env.ws, "idempotency_key": uuid4().hex,
            "files": [{"path": p, "content_b64": base64.b64encode(t.encode()).decode()} for p, t in files(yaml_text=yaml_text).items()]}
    dto = admin.post("/v2/sources/imports", json=body).json()
    r = admin.post(f"/v2/sources/imports/{dto['import_id']}/commit", json={"workspace_id": env.ws, "expected_version": 0, "acknowledged_skips": [],
                                                                             "source_roles": {i["logical_path"]: KINDS[next(k for k in KINDS if i["logical_path"].endswith(k))]
                                                                                              for i in dto["items"] if i["disposition"] == "admitted"}})
    assert r.status_code == 200, r.text
    drain(env)
    stub = Stub(capex_key)
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    head = env.store.head(env.tenant, env.ws)
    for a in env.store.source_revision_view(env.tenant, env.ws, head.source_revision_id)["artifacts"]:
        assert a["source_role"] == KINDS[next(k for k in KINDS if a["logical_path"].endswith(k))]           # the kind the person confirmed reaches the job
        jid, r = create_job(req, env, "extraction", {"artifact_id": a["artifact_id"], "source_role": a["source_role"]})
        assert r.status_code == 200, r.text
        assert admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
        drain(env)                                                                                          # one file at a time (workspace concurrency)
    if not analyse:                                                                                         # the person reads the rules in the UI
        return None, stub
    r = admin.post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    view = business_view(env.store, env.tenant, env.ws)
    view["pending"]["analyse_response"] = r.json()
    return view["pending"], stub


def test_a_code_route_gets_its_capex_condition_and_configured_value_with_traced_links_and_its_actor_only_from_the_codes_role_check(env, monkeypatch):
    snap, stub = extract_and_read(env, monkeypatch)
    assert stub.calls == 4
    rules = {r["candidate_ids"][0]: r for r in snap["rules"]}
    capex = next(r for r in snap["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"])
    assert capex["condition"] == {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"},
                                          {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]}
    refs = {b["evidence_ref"] for b in capex["source_bindings"]}
    assert any(r.endswith(YAML + "#routing_rules.capex_finance_threshold_jpy") for r in refs)
    methods = {p["method"] for p in capex["provenance"] if p["kind"] == "source_link"}
    assert methods == {"attribute_name", "enum_member", "outcome_actor"} and capex["actor_ref"] == "role-finance"
    assert capex["authority"] == "OBSERVED" and capex["status"] != "CONFIRMED"                                 # a link never confirms anything
    cov = snap["snapshot"]["coverage"]["sources"]
    assert cov["supporting_facts_count"] == 3 and cov["incomplete_count"] == 1 and cov["unresolved_references"] == []      # currency check: REJECTED from its branch; the exceptions sentence: incomplete
    assert not any(r["title"].startswith(("routing_rules.", "matching_rules.")) for r in snap["rules"])
    assert not any(g["kind"] == "missing_actor" and capex["rule_id"] in g["affected_rule_ids"] for g in snap["gaps"])
    assert len(rules) == 4                                                                                      # route, currency check, role check, policy rule
    s = snap["analyse_response"]["sources"]                                                                     # what the reading panel shows
    assert s["supporting_facts"] == 3 and s["incomplete"] == 1 and s["source_links"] >= 3 and s["unresolved_references"] == []
    assert s["incomplete_items"][0]["missing"] == ["actor", "outcome"] and s["incomplete_items"][0]["file"].endswith(POLICY)
    assert s["code_branches"] == 3 and s["uncovered_branches"] == 0


def test_a_renamed_configuration_key_asks_which_value_the_rule_uses_instead_of_guessing(env, monkeypatch):
    yaml_text = YAML_TEXT.replace("capex_finance_threshold_jpy", "capex_threshold_jpy")
    snap, _ = extract_and_read(env, monkeypatch, yaml_text=yaml_text)
    capex = next(r for r in snap["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"])
    assert capex["condition"] is None
    link_gaps = [g for g in snap["gaps"] if g["kind"] == "uncertain_link"]
    assert len(link_gaps) == 1 and capex["rule_id"] in link_gaps[0]["affected_rule_ids"]
    assert "config.capex_finance_threshold_jpy" in link_gaps[0]["question"] and "no configuration value with that name" in link_gaps[0]["question"]
    assert snap["snapshot"]["coverage"]["sources"]["unresolved_references"][0]["reason"] == "not_found"


def test_a_refresh_with_a_new_threshold_re_reads_the_linked_configuration_value(env, monkeypatch):
    snap, _ = extract_and_read(env, monkeypatch)
    admin = env.admin_client()
    new = files(capex=2500000)
    prep = admin.post("/v2/sources/refresh/prepare", json={"workspace_id": env.ws, "idempotency_key": uuid4().hex,
                                                          "files": [{"path": p, "content_b64": base64.b64encode(t.encode()).decode()} for p, t in new.items()]}).json()
    r = admin.post("/v2/sources/refresh/commit", json={"workspace_id": env.ws, "refresh_id": prep["refresh_id"], "expected_version": prep["base"]["version"],
                                                       "expected_refresh_digest": prep["refresh_digest"], "acknowledged_deletions": []})
    assert r.status_code == 200, r.text
    drain_all(env)
    head = env.store.head(env.tenant, env.ws)
    kinds = {a["logical_path"]: a["source_role"] for a in env.store.source_revision_view(env.tenant, env.ws, head.source_revision_id)["artifacts"]}
    assert all(kinds[p] == KINDS[p] for p in kinds)                                                            # confirmed kinds survive the refresh
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    pending = business_view(env.store, env.tenant, env.ws)["pending"]
    capex = next(r for r in pending["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"])
    assert capex["condition"]["all"][1]["value"] == 2500000 and pending["snapshot"]["source_revision_id"] == head.source_revision_id


def test_a_refresh_that_re_extracts_only_the_changed_file_keeps_the_rules_of_the_unchanged_files(env, monkeypatch):
    """With a worker budget config (as in a live run) a refresh schedules re-extraction of the changed file; its candidates are the only ones of the new
    revision. Reading the rules again must not drop the rules of the files that did not change."""
    from tests.pitch_brief.conftest import REAL_BUDGET_PATH
    snap, stub = extract_and_read(env, monkeypatch)
    before = {r["title"] for r in snap["rules"]}
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(REAL_BUDGET_PATH))
    admin = env.admin_client()
    new = files(capex=2500000)
    prep = admin.post("/v2/sources/refresh/prepare", json={"workspace_id": env.ws, "idempotency_key": uuid4().hex,
                                                          "files": [{"path": p, "content_b64": base64.b64encode(t.encode()).decode()} for p, t in new.items()]}).json()
    r = admin.post("/v2/sources/refresh/commit", json={"workspace_id": env.ws, "refresh_id": prep["refresh_id"], "expected_version": prep["base"]["version"],
                                                       "expected_refresh_digest": prep["refresh_digest"], "acknowledged_deletions": []})
    assert r.status_code == 200, r.text
    for _ in range(6):                                                                                         # later worker passes: a refused event is redelivered
        drain_all(env)
        with env.store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (env.tenant, env.ws))
    with env.store._tx() as cur:
        cur.execute("SELECT job_id, execution_status FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND job_id LIKE 'reanalysis-%%'", (env.tenant, env.ws))
        reextracted = cur.fetchall()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    pending = business_view(env.store, env.tenant, env.ws)["pending"]
    after = {r["title"] for r in pending["rules"]}
    assert before <= after, (reextracted, before - after)
    capex = next(r for r in pending["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"])
    assert capex["condition"]["all"][1]["value"] == 2500000                                                    # re-linked to the CURRENT configuration value
    cov = pending["snapshot"]["coverage"]
    assert reextracted and cov["observed_from"] == "extraction_candidates" and len(cov["sources"]["carried_files"]) == 3   # the unchanged files are carried
    assert all(b["revision_id"] == pending["snapshot"]["source_revision_id"] for r in pending["rules"] for b in r["source_bindings"])


def test_code_candidates_without_outcome_or_actor_from_the_model_still_become_complete_traced_rules_and_the_models_output_is_kept(env, monkeypatch):
    """The shape of live run JOURNEY-V2-J1: every code candidate with outcome and actor null. The branch facts of the code (AST, never executed) give the outcome
    and, through approval.py's role check, the actor; the model's own output stays with the job result; a non-string actor is recorded as dropped, not lost."""
    import tests.final_loop.test_kno02_source_links as me
    original = me.payload_for

    def nulls(prompt, capex_key):
        out = original(prompt, capex_key)
        if "def route_request" in prompt or "def required_role" in prompt:
            for c in out:
                c["outcome"], c["actor"] = None, None
            if "def required_role" in prompt:
                out[0]["actor"] = {"role": "FINANCE"}                                                       # a shape slip: must be recorded, never silently dropped
        return out
    monkeypatch.setattr(me, "payload_for", nulls)
    snap, stub = extract_and_read(env, monkeypatch)
    capex = next(r for r in snap["rules"] if "request.category" in json.dumps(r["condition"]) and r["candidate_ids"][0] != "" and "capital" not in r["title"].lower())
    assert capex["outcome"]["kind"] == "RouteType.NEEDS_FINANCE" and capex["actor_ref"] == "role-finance"
    methods = sorted(p["method"] for p in capex["provenance"] if p["kind"] == "source_link")
    assert methods == ["attribute_name", "branch_outcome", "enum_member", "outcome_actor"]
    to_ref = next(p for p in capex["provenance"] if p.get("method") == "outcome_actor")["to_ref"]
    assert to_ref.startswith(ROLES + "#required_role:")                                                       # the line of approval.py that assigns the role
    s = snap["analyse_response"]["sources"]
    assert s["incomplete"] == 1 and s["uncovered_branches"] == 0 and s["code_branches"] >= 3
    with env.store._tx() as cur:
        cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'", (env.tenant, env.ws))
        results = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in cur.fetchall()]
    assert all("model_output" in r and r["model_output_truncated"] is False for r in results)
    assert any(r["fields_dropped"] == [{"index": 0, "field": "actor", "type": "dict"}] for r in results)
    assert any('"outcome": null' in r["model_output"] for r in results)                                      # what the model said, verbatim


def test_a_file_extracted_twice_in_one_revision_is_read_from_its_newest_completed_job(env, monkeypatch):
    """Found by the journey-v3 harness (2026-10-22): a person-approved extraction job has no JOB_QUEUED outbox row, so the earlier ordering fell back to the job id
    and could pick an OLDER extraction of the same file. The newest completed attempt wins."""
    import tests.final_loop.test_kno02_source_links as me
    snap, stub = extract_and_read(env, monkeypatch)
    first = next(r for r in snap["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"] and r["candidate_ids"])
    original = me.payload_for

    def renamed(prompt, capex_key):                                                                            # a second extraction of routing.py with another subject
        out = original(prompt, capex_key)
        if "def route_request" in prompt:
            out[0]["subject"], out[0]["predicate"] = "capex request", "goes to finance"
        return out
    monkeypatch.setattr(me, "payload_for", renamed)
    admin, req = env.admin_client(), env.req_client()
    head = env.store.head(env.tenant, env.ws)
    art = next(a for a in env.store.source_revision_view(env.tenant, env.ws, head.source_revision_id)["artifacts"] if a["logical_path"].endswith(CODE))
    for _ in range(3):                                                                                         # three more jobs for the same file; the last one must win
        jid, r = create_job(req, env, "extraction", {"artifact_id": art["artifact_id"], "source_role": art["source_role"]})
        assert r.status_code == 200 and admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
        drain(env)
    r = admin.post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 200 and r.json()["derived"] and r.json()["replayed"] is False                       # the sources did not move, the extraction did: read again
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).json()["replayed"] is True       # and reading once more is idempotent
    pending = business_view(env.store, env.tenant, env.ws)["pending"]
    capex = [r for r in pending["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"] and "request.category" in json.dumps(r["condition"])]
    live = [r for r in capex if r["status"] != "SUPERSEDED"]
    assert len(live) == 1 and live[0]["title"] == "capex request - goes to finance" and live[0]["rule_id"] != first["rule_id"]
    assert [r["rule_id"] for r in capex if r["status"] == "SUPERSEDED"] == [first["rule_id"]]                   # the earlier reading is withdrawn, kept for history


def test_a_candidate_without_scope_keeps_the_outcome_actor_and_condition_the_model_stated(env, monkeypatch):
    """Found on live run JOURNEY-V3-J1 (2026-10-22): the model wrote the branch's return expression as the outcome but `scope: null`, and the missing-scope path
    built an UNKNOWN candidate with actor, outcome and condition discarded. A missing scope is a visible limitation, never a reason to drop the rest."""
    import tests.final_loop.test_kno02_source_links as me
    original = me.payload_for

    def no_scope(prompt, capex_key):
        out = original(prompt, capex_key)
        if "def route_request" in prompt:
            out[0]["scope"] = None
            out[0]["outcome"] = 'RouteDecision(request.request_id, RouteType.NEEDS_FINANCE, "CAPEX_HIGH_VALUE", request.net_amount_jpy, config.version)'
        return out
    monkeypatch.setattr(me, "payload_for", no_scope)
    snap, _ = extract_and_read(env, monkeypatch)
    with env.store._tx() as cur:
        cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'", (env.tenant, env.ws))
        cands = [c for r in cur.fetchall() for c in (r[0] if isinstance(r[0], dict) else json.loads(r[0]))["candidates"]]
    c = next(c for c in cands if c["applicability"] == "UNKNOWN" and c["applicability_reason"] == "missing_scope")
    assert c["outcome"].startswith("RouteDecision(") and c["business_fields_rejected"] == "condition_unsupported_expression" and c.get("condition_raw")
    capex = next(r for r in snap["rules"] if "NEEDS_FINANCE" in r["outcome"]["kind"] and "2000000" in json.dumps(r["condition"]) and "capital" not in r["title"].lower())
    assert capex["actor_ref"] == "role-finance" and capex["status"] == "DRAFT"                                # the model's outcome text is kept; the branch link names the value
