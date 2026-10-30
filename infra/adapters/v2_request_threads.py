"""Private request-assistant threads and messages (RQA-02; contract docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md section 5).

A thread is its creator's alone, for ONE request; every read, ask and cancel checks, in this order: the thread belongs to the caller (else "thread not found"), the caller may
still read the request (else the whole thread is withheld as "thread not found"), and, for an ask, the caller holds `request.assistant.ask` (removed by `ai_suspended`).
Creating a thread and reading one never call a model. An ask stores the question with the context manifest and the caller's authorization fingerprint and creates ONE
admitted `request.qa` job (idempotent per thread + key; a stale `after_seq` from another tab is refused instead of interleaving). An answer is shown only when the caller's
CURRENT authorization fingerprint equals the one it was asked under; otherwise it is withheld in full (never redacted piecemeal) until the person asks again.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from psycopg2.extras import Json

from core.change_intelligence.v2 import request_assistant as ra
from infra.adapters import v2_request_assistant as vra
from infra.adapters import v2_request_tasks
from infra.adapters.v2_policy import PermissionDenied, permissions_for
from infra.adapters.v2_postgres import TenantScopeError, _as_object

CAPABILITY = "request.assistant.ask"
THREAD_NOT_FOUND = "thread not found"
AUDIT = {"thread": "REQUEST_ASSISTANT_THREAD_CREATED", "ask": "REQUEST_ASSISTANT_ASKED", "cancel": "REQUEST_ASSISTANT_CANCELLED"}


class AssistantRefused(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def _limits() -> dict:
    from infra.worker.request_assistant import load_config
    return load_config()[0]["limits"]


def _audit(cur, tenant, ws, event, actor, payload):
    cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)", (tenant, ws, event, actor, Json(payload)))


def _require_capability(authority):
    if CAPABILITY not in permissions_for(authority.get("roles")):
        raise PermissionDenied(CAPABILITY)


OFF_MESSAGE = "The request assistant is switched off on this server. Earlier answers stay readable; the request and its actions work as usual."


def _require_feature() -> None:
    from infra import operator_flags
    if not operator_flags.assistant_on():
        raise AssistantRefused("assistant_off", OFF_MESSAGE, 409)


def create_thread(store, tenant, ws, family, rid, authority) -> dict:
    _require_capability(authority)
    _require_feature()
    tv = v2_request_tasks.task_view(store, tenant, ws, family, rid, authority)               # the family read rule; generic 404 otherwise
    ref = tv["request_ref"].get("canonical_ref") or tv["request_ref"]
    tid = "rqt-" + uuid.uuid4().hex[:20]
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_request_threads (tenant_id, workspace_id, thread_id, family, request_id, created_by) VALUES (%s,%s,%s,%s,%s,%s)",
                    (tenant, ws, tid, ref["family"], ref["id"], authority["account_id"]))
        _audit(cur, tenant, ws, AUDIT["thread"], authority["account_id"], {"thread_id": tid, "family": ref["family"], "request_id": ref["id"]})
    return read_thread(store, tenant, ws, tid, authority)


def list_threads(store, tenant, ws, family, rid, authority) -> dict:
    tv = v2_request_tasks.task_view(store, tenant, ws, family, rid, authority)
    ref = tv["request_ref"].get("canonical_ref") or tv["request_ref"]
    with store._tx() as cur:
        cur.execute("""SELECT t.thread_id, t.created_at, (SELECT count(*) FROM v2_request_messages m WHERE m.tenant_id=t.tenant_id AND m.workspace_id=t.workspace_id
                         AND m.thread_id=t.thread_id) FROM v2_request_threads t WHERE t.tenant_id=%s AND t.workspace_id=%s AND t.created_by=%s AND t.family=%s AND t.request_id=%s
                       ORDER BY t.created_at DESC""", (tenant, ws, authority["account_id"], ref["family"], ref["id"]))
        rows = cur.fetchall()
    return {"request_ref": ref, "threads": [{"thread_id": r[0], "created_at": r[1].isoformat(), "messages": r[2]} for r in rows], "private": True}


def _thread(store, tenant, ws, tid, authority) -> dict:
    with store._tx() as cur:
        cur.execute("SELECT family, request_id, created_by, created_at FROM v2_request_threads WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s", (tenant, ws, tid))
        r = cur.fetchone()
    if r is None or r[2] != authority["account_id"]:
        raise TenantScopeError(THREAD_NOT_FOUND)                                              # another person's thread looks exactly like a missing one
    try:
        pack = vra.build_context(store, tenant, ws, r[0], r[1], authority)                  # the request must still be readable now
    except TenantScopeError as exc:
        raise TenantScopeError(THREAD_NOT_FOUND) from exc
    except vra.ContextOverflow as exc:                                                       # AIG-02: readable, but its mandatory facts do not fit one pack: no new question
        pack = {"facts": [], "coverage": {"overflow": exc.reason}, "auth": vra.auth_fingerprint(r[0], authority), "as_of": None, "manifest_hash": None, "overflow": exc.reason}
    return {"thread_id": tid, "family": r[0], "request_id": r[1], "created_by": r[2], "created_at": r[3].isoformat(), "pack": pack}


def _messages(store, tenant, ws, tid) -> list:
    with store._tx() as cur:
        cur.execute("""SELECT message_id, seq, question, asked_by, idempotency_key, payload_hash, job_id, manifest_hash, auth, as_of, answer, answered_at, created_at
                       FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s ORDER BY seq""", (tenant, ws, tid))
        rows = cur.fetchall()
    return [{"message_id": r[0], "seq": r[1], "question": r[2], "asked_by": r[3], "idempotency_key": r[4], "payload_hash": r[5], "job_id": r[6], "manifest_hash": r[7],
             "auth": _as_object(r[8]), "as_of": r[9], "answer": _as_object(r[10]), "answered_at": r[11].isoformat() if r[11] else None, "created_at": r[12].isoformat()} for r in rows]


def _settle(store, tenant, ws, m, current_auth) -> dict:
    """Pull the job's validated answer into the message (publish), only under the same authorization the question was asked with."""
    if m["answer"] is not None or m["auth"] != current_auth:
        return m
    try:
        rec = store.job_record(tenant, ws, m["job_id"])
    except TenantScopeError:
        return m
    st = rec.get("execution_status")
    if st == "COMPLETED" and isinstance(rec.get("execution_result"), dict):
        ans = {k: rec["execution_result"].get(k) for k in ("status", "outcome", "blocks", "rejected", "limitations", "next_actions", "as_of", "manifest_hash",
                                                           "answer_schema", "checks_version", "locale", "context_version", "explained_fact_kinds", "blocker_coverage", "authority_accounting", "parts", "policy_refusal",
                                                           "limitation_support")}                    # checks/30: what a kept limitation rests on (absent in older answers)
    elif st in ("FAILED", "CANCELLED", "REJECTED"):
        ans = {"status": "unavailable" if st == "FAILED" else "cancelled", "reason": rec.get("execution_reason"), "blocks": [], "rejected": [], "limitations": []}
    else:
        return dict(m, phase="running" if st == "RUNNING" else "queued")              # R11: whether Stop can still take effect
    with store._tx() as cur:
        cur.execute("UPDATE v2_request_messages SET answer=%s, answered_at=now() WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s AND answer IS NULL",
                    (Json(ans), tenant, ws, m["message_id"]))
    return dict(m, answer=ans)


LEGACY_NOTE = ("This answer was checked by an earlier version of the assistant's checks. Statements about who holds a step or who may act are not shown here; "
               "use the request's task panel, or ask again.")


def _legacy_view(answer: dict) -> dict:
    """AIG-01: an answer stored before typed authority (checks < 4) is shown as it was stored, except free blocks that state authority, which are withheld on read.
    The stored record is not rewritten and nothing is regenerated."""
    if answer.get("checks_version") in ra.COMPATIBLE_CHECKS or answer.get("status") in ("unavailable", "cancelled"):
        return answer
    blocks = answer.get("blocks") or []
    kept = [b for b in blocks if b.get("kind") == "authority" or not ra.free_text_authority(b.get("text") or "")]
    return {**answer, "blocks": kept, "legacy_checks": True, "legacy_withheld": len(blocks) - len(kept), "legacy_note": LEGACY_NOTE}


def reason_code(reason) -> str:
    """R11: a stable code for why no answer came, for the page's plain-language text (the stored reason stays as recorded)."""
    r = str(reason or "")
    if "ProviderTimeoutError" in r:
        return "provider_timeout"                                                         # no answer before the deadline, or an answer after it (not used)
    if r.startswith("provider_attempt_limit_reached"):
        return "attempt_limit"                                                            # an earlier attempt's outcome is unknown (worker stopped); none is repeated
    if r.startswith("provider_execution_failed"):
        return "provider_failed"
    if "cancel" in r:
        return "cancelled"
    return "not_answered"


def _public(m, current_auth, pack) -> dict:
    out = {"message_id": m["message_id"], "seq": m["seq"], "question": m["question"], "created_at": m["created_at"], "as_of": m["as_of"]}
    if m["auth"] != current_auth:
        return {**out, "state": "withheld", "reason": "Your access to this request changed after this question; the answer is withheld. Ask again to get an answer for your current access."}
    if m["answer"] is None:
        return {**out, "state": "pending", "phase": m.get("phase", "queued")}
    answer = _legacy_view(m["answer"])
    if answer.get("status") in ("unavailable", "cancelled"):
        answer = {**answer, "reason_code": reason_code(answer.get("reason")), "reference": m["message_id"]}   # the reference support uses to find the job
    facts = {f["id"]: f for f in pack["facts"]} if m["manifest_hash"] == pack["manifest_hash"] else {}
    refs = [r for b in answer.get("blocks") or [] for r in b["fact_refs"]] + [r for s in answer.get("limitation_support") or [] for r in s.get("fact_refs") or []]
    return {**out, "state": "answered" if answer.get("status") not in ("unavailable", "cancelled") else answer["status"], "answer": answer,
            "current": m["manifest_hash"] == pack["manifest_hash"], "citations": facts and {r: facts[r] for r in refs if r in facts}}   # checks/30: + what a kept limitation rests on


def read_thread(store, tenant, ws, tid, authority) -> dict:
    t = _thread(store, tenant, ws, tid, authority)
    pack = t.pop("pack")
    msgs = [_settle(store, tenant, ws, m, pack["auth"]) for m in _messages(store, tenant, ws, tid)]
    return {**t, "private": True, "as_of": pack["as_of"], "context": {"facts": len(pack["facts"]), "coverage": pack["coverage"], "auth": pack["auth"]},
            "messages": [_public(m, pack["auth"], pack) for m in msgs],
            "statement": "This conversation is private to you. The assistant reads only what you may read about this request and never acts; decisions use the request's own controls."}


def ask(store, tenant, ws, tid, authority, *, question, idempotency_key, after_seq) -> dict:
    _require_capability(authority)
    _require_feature()
    lim = _limits()
    if not isinstance(question, str) or not question.strip() or len(question) > int(lim["max_question_chars"]):
        raise AssistantRefused("question_invalid", f"a question of 1-{lim['max_question_chars']} characters is required", 400)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 80 or isinstance(after_seq, bool) or not isinstance(after_seq, int):
        raise AssistantRefused("idempotency_and_sequence_required", "idempotency_key (8-80 characters) and after_seq (the last message number you saw) are required", 400)
    t = _thread(store, tenant, ws, tid, authority)
    pack = t["pack"]
    question = " ".join(question.split())
    payload_hash = hashlib.sha256(json.dumps([tid, question], ensure_ascii=False).encode("utf-8")).hexdigest()
    msgs = _messages(store, tenant, ws, tid)
    prior = next((m for m in msgs if m["idempotency_key"] == idempotency_key), None)
    if prior is not None:
        if prior["payload_hash"] != payload_hash:
            raise AssistantRefused("idempotency_conflict", "the same key was used for a different question")
        store.link_job_request(tenant, ws, prior["job_id"])                                    # AH-05: the retrying request is linked to the same job
        return {**read_thread(store, tenant, ws, tid, authority), "replayed": True, "message_id": prior["message_id"]}
    if pack.get("overflow"):                                                                  # refused before any job or provider call; nothing is trimmed to fit
        raise AssistantRefused("context_overflow", "this request has more required facts than one question can carry; use the request's own pages", 409)
    last = msgs[-1]["seq"] if msgs else 0
    if after_seq != last:
        raise AssistantRefused("sequence_conflict", "the conversation changed in another window; reload it before asking")
    if len(msgs) >= int(lim["max_messages_per_thread"]):
        raise AssistantRefused("thread_full", "this conversation reached its message limit; start a new one")
    from infra import ai_admission, provider_gate
    from infra.v2_business_routes import _quota_for
    from infra.worker.request_assistant import KIND, job_id_for
    provider_gate.require_provider(KIND)
    mid = "rqm-" + uuid.uuid4().hex[:20]
    jid = job_id_for(tid, mid)
    payload = {"thread_id": tid, "message_id": mid, "family": t["family"], "request_id": t["request_id"], "manifest_hash": pack["manifest_hash"], "asked_by": authority["account_id"]}
    limit, reservation = _quota_for(KIND)
    head = store.head(tenant, ws)
    import psycopg2
    try:
        with store._tx() as cur:                                                              # the message row first (seq and key are unique per thread: a racing tab fails here)
            cur.execute("""INSERT INTO v2_request_messages (tenant_id, workspace_id, thread_id, message_id, seq, question, asked_by, idempotency_key, payload_hash, job_id,
                           manifest_hash, auth, as_of) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant, ws, tid, mid, last + 1, question, authority["account_id"], idempotency_key, payload_hash, jid, pack["manifest_hash"], Json(pack["auth"]), pack["as_of"]))
    except psycopg2.IntegrityError:                                                           # a concurrent click of the SAME question won the race: replay it, never a second job
        won = next((m for m in _messages(store, tenant, ws, tid) if m["idempotency_key"] == idempotency_key), None)
        if won is not None and won["payload_hash"] == payload_hash:
            store.link_job_request(tenant, ws, won["job_id"])
            return {**read_thread(store, tenant, ws, tid, authority), "replayed": True, "message_id": won["message_id"]}
        raise AssistantRefused("sequence_conflict", "the conversation changed in another window; reload it before asking")
    try:
        store.create_job(tenant, ws, jid, KIND, payload, expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit,
                         budget_reservation=reservation, requested_by=authority["account_id"])
    except Exception:
        with store._tx() as cur:                                                              # no job, no question: the person can ask again
            cur.execute("DELETE FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s AND answer IS NULL", (tenant, ws, mid))
        raise
    ai_admission.admit(store, tenant, ws, jid, KIND, authority)
    with store._tx() as cur:
        _audit(cur, tenant, ws, AUDIT["ask"], authority["account_id"], {"thread_id": tid, "message_id": mid, "job_id": jid, "chars": len(question)})
    return {**read_thread(store, tenant, ws, tid, authority), "replayed": False, "message_id": mid}


SOURCE_FACT_KINDS = ("impact", "rule_change")
CITATION_PAGE = 10                                                                    # AH-02: bindings resolved per citation request (the page shown)


def _rules_for_fact(store, tenant, ws, family, rid, authority, fact) -> list:
    """The business rules a cited fact is about, resolved by the SERVER from the fact's typed key (never from client text): an impact fact by its logical-rule
    key, a draft rule change by its position in the stored draft. Other facts have no source locator."""
    key = str(fact.get("key") or "")
    if family != "business_change":
        return []
    from infra.adapters import v2_business
    from core.change_intelligence.v2.business_impact import ref_key
    if fact["kind"] == "impact" and key.startswith("impact:rk-"):
        _view, snap = v2_business._latest_view(store, tenant, ws)
        return [r for r in (snap or {}).get("rules") or [] if ref_key(r["rule_id"]) == key[len("impact:"):]]
    if fact["kind"] == "rule_change" and key.startswith("rule_change:draft:"):
        from infra.adapters import v2_business_review
        view = v2_business_review.review_view(store, tenant, ws, rid, account_id=authority["account_id"], roles=authority.get("roles"))
        try:
            rule_id = ((view or {}).get("draft") or {})["rule_changes"][int(key.split(":")[2])]["rule_id"]   # R12: "...:NNN:range" (computed from that rule) has the same source
        except (KeyError, IndexError, ValueError, TypeError):
            return []
        _view, snap = v2_business._latest_view(store, tenant, ws)
        return [r for r in (snap or {}).get("rules") or [] if r["rule_id"] == rule_id]
    return []


def _yaml_excerpt(store, tenant, ws, revision_id, artifact_id, path, locator):
    if not locator or not path.lower().endswith((".yaml", ".yml")):
        return None
    try:
        import yaml
        blob = store.read_artifact_blob(tenant, ws, revision_id, artifact_id)
        data = yaml.safe_load(blob["content"] if isinstance(blob.get("content"), (bytes, str)) else b"")
        for part in locator.split("."):
            data = data[part]
        if isinstance(data, (dict, list)):
            return None
        return f"{locator}: {data}"
    except Exception:                                                                     # noqa: BLE001 - no excerpt rather than a guess
        return None


def citation_source(store, tenant, ws, tid, mid, fact_ref, authority) -> dict:
    """EUI-03 citation -> source, resolved by the server from the pack the answer was built on (same manifest), rechecked NOW: the asker's own thread, the
    request still readable, the same authorization, the fact cited by this answer, `evidence.read` for any path or excerpt. Never a model call, never a
    client-supplied path; a stale context, a non-source fact or missing rights get an honest reason instead of a location."""
    t = _thread(store, tenant, ws, tid, authority)
    pack = t["pack"]
    m = next((x for x in _messages(store, tenant, ws, tid) if x["message_id"] == mid), None)
    if m is None or m["answer"] is None:
        raise TenantScopeError(THREAD_NOT_FOUND)
    out = {"fact_ref": fact_ref, "available": False}
    if m["auth"] != pack["auth"]:
        return {**out, "reason": "access_changed"}
    cited = {r for b in m["answer"].get("blocks") or [] for r in b.get("fact_refs") or []}
    if fact_ref not in cited:
        return {**out, "reason": "not_cited_by_this_answer"}
    if m["manifest_hash"] != pack["manifest_hash"]:
        return {**out, "reason": "stale_context"}                                        # the request changed since the answer: the stored pack is not the current one
    fact = next((f for f in pack["facts"] if f["id"] == fact_ref), None)
    if fact is None:
        return {**out, "reason": "stale_context"}
    out["kind"] = fact["kind"]
    if fact["kind"] not in SOURCE_FACT_KINDS:
        return {**out, "reason": "no_source_locator"}                                     # local fact: the page shows it; no locator is made up
    if "evidence.read" not in permissions_for(authority.get("roles")):
        return {**out, "reason": "needs_evidence_rights"}                                 # no path, no excerpt
    rules = _rules_for_fact(store, tenant, ws, t["family"], t["request_id"], authority, fact)
    # AH-02: select the bounded, de-duplicated set of bindings FIRST (rule order, then binding order, first occurrence kept), then do the per-binding work for
    # that page only, with bulk reads: before, every binding cost a head read, two revision reads and a blob read, and all of it was done before `[:10]`.
    seen, bindings = set(), []
    for r in rules:
        for b in r.get("source_bindings") or []:
            key = (b.get("artifact_id"), b.get("revision_id"), str(b.get("evidence_ref")))
            if key not in seen:
                seen.add(key)
                bindings.append(b)
    if not bindings:
        return {**out, "reason": "no_source_binding"}
    page = bindings[:CITATION_PAGE]
    head = store.head(tenant, ws)
    ids = sorted({b["artifact_id"] for b in page})
    pairs = sorted({(b["revision_id"], b["artifact_id"]) for b in page})
    with store._tx() as cur:
        now = {}
        if head.source_revision_id is not None:
            cur.execute("""SELECT artifact_id, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                           AND artifact_id = ANY(%s)""", (tenant, ws, head.source_revision_id, ids))
            now = dict(cur.fetchall())
        cur.execute("""SELECT r.source_revision_id, r.artifact_id, r.blob_hash FROM v2_artifact_revisions r
                       JOIN unnest(%s::text[], %s::text[]) AS w(rev, art) ON r.source_revision_id = w.rev AND r.artifact_id = w.art
                       WHERE r.tenant_id=%s AND r.workspace_id=%s""", ([x[0] for x in pairs], [x[1] for x in pairs], tenant, ws))
        then = {(row[0], row[1]): row[2] for row in cur.fetchall()}
    from core.change_intelligence.v2.business_draft import binding_location
    paths: dict = {}                                                                      # revision -> {artifact_id: logical_path}, read once per revision on the page
    sources = []
    for b in page:
        rev, art = b["revision_id"], b["artifact_id"]
        if rev not in paths:
            try:
                view = store.source_revision_view(tenant, ws, rev)
                paths[rev] = {a["artifact_id"]: a.get("logical_path") or a["artifact_id"] for a in view["artifacts"]}
            except Exception:                                                             # noqa: BLE001 - a missing revision gives no path, never a guess
                paths[rev] = {}
        path, loc = binding_location(b, paths[rev])                                       # JSON code / line locators keep their file (same rule as the draft)
        row = then.get((rev, art))
        current = now.get(art)
        state = "absent_now" if current is None else ("current" if rev == head.source_revision_id or (row is not None and row == current) else "changed_since")
        sources.append({"logical_path": path, "locator": loc, "revision_id": rev, "state": state,
                        "excerpt": _yaml_excerpt(store, tenant, ws, rev, art, path, loc) if row is not None else None})
    return {**out, "available": True, "sources": sources, "sources_total": len(bindings), "has_more": len(bindings) > len(page)}


def first_thread_id(tenant: str, ws: str, account: str, ref: dict, idempotency_key: str) -> str:
    return "rqt-" + hashlib.sha256(json.dumps([tenant, ws, account, ref["family"], ref["id"], idempotency_key]).encode("utf-8")).hexdigest()[:20]


def first_ask(store, tenant, ws, family, rid, authority, *, question, idempotency_key) -> dict:
    """EUI-02: the first question of a new private conversation as ONE idempotent action. The thread id is derived from the person, the request and the key, so a lost
    response, a retry or a concurrent click with the same key reuses the same thread and the same message (at most one job and one reservation). Opening the panel
    creates nothing; only this explicit ask does."""
    _require_capability(authority)
    _require_feature()
    lim = _limits()
    if not isinstance(question, str) or not question.strip() or len(question) > int(lim["max_question_chars"]):   # checked before any row is written
        raise AssistantRefused("question_invalid", f"a question of 1-{lim['max_question_chars']} characters is required", 400)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 80:
        raise AssistantRefused("idempotency_and_sequence_required", "idempotency_key (8-80 characters) is required", 400)
    tv = v2_request_tasks.task_view(store, tenant, ws, family, rid, authority)               # the family read rule; generic 404 otherwise
    ref = tv["request_ref"].get("canonical_ref") or tv["request_ref"]
    tid = first_thread_id(tenant, ws, authority["account_id"], ref, idempotency_key)
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_request_threads (tenant_id, workspace_id, thread_id, family, request_id, created_by) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, workspace_id, thread_id) DO NOTHING""", (tenant, ws, tid, ref["family"], ref["id"], authority["account_id"]))
        if cur.rowcount:
            _audit(cur, tenant, ws, AUDIT["thread"], authority["account_id"], {"thread_id": tid, "family": ref["family"], "request_id": ref["id"], "first_ask": True})
    return ask(store, tenant, ws, tid, authority, question=question, idempotency_key=idempotency_key, after_seq=0)


def cancel(store, tenant, ws, tid, mid, authority) -> dict:
    _thread(store, tenant, ws, tid, authority)
    m = next((x for x in _messages(store, tenant, ws, tid) if x["message_id"] == mid), None)
    if m is None:
        raise TenantScopeError(THREAD_NOT_FOUND)
    # R11: Stop says what it did. Only a question still waiting in the queue is cancelled (no provider call, nothing charged); one already being answered keeps
    # running (the provider call cannot be withdrawn), its result and cost are kept; nothing is claimed about stopping the provider or the charge
    outcome = "finished"
    if m["answer"] is None:
        st = (store.job_record(tenant, ws, m["job_id"]) or {}).get("execution_status")
        if st == "QUEUED":
            try:
                store.request_cancel(tenant, ws, m["job_id"], reason="cancelled by the person who asked")
            except Exception:                                                                 # noqa: BLE001 - started or finished meanwhile: read the state below
                pass
            after = (store.job_record(tenant, ws, m["job_id"]) or {}).get("execution_status")     # said only as the record now shows it
            outcome = "cancelled" if after == "CANCELLED" else "running" if after == "RUNNING" else "finished"
        elif st == "RUNNING":
            outcome = "running"
        with store._tx() as cur:
            _audit(cur, tenant, ws, AUDIT["cancel"], authority["account_id"], {"thread_id": tid, "message_id": mid, "job_id": m["job_id"], "outcome": outcome})
    return {**read_thread(store, tenant, ws, tid, authority), "cancel": {"message_id": mid, "outcome": outcome}}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
