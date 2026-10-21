"""Local rehearsal server for the judge journey. EVERY MODEL OUTPUT HERE IS SIMULATED (deterministic stub providers):
no network, no cloud, no spend. It exists so a person can click through explorer -> confirm -> request -> draft -> impact ->
approval -> apply -> new graph -> query BEFORE anything is deployed. It is not live GenAI evidence.

  python -m tools.judge_local.run_local            # starts on http://127.0.0.1:8765 (REHEARSAL_PORT to change)

Options (2026-10-21, human acceptance): REHEARSAL_KNOWLEDGE=curated (default; the curated Harbor observed rules stand in for the extraction result) or
`extracted`; REHEARSAL_WORKER=1 (default; an in-process job worker with the simulated stubs) or 0. Nothing is accepted or allowed for a person: every AI run
needs a second person in this shared trial. No Google credentials are visible to the process, so no real model can be called.

Needs a disposable PostgreSQL (default: the draft container on 127.0.0.1:55831; the database `rehearsal` is DROPPED and
re-created on every start - never point REHEARSAL_ADMIN_DSN at a database you care about). Source files and blobs live in
memory: a restart is a full reset to the seed (public Harbor files, originals untouched). Passwords are random per
start and printed once to this console only.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ADMIN_DSN = os.environ.get("REHEARSAL_ADMIN_DSN", "host=127.0.0.1 port=55831 dbname=postgres user=postgres password=localtest")
DB = os.environ.get("REHEARSAL_DB", "rehearsal")        # a second build uses its OWN database (e.g. uiux_rehearsal); never the other one
DSN = ADMIN_DSN.replace("dbname=postgres", f"dbname={DB}")
PORT = int(os.environ.get("REHEARSAL_PORT", "8765"))
_KEY = "routing_rules.standard_manager_threshold_jpy:"


def _curated_knowledge(env):
    """REHEARSAL_KNOWLEDGE=curated (the default since 2026-10-21; `extracted` keeps only the simulated extractor's thin candidates): stands in for the extraction
    RESULT with the CURATED Harbor observed rules the automated tests use (bound to this rehearsal's real source revision, never confirmed, PENDING_HUMAN, not
    analysed). The person then reads the business rules from the sources in the UI (Questions & Decisions -> Rules, "Read the business rules from the sources"),
    answers the business questions and accepts the knowledge themselves. Fixture knowledge, not model extraction; the console says so."""
    from tests.final_loop.test_kno01_business_store import bind
    from tests.final_loop.test_kno03_graph_gaps import seed
    from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE
    head = env.store.head(env.tenant, env.ws)
    seed(env, head, rules=bind(KNOWLEDGE["observed_rules"] + DISTRACTORS, head.source_revision_id), key="rehearsal-curated", snapshot_id="snap-rehearsal-curated")


def _fresh_database():
    """DESTRUCTIVE, LOCAL ONLY: drops and re-creates the throwaway database named exactly `rehearsal` on a loopback server.
    It must never be reused for a cloud sandbox reset (a cloud reset is a history restore toward the seed; history, audit and
    the cost ledger are never deleted)."""
    import psycopg2
    host = [p.split("=", 1)[1] for p in ADMIN_DSN.split() if p.startswith("host=")]
    if not re.fullmatch(r"[a-z0-9_]*rehearsal", DB) or not host or host[0] not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to drop a database: only a loopback database whose name ends with 'rehearsal' may be dropped")
    conn = psycopg2.connect(ADMIN_DSN, connect_timeout=3)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (DB,))
    cur.execute(f'DROP DATABASE IF EXISTS "{DB}"')
    cur.execute(f'CREATE DATABASE "{DB}"')
    conn.close()


def _usage(prompt_tokens=40, out_tokens=30):
    return SimpleNamespace(prompt_token_count=prompt_tokens, candidates_token_count=out_tokens,
                           thoughts_token_count=None, total_token_count=prompt_tokens + out_tokens)


def main() -> int:
    import pytest
    try:
        _fresh_database()
    except Exception as exc:
        print(f"cannot reach the rehearsal PostgreSQL ({type(exc).__name__}). Start it: docker start ripple-draft-pg", file=sys.stderr)
        return 2
    os.environ["RIPPLE_V2_DSN"] = DSN
    os.environ["RIPPLE_V2_BLOB_BUCKET"] = "local-rehearsal"
    # no real model can be reached from the rehearsal (2026-10-21): no Google credentials and an empty SDK configuration, so an unpatched path fails closed
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GCP_PROJECT"):
        os.environ.pop(name, None)
    os.environ["CLOUDSDK_CONFIG"] = tempfile.mkdtemp(prefix="ripple_rehearsal_no_gcloud_")
    os.environ.pop("RIPPLE_JUDGE_ORIGIN", None)
    from tests.pitch_brief import conftest as C
    # the real candidate budget config, plus the Final knowledge-loop roles pinned to the lite model so their (SIMULATED) jobs are admitted; written to a temp copy, never edited in place
    _final_roles = "".join(f"  {r}: gemini-3.5-flash-lite\n" for r in ("knowledge.qa", "change.understand", "change.impact_deps", "change.draft_guidance", "knowledge.gap_proposals", "gap.answer_interpret",
                                                                        # extension screens (2026-10-19): their SIMULATED jobs decline with a labelled note (sim_final.EXTRA)
                                                                        "knowledge.mining", "document.wording", "document.variant", "gap.interview", "gap.interview_question",
                                                                        "ma.specialist", "change.risk_memo", "ma.synthesis_memo",
                                                                        "request.assistant", "request.qa"))      # request assistant: SIMULATED decline (sim_final.EXTRA)
    _cfg = C.REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("role_model_ids:\n", "role_model_ids:\n" + _final_roles, 1)
    _cfg_path = pathlib.Path(tempfile.gettempdir()) / "ripple_rehearsal_budget.yaml"
    _cfg_path.write_text(_cfg, encoding="utf-8")
    os.environ["RIPPLE_BUDGET_CONFIG_PATH"] = str(_cfg_path)

    import uvicorn
    from fastapi.testclient import TestClient
    from infra import judge, v2_source_routes
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    from infra.worker import change_draft, extraction, gap_answer, intent_interpret
    from tools.judge_local.sim_interpreter import SimInterpreter
    from tests.pitch_brief.chain import APPROVAL, build
    from tests.pitch_brief.test_change_draft import Model

    blobs = C.MemoryBlobs()
    v2_source_routes._blob_port = lambda: blobs
    tenant, project, ws = "rehearsal-sim", "rehearsal-sim-p", "rehearsal-sim-ws"
    store = PostgresV2Store(DSN, blobs)
    store.migrate()
    store.create_workspace(tenant, project, ws)
    store.set_project_model_policy(tenant, project, external_provider="allow")   # providers below are stubs
    ids = PostgresIdentityStore(DSN)
    ids.migrate()
    creds = {}
    for name, roles in (("rehearsal-admin", None), ("rehearsal-requester", ("requester",)),
                        ("rehearsal-approver", ("change_approver",))):
        creds[name] = secrets.token_urlsafe(9)
        if roles is None:
            ids.bootstrap_admin(tenant, project, ws, name, creds[name])
        else:
            ids.create_account(name, creds[name])
            ids.grant_membership(tenant, project, ws, name, roles)

    def client(account):
        c = TestClient(judge.app, base_url="https://judge.test")
        c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        assert c.post("/v2/auth/login", json={"account_id": account, "credential": creds[account]}).status_code == 200
        return c

    env = SimpleNamespace(tenant=tenant, ws=ws, store=store, blobs=blobs, dsn=DSN, admin="rehearsal-admin",
                          req="rehearsal-requester", admin_client=lambda: client("rehearsal-admin"),
                          req_client=lambda: client("rehearsal-requester"))
    ch = build(env, pytest.MonkeyPatch(), finguide_scope={"category": "all"})   # real import + extraction via the worker (SIMULATED model)
    static = ch.stub
    curated = os.environ.get("REHEARSAL_KNOWLEDGE", "curated") == "curated"
    if curated:
        _curated_knowledge(env)

    class Extractor:
        """SIMULATED extraction: reads the CURRENT approval.yaml value from the prompt, so a new revision re-extracts the new
        value; other files use the fixed Harbor-shaped payloads. No model is called."""
        def __init__(self):
            self.calls = 0
            self.models = SimpleNamespace(generate_content=self._gen)

        def _gen(self, **kw):
            self.calls += 1
            prompt = kw.get("contents") or ""
            if _KEY in prompt:
                line = next(x for x in prompt.splitlines() if _KEY in x)
                val = int(line.rsplit(":", 1)[1].strip())
                cands = json.loads(json.dumps(static.payloads[0][1]))
                cands[0]["value"] = val
                cands[0]["quotes"] = [f"{_KEY} {val}"]
                return SimpleNamespace(text=json.dumps({"candidates": cands}), model_version=C.EXTRACT_MODEL,
                                       candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=_usage())
            return static._gen(**kw)

    class Answerer:
        def __init__(self):
            self.models = SimpleNamespace(generate_content=self._gen)

        def _gen(self, **kw):
            prompt = kw.get("contents") or ""
            line = next((x for x in prompt.splitlines() if _KEY in x), None)
            if line is None:
                obj = {"status": "abstained", "answer": None, "states_absence": False, "evidence": [],
                       "question_for_human": "SIMULATED provider found no matching line.", "scope_examined": [APPROVAL]}
            else:
                val = line.rsplit(":", 1)[1].strip()
                obj = {"status": "answered", "answer": f"{val} JPY (SIMULATED answer read from the stored text)",
                       "states_absence": False, "evidence": [{"artifact_path": APPROVAL, "quote": f"{_KEY} {val}"}],
                       "question_for_human": None, "scope_examined": [APPROVAL]}
            return SimpleNamespace(text=json.dumps(obj), model_version=C.GAP_MODEL,
                                   candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=_usage())

    def ask(obj, prompt):
        # SIMULATED draft: raises one open question until a confirmed clarification is part of the request.
        if "confirmed_clarifications" not in prompt:
            obj["questions_for_human"] = ["Should this change also apply to any channel or document the evidence did not cover?"]

    class SimDrafter(Model):
        """SIMULATED analyst for ANY request (generic rules over the stored evidence, nothing Harbor-specific): a YAML line
        whose key shares a word with the subject and holds the old value is proposed for edit; a workbook cell already at the
        new value is a no-op; text that mentions the subject is manual; the rest is a different subject."""

        def _gen(self, **kw):
            import re
            prompt = kw["contents"]
            try:
                ci = json.loads(prompt.split("<request>\n", 1)[1].split("\n</request>", 1)[0]).get("confirmed_intent", {})
            except (IndexError, ValueError):
                ci = {}
            toks = set(re.findall(r"[a-z]{3,}", str(ci.get("subject", "")).lower())) - {"the", "and", "limit", "for", "new"}
            old, new = str(ci.get("old_value")), str(ci.get("new_value"))
            has = lambda text, num: re.search(r"(?<![\d.])" + re.escape(num) + r"(?![\d])", text.replace(",", "")) is not None

            def decide(e):
                t = e["text"].lower().replace("_", " ")
                hit = any(w in t for w in toks)
                if e["file_kind"] == "yaml" and hit and has(e["text"], old):
                    return "edit", "the key for this subject holds the old value"
                if e["file_kind"] == "xlsx" and hit:
                    return ("no_op", "the workbook already shows the new value") if has(e["text"], new) else ("manual", "a person edits the workbook")
                if not hit:
                    return "exclude", "a different subject"
                return "manual", "the document mentions it; a person edits"
            self.decide = decide
            return super()._gen(**kw)

    ext, ans, drafter = Extractor(), Answerer(), SimDrafter(mutate=ask)
    extraction._client_for_model = lambda model: ext
    gap_answer._client_for_model = lambda model: ans
    change_draft._client_for_model = lambda model: drafter
    change_draft._assert_global_endpoint = lambda: None
    interp = SimInterpreter(C.EXTRACT_MODEL)                                   # SIMULATED chat interpreter (rules, not a model)
    intent_interpret._client_for_model = lambda model: interp
    intent_interpret._assert_global_endpoint = lambda: None
    from infra.worker import explain
    from tools.judge_local.sim_explain import SimWording
    from tools.judge_local.sim_final import SimFinal
    sim_final = SimFinal("gemini-3.5-flash-lite", SimWording("gemini-3.5-flash-lite"))      # Final AI roles DECLINE (schema-valid, labelled); wording stub for the rest
    explain._client_for_model = lambda model: sim_final                                     # SIMULATED wording of verified facts + Final roles
    explain._assert_global_endpoint = lambda: None

    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=20).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        head, dirty = "unknown", False
    os.environ["RIPPLE_PROVIDER_MODE"] = "SIMULATED"   # declared by the operator; the UI never infers LIVE from a model-shaped name
    os.environ["RIPPLE_UI_NOTICE"] = ("LOCAL TEST - the AI is SIMULATED (rules, not a real model). Requests about scope, approvers or exceptions are only NOTED, "
                                      "not carried out. Several steps need ANOTHER person to approve. (click for details)")
    os.environ["RIPPLE_UI_NOTICE_DETAILS"] = (
        f"Local rehearsal build, code {head}{' + uncommitted changes' if dirty else ''}. GenAI outputs are SIMULATED (stub providers, not live evidence). "
        "Harbor simulation is REAL-RUN-OFFLINE: Harbor's own CLI from a pinned, hash-verified public copy, deterministic, no model call. "
        "The runner is resource-limited and is NOT a security boundary; no uploaded or model-written code is executed. "
        "Restarting this server resets the rehearsal data to the seed (the rehearsal database is re-created); do not restart while someone is testing.")
    os.environ["RIPPLE_SANDBOX_CONFIG"] = C.sandbox_config(ws, ch.rev)       # the canonical limits file, unchanged
    server = uvicorn.Server(uvicorn.Config(judge.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    worker = None
    if os.environ.get("REHEARSAL_WORKER", "1") == "1":                                      # on by default (2026-10-21): jobs queued after start-up need a worker;
        from tests.pitch_brief.conftest import LiveWorker                                    # it runs IN this process, so it uses the SIMULATED stubs above (no real model)
        worker = LiveWorker(env).start()
    print("=" * 78)
    print("RIPPLE LOCAL REHEARSAL - GenAI SIMULATED (stub providers, no spend); Harbor simulation REAL-RUN-OFFLINE (pinned public copy)")
    print(f"Server code HEAD: {head}{' (+ uncommitted changes)' if dirty else ''}")
    print(f"Open http://127.0.0.1:{PORT}/app-next/  (the new interface; the old one is still at /app)  (use a different BROWSER or browser PROFILE per person: windows of one browser share the login cookie)")
    print(f"Workspace: {ws}   seed source revision: {ch.rev}")
    if worker is not None:
        print("Job worker: running inside this process (SIMULATED stubs; no real model call). Every AI run still needs a SECOND PERSON to allow it in this shared trial:")
        print("  the other account sees it under Questions & Decisions -> 'AI runs waiting for a second person'. Nothing is allowed or accepted automatically.")
    if curated:
        print("Business knowledge: CURATED Harbor observed rules stand in for the extraction result (the automated tests' fixture, not a model; nothing confirmed).")
        print("  Next, as admin: take the lease (Sandbox), then Questions & Decisions -> Rules -> 'Read the business rules from the sources'.")
    print("Simulated AI: business Q&A declines (labelled); change requests are read by RULES (one number on one rule) - success, a purposeful question, or a refusal.")
    for name in creds:
        print(f"  account {name:22s} password {creds[name]}")
    print("Roles: admin confirms knowledge and approves model jobs; requester drafts; approver decides changes (distinct people).")
    print("First step on the Sandbox page: take the writing session (list the other accounts as participants).")
    print("Ctrl+C stops the server; restarting resets everything to the seed.")
    print("=" * 78, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
