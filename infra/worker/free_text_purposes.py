"""Free-text surfaces beyond the change request, on the SAME conversation machinery and the SAME interpreter role (`intent.interpret`):

  knowledge_qa   a question about the documents      -> preview {question, files}          -> the existing `gap.answer` job
  gap_reply      a reply to one open point of a draft -> preview {answer, kind, wish/fact} -> the existing GAP comment / decision routes
  draft_feedback feedback on the items of a draft     -> preview {reaction per item}        -> GAP comments (recorded, not authoritative)
  whatif         "what if the limit were 750,000"     -> preview {supported rule, value}    -> the existing simulation job
  question_reply a reply to a question of the work queue -> preview {answer, comment|decision, rule, fact|wish} -> /v2/hitl/answer or /v2/knowledge/confirm

Pipeline for each: the person's original words (kept) -> context collected on the SERVER from what this account may read (codes, never
ids, go into the prompt) -> one model call that only PROPOSES a structured preview -> the server verifies it (codes exist, numbers were
typed by the person, quotes are the person's words, text is safe) -> the person confirms the preview -> the person presses the existing
guarded action. Confirming a preview is not an approval and executes nothing. Nothing here calls a model by itself: the executor does,
under the same finite, revocable AI permission and budget reservation as the change-request chat.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

from infra import chat_safety, v2_chat_store as chat
from infra.chat_numbers import numbers_with_quotes
from infra.worker.change_draft import _canon_json, _clip

CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "free_text_interpret.json"
PURPOSES = ("knowledge_qa", "gap_reply", "draft_feedback", "whatif", "question_reply")
STATUSES = ("READY", "CLARIFY", "UNSUPPORTED")
REACTIONS = ("exclude", "ambiguous", "manual", "keep", "question")
START_PERMISSION = {"knowledge_qa": ("ai.use",), "gap_reply": ("knowledge.answer", "knowledge.confirm"), "question_reply": ("knowledge.answer", "knowledge.confirm"),
                    "draft_feedback": ("intent.create",), "whatif": ("change.prepare",)}

# What the Harbor what-if can really run (deterministic CLI). `note` states a limit the person must see before they run it.
WHATIF_RULES = (
    {"key": "routing_rules.standard_manager_threshold_jpy", "case": "route", "label": "Manager approval threshold (JPY)", "integer": True, "note": "",
     "scope": "Standard orders", "unchanged": "Emergency orders use their own rule and are not changed by this value."},
    {"key": "routing_rules.capex_finance_threshold_jpy", "case": "route", "label": "CAPEX finance threshold (JPY)", "integer": True,
     "note": "This value also changes emergency-process CAPEX rows, because the emergency code reads it.",
     "scope": "CAPEX purchases (including the emergency process, which reads the same value)", "unchanged": "Non-CAPEX orders are not changed."},
    {"key": "matching_rules.price_tolerance_fixed_jpy", "case": "match", "label": "Invoice price tolerance, fixed (JPY)", "integer": True, "note": "",
     "scope": "Invoice matching", "unchanged": "Approval routing is not changed."},
    {"key": "matching_rules.price_tolerance_percent", "case": "match", "label": "Invoice price tolerance, percent", "integer": True, "note": "",
     "scope": "Invoice matching", "unchanged": "Approval routing is not changed."},
    {"key": "matching_rules.payment_term_days", "case": "match", "label": "Payment terms (days)", "integer": True,
     "note": "The configuration does not control this behaviour: the simulation will show that nothing changes.",
     "scope": "Invoice matching", "unchanged": "Everything: the configuration does not control this behaviour.", "not_controlled": True},
)


def _scope_words(scope) -> str:
    if not isinstance(scope, dict) or not scope:
        return "not stated"
    return ", ".join(f"{str(k).replace('_', ' ')}: {v}" for k, v in scope.items())


def current_sources(store, tenant: str, ws: str, revision: str | None) -> dict:
    """{setting key: [what each analysed source currently says]} from the stored candidates of the CURRENT revision (server-side, no model).
    A setting is matched by the key path of a candidate's evidence locator, so a value is never guessed from wording."""
    if not revision:
        return {}
    with store._tx() as cur:
        cur.execute("SELECT DISTINCT payload->>'artifact_id' FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'", (tenant, ws))
        art_ids = sorted(r[0] for r in cur.fetchall() if r[0])
        cur.execute("SELECT source_revision_id, artifact_id, logical_path FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws))
        paths = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        cur.execute("""SELECT target FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s AND action='confirm'
            AND superseded_by IS NULL AND revoked_by IS NULL""", (tenant, ws))
        confirmed = {r[0] for r in cur.fetchall()}
    out: dict = {}
    for aid in art_ids:
        for c in store.knowledge_draft(tenant, ws, aid).get("candidates", []):
            if not isinstance(c, dict) or c.get("source_revision_id") != revision:
                continue
            for e in c.get("evidence") or []:
                loc = e.get("locator") if isinstance(e, dict) else None
                if isinstance(loc, str) and loc.strip().startswith("{"):
                    try:
                        loc = json.loads(loc)
                    except ValueError:
                        loc = None
                key = loc.get("key_path") if isinstance(loc, dict) else None
                if key:
                    out.setdefault(key, []).append({
                        "value": c.get("value"), "unit": c.get("unit"), "scope": _scope_words(c.get("scope")),
                        "file": _basename(paths.get((revision, c.get("artifact_revision_id")))),
                        "state": "confirmed" if c.get("candidate_id") in confirmed else "pending"})
                    break
    return out


def _typed_scope_mapping(store, tenant: str, ws: str, revision: str | None) -> dict:
    """Server-side only (never shown to the model): the files of the current source revision, to find the files the person NAMED in their own words."""
    from infra.worker.typed_scope import known_files
    try:
        arts = store.source_revision_view(tenant, ws, revision).get("artifacts", []) if revision else []
    except Exception:                                         # an unreadable view must not block a reply; no file will be listed
        arts = []
    return {"files_known": known_files(arts), "revision": revision}


def _attach_typed(purpose: str, fields: dict, typed: str, mapping: dict) -> dict:
    """The person's VERBATIM words travel with a reply/decision next to the AI wording: exact text, the files they named (with the source revision they exist in)
    and where the text came from. The server sets these; a client or a model can never supply them."""
    if purpose not in ("gap_reply", "question_reply"):
        return fields
    from infra.worker.typed_scope import files_in_text
    fields = dict(fields)
    fields["typed_text"] = str(typed or "").strip()[:2000]
    fields["files"] = files_in_text(fields["typed_text"], mapping.get("files_known") or [])
    fields["source_revision_id"] = mapping.get("revision")
    fields["provenance"] = "typed by the person; AI wording is supplementary"
    return fields


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().casefold()


def _basename(p) -> str:
    return str(p or "").replace("\\", "/").split("/")[-1]


# ------------------------------------------------------------------------------------------------ start / context
class PurposeError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


def _queue_item(store, tenant: str, ws: str, qid: str):
    """One question of the work queue, WITHOUT evidence quotes (they never go to the model)."""
    for it in store.hitl_queue(tenant, ws, redact_evidence=True).get("items", []):
        if it.get("question_id") == qid:
            return it
    return None


def validate_start(store, tenant: str, ws: str, purpose: str, context_in) -> dict:
    """Check what a conversation of this purpose is about and return the context to persist (ids only, never text)."""
    context_in = context_in if isinstance(context_in, dict) else {}
    if purpose in ("knowledge_qa", "whatif"):
        return {}
    if purpose == "question_reply":
        qid = context_in.get("question_id")
        if not isinstance(qid, str) or not qid:
            raise PurposeError("question_id required", 400)
        if _queue_item(store, tenant, ws, qid) is None:
            raise PurposeError("question not found", 404)
        return {"question_id": qid}
    draft_id = context_in.get("draft_job_id")
    if not isinstance(draft_id, str) or not draft_id:
        raise PurposeError("draft_job_id required", 400)
    try:
        rec = store.job_record(tenant, ws, draft_id)
    except Exception as exc:                                    # unknown / foreign job: nothing is revealed
        raise PurposeError("draft not found", 404) from exc
    if rec.get("kind") != "change.draft" or rec.get("execution_status") != "COMPLETED":
        raise PurposeError("the draft is not finished", 409)
    if purpose == "draft_feedback":
        return {"draft_job_id": draft_id}
    key = context_in.get("gap_key")
    from infra.v2_gaps import build_gaps
    gaps = build_gaps(store, tenant, ws, draft_id) or {}
    gap = next((g for g in gaps.get("gaps", []) if g["key"] == key), None)
    if gap is None:
        raise PurposeError("open point not found", 404)
    return {"draft_job_id": draft_id, "gap_key": key}


def build_context(store, tenant: str, ws: str, purpose: str, context_ref: dict | None, revision: str | None, lim: dict) -> tuple[dict, dict]:
    """(context shown to the model, server-side mapping from the codes in it to real things). Ids never reach the model."""
    n, clip = lim["max_context_items"], lim["max_item_chars"]
    if purpose == "knowledge_qa":
        files, mapping = [], {}
        if revision:
            view = store.source_revision_view(tenant, ws, revision)
            for i, a in enumerate(view.get("artifacts", [])[:n], 1):
                code = f"f{i}"
                files.append({"file": code, "name": _clip(_basename(a["logical_path"]), clip), "kind": a.get("media_type")})
                mapping[code] = {"artifact_id": a["artifact_id"], "name": _basename(a["logical_path"])}
        return {"files": files}, {"files": mapping}
    if purpose == "whatif":
        rules, mapping = [], {}
        seen = current_sources(store, tenant, ws, revision)           # what the sources say now; shown to the person, never chosen by the model
        for i, r in enumerate(WHATIF_RULES, 1):
            rules.append({"rule": f"r{i}", "label": r["label"], "note": r["note"]})
            mapping[f"r{i}"] = {**r, "current": seen.get(r["key"], [])}
        return {"rules": rules}, {"rules": mapping}
    ctx = context_ref or {}
    if purpose == "question_reply":
        it = _queue_item(store, tenant, ws, ctx.get("question_id") or "")
        if it is None:
            raise PurposeError("question not found", 404)
        claims, mapping = [], {}
        for i, c in enumerate((it.get("candidates") or [])[:n], 1):
            code = f"c{i}"
            val = c.get("value")
            claims.append({"claim": code, "rule": _clip(f"{str(c.get('predicate') or '').replace('_', ' ')} = {val}{' ' + str(c.get('unit')) if c.get('unit') else ''}", clip),
                           "applies_to": _clip(_scope_words(c.get("scope")), clip)})
            mapping[code] = c.get("candidate_id")
        return ({"question": _clip(str(it.get("prompt") or ""), 400), "claims": claims},
                {"question": {"question_id": it["question_id"], "version": it.get("version")}, "claims": mapping, **_typed_scope_mapping(store, tenant, ws, revision)})
    draft_id = ctx.get("draft_job_id")
    rec = store.job_record(tenant, ws, draft_id)
    result = rec.get("execution_result") or {}
    if purpose == "gap_reply":
        from infra.v2_gaps import build_gaps
        gap = next((g for g in (build_gaps(store, tenant, ws, draft_id) or {}).get("gaps", []) if g["key"] == ctx.get("gap_key")), None)
        if gap is None:
            raise PurposeError("open point not found", 404)
        return ({"open_point": _clip(gap["text"], 400), "kind": gap.get("kind")},
                {"gap": {"gap_id": gap["gap_id"], "key": gap["key"], "draft_job_id": draft_id}, **_typed_scope_mapping(store, tenant, ws, revision)})
    items, mapping = [], {}
    for i, x in enumerate((result.get("items") or [])[:n], 1):
        code = f"i{i}"
        items.append({"item": code, "file": _clip(_basename(x.get("logical_path")), clip), "status": x.get("status"),
                      "draft_says": _clip(str(x.get("rationale") or x.get("reason") or ""), clip)})
        mapping[code] = {"ref": x.get("ref"), "name": _basename(x.get("logical_path")), "status": x.get("status")}
    return {"items": items}, {"items": mapping, "draft_job_id": draft_id}


def build_prompt(purpose: str, context: dict, conversation: str) -> tuple[str, dict, str]:
    cfg, sha = load_config()
    p = cfg["purposes"][purpose]["prompt"].replace("{common}", cfg["common"]).replace("{context}", _canon_json(context)) \
        .replace("{conversation}", conversation)
    return p, _schema(), sha


def _schema() -> dict:
    return {"type": "object", "required": ["status", "message", "questions", "fields", "assumptions"], "additionalProperties": False,
            "properties": {"status": {"type": "string", "enum": list(STATUSES)}, "message": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}},
                           "fields": {"type": "object"}, "assumptions": {"type": "array", "items": {"type": "string"}},
                           "out_of_scope": {"type": "array", "items": {"type": "string"}}}}


# ------------------------------------------------------------------------------------------------ validation
def normalise_fields(purpose: str, fields: dict, mapping: dict, lim: dict, typed: str) -> tuple[dict, list]:
    """Canonical, verified fields for a purpose. `typed` = everything the person typed (words, edits): a value must come from it."""
    problems: list = []
    f = fields if isinstance(fields, dict) else {}
    if purpose == "knowledge_qa":
        q = _clip(str(f.get("question") or "").strip(), lim["max_question_chars"])
        if chat_safety.unsafe_output(q):
            q = ""
            problems.append("The question contained a link, command or markup and was removed; please write it again.")
        ids = [i for i in (f.get("artifact_ids") or []) if isinstance(i, str)]
        known = {m["artifact_id"]: m["name"] for m in mapping.get("files", {}).values()}
        ids = [i for i in dict.fromkeys(ids) if i in known][:12]
        if not q:
            problems.append("Still needed: the question.")
        if not ids:
            problems.append("Still needed: which files to search.")
        return {"question": q, "artifact_ids": ids, "files": [known[i] for i in ids]}, problems
    if purpose == "gap_reply":
        ans = _clip(str(f.get("answer") or "").strip(), lim["max_answer_chars"])
        kind = f.get("kind") if f.get("kind") in ("comment", "decision") else "comment"
        wf = f.get("wish_or_fact") if f.get("wish_or_fact") in ("fact", "wish") else None
        why = _clip(str(f.get("rationale") or "").strip(), 300) or None
        if chat_safety.unsafe_output(ans) or chat_safety.unsafe_output(why or ""):
            ans, why = "", None
            problems.append("The reply contained a link, command or markup and was removed; please write it again.")
        if not ans:
            problems.append("Still needed: your answer to the open point.")
        if wf is None and ans:
            problems.append("Still needed: is this how things work today (a fact) or how you want them to work (a wish)?")
        g = mapping.get("gap", {})
        return {"answer": ans, "kind": kind, "wish_or_fact": wf, "rationale": why, "gap_key": g.get("key"), "gap_id": g.get("gap_id"),
                "draft_job_id": g.get("draft_job_id")}, problems
    if purpose == "question_reply":
        ans = _clip(str(f.get("answer") or "").strip(), lim["max_answer_chars"])
        kind = f.get("kind") if f.get("kind") in ("comment", "decision") else "comment"
        wf = f.get("wish_or_fact") if f.get("wish_or_fact") in ("fact", "wish") else None
        why = _clip(str(f.get("rationale") or "").strip(), 300) or None
        claims = mapping.get("claims", {})
        cid = f.get("claim_id") if f.get("claim_id") in claims.values() else claims.get(f.get("claim"))
        if chat_safety.unsafe_output(ans) or chat_safety.unsafe_output(why or ""):
            ans, why = "", None
            problems.append("The reply contained a link, command or markup and was removed; please write it again.")
        if not ans:
            problems.append("Still needed: your answer to the question.")
        if wf is None and ans:
            problems.append("Still needed: is this how things work today (a fact) or how you want them to work (a wish)?")
        forced = False
        if kind == "decision" and wf == "wish":
            kind, forced = "comment", True            # a wish is never recorded as a confirmed fact
        if kind == "decision" and not cid:
            problems.append("Still needed: which rule you are confirming.")
        q = mapping.get("question", {})
        return {"answer": ans, "kind": kind, "wish_or_fact": wf, "rationale": why, "claim_id": cid if kind == "decision" else None,
                "question_id": q.get("question_id"), "asks_source_change": bool(f.get("asks_source_change")) or wf == "wish", "forced_comment": forced}, problems
    if purpose == "draft_feedback":
        items = mapping.get("items", {})
        out = []
        for r in (f.get("reactions") or [])[: lim["max_reactions"]]:
            if not isinstance(r, dict):
                continue
            ref = r.get("ref") or (items.get(r.get("item")) or {}).get("ref")
            known = next((v for v in items.values() if v["ref"] == ref), None)
            if known is None or r.get("reaction") not in REACTIONS:
                continue
            note = _clip(str(r.get("note") or "").strip(), 300)
            quote = _clip(str(r.get("quote") or "").strip(), 300)
            if chat_safety.unsafe_output(note) or chat_safety.unsafe_output(quote):
                problems.append("Part of the feedback contained a link, command or markup and was removed.")
                continue
            traced = bool(quote) and _norm(quote) in _norm(typed)
            out.append({"ref": ref, "item": f"{known['name']} ({known['status']})", "reaction": r["reaction"], "note": note,
                        "quote": quote if traced else "", "traced": traced})
        clar = _clip(str(f.get("clarification") or "").strip(), 300) or None
        if chat_safety.unsafe_output(clar or ""):
            clar = None
            problems.append("The clarification contained a link, command or markup and was removed.")
        if not out and not clar:
            problems.append("Still needed: which item of the draft your feedback is about, and what you want done.")
        return {"reactions": out, "clarification": clar, "draft_job_id": mapping.get("draft_job_id")}, problems
    # whatif
    rule = None
    key = f.get("rule_key") or f.get("rule")
    for code, r in mapping.get("rules", {}).items():
        if key in (code, r["key"]):
            rule = r
    val = f.get("value")
    nums = {v for v, _ in numbers_with_quotes(typed)}
    try:
        num = float(val) if val is not None and not isinstance(val, bool) else None
    except (TypeError, ValueError):
        num = None
    if num is not None and num not in nums:
        problems.append("The value is not a number you wrote; please state it.")
        num = None
    if num is not None and (num < 0 or (rule and rule["integer"] and num != int(num))):
        problems.append("The value must be a whole number that is not negative.")
        num = None
    if rule is None:
        problems.append("Still needed: which rule to try.")
    if num is None and not any("number you wrote" in p or "whole number" in p for p in problems):
        problems.append("Still needed: the value to try.")
    cur_vals = (rule or {}).get("current") or []
    return {"rule_key": rule["key"] if rule else None, "case": rule["case"] if rule else None, "label": rule["label"] if rule else None,
            "value": int(num) if num is not None else None, "note": rule["note"] if rule else "",
            "scope": rule.get("scope") if rule else None, "unchanged": rule.get("unchanged") if rule else None,
            "current": cur_vals, "sources_disagree": len({str(c.get("value")) for c in cur_vals}) > 1,
            "not_controlled": bool(rule and rule.get("not_controlled")),
            "simulated_by": "The Harbor program run offline on its pinned public copy (no AI computes the result)" if rule else None}, problems


def check_shape(obj: dict) -> None:
    from infra.worker.execution_types import NonRetryableExecutionError

    def bad(why):
        raise NonRetryableExecutionError("schema_violation", why)
    keys = {"status", "message", "questions", "fields", "assumptions"}
    if not isinstance(obj, dict) or not keys <= set(obj) <= keys | {"out_of_scope", "changes"}:
        bad("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["message"], str):
        bad("status/message")
    for k in ("questions", "assumptions"):
        if not isinstance(obj[k], list) or any(not isinstance(x, str) for x in obj[k]):
            bad(k)
    if not isinstance(obj["fields"], dict):
        bad("fields")


def resolve(purpose: str, obj: dict, user_text: str, mapping: dict, limits: dict, max_questions: int = 4) -> dict:
    """Server-side verification of a model preview. Pure. The model's codes are mapped to real things; anything it invented is dropped."""
    f_in = dict(obj["fields"])
    if purpose == "knowledge_qa":
        files = mapping.get("files", {})
        f_in["artifact_ids"] = [files[c]["artifact_id"] for c in (f_in.get("files") or []) if isinstance(c, str) and c in files]
    if purpose == "whatif":
        f_in["rule_key"] = f_in.get("rule")
    if purpose == "draft_feedback":
        f_in["reactions"] = [{**r, "ref": (mapping.get("items", {}).get(r.get("item")) or {}).get("ref")} for r in (f_in.get("reactions") or []) if isinstance(r, dict)]
    fields, problems = normalise_fields(purpose, f_in, mapping, limits, user_text)
    fields = _attach_typed(purpose, fields, user_text, mapping)
    notes: list = []
    status = obj["status"]
    message = obj["message"].strip()
    questions = [_clip(q.strip(), 200) for q in obj["questions"] if q.strip()][:max_questions]
    if chat_safety.unsafe_output(message) or any(chat_safety.unsafe_output(q) for q in questions):
        message, questions, status = "I could not produce a safe reply. Please say it again.", [], "CLARIFY"
        notes.append({"code": "MODEL_OUTPUT_UNSAFE", "business": "Part of the AI reply contained a link, command or markup and was removed."})
    if chat_safety.claims_completion(message):
        message = "I understood your message as shown below. " + chat_safety.NOT_DONE_NOTE
        notes.append({"code": "MODEL_CLAIMED_COMPLETION", "business": "The AI wrote as if something had been done. Nothing has: this is only what was understood, and the sentence was replaced."})
    assumptions = [_clip(a, 200) for a in obj["assumptions"][:6]]
    sources: dict = {}
    if purpose in ("gap_reply", "question_reply"):
        quote = _clip(str(obj["fields"].get("quote") or "").strip(), 300)
        sources["typed"] = {"from": "your words (verbatim)", "quote": fields.get("typed_text")}
        if quote and _norm(quote) in _norm(user_text):
            sources["answer"] = {"from": "your words", "quote": quote}
        elif fields["answer"]:
            assumptions.append("The answer is the AI's wording, not a quote of yours. Check that it says what you mean.")
            sources["answer"] = {"from": "AI wording", "quote": None}
    if purpose == "whatif" and fields.get("value") is not None:
        sources["value"] = {"from": "your words", "quote": next((q for v, q in numbers_with_quotes(user_text) if v == fields["value"]), None)}
    out_of_scope = [_clip(x, 200) for x in (obj.get("out_of_scope") or [])[:6] if isinstance(x, str)]
    if status == "READY" and problems:
        status = "CLARIFY"
    if status == "CLARIFY" and not (questions or problems):
        problems.append("Please add the missing detail.")
    if status == "UNSUPPORTED":
        problems = []
    flags = chat_safety.scan(user_text)
    notes += flags
    if purpose == "whatif" and fields.get("note"):
        assumptions.append(fields["note"])
    return {"status": status, "message": _clip(message, 400), "questions": questions + problems[: max(0, max_questions - len(questions))],
            "fields": fields, "assumptions": list(dict.fromkeys(assumptions))[:8], "problems": problems, "out_of_scope": out_of_scope,
            "changes": [], "sources": sources, "purpose": purpose,
            "safety": [{"code": n["code"], "business": n["business"]} for n in notes], "injection_flagged": bool(flags)}


# ------------------------------------------------------------------------------------------------ confirm
def understanding_from_fields(purpose: str, fields: dict, mapping: dict, lim: dict, typed: str) -> dict:
    """What an edit by the person becomes: the same verified shape, source "person"."""
    f_in = dict(fields)
    if purpose == "knowledge_qa":
        f_in["artifact_ids"] = [i for i in (f_in.get("artifact_ids") or [])]
    if purpose == "draft_feedback":
        f_in["reactions"] = [dict(r, quote=r.get("quote") or r.get("note") or "") for r in (f_in.get("reactions") or []) if isinstance(r, dict)]
    out, problems = normalise_fields(purpose, f_in, mapping, lim, typed)
    out = _attach_typed(purpose, out, typed, mapping)
    ok = not problems
    return {"status": "READY" if ok else "CLARIFY", "message": "Your edited details look complete. Check them, then confirm." if ok else "Some details are still missing or unclear.",
            "questions": problems, "fields": out, "assumptions": [], "problems": problems, "out_of_scope": [], "changes": [], "sources": {},
            "purpose": purpose, "safety": [], "source": "person"}


def outcome_for(purpose: str, u: dict, perms: set[str]) -> dict:
    """The structured thing the person agreed to. NOTHING has been executed: the page offers the existing guarded action next."""
    f = u["fields"]
    if purpose == "knowledge_qa":
        return {"action": "ask", "question": f["question"], "artifact_ids": f["artifact_ids"], "files": f["files"],
                "needs": "A second person must allow the AI run."}
    if purpose == "gap_reply":
        kind = f["kind"]
        if kind == "decision" and "knowledge.confirm" not in perms:
            raise PurposeError("a decision needs a person with permission to confirm; save it as a comment instead", 409)
        if kind == "comment" and "knowledge.answer" not in perms:
            raise PurposeError("your account may not write comments on open points", 403)
        return {"action": "gap_reply", "draft_job_id": f["draft_job_id"], "gap_key": f["gap_key"], "gap_id": f["gap_id"], "kind": kind,
                "answer": f["answer"], "rationale": f["rationale"], "wish_or_fact": f["wish_or_fact"],
                "typed_text": f.get("typed_text"), "files": f.get("files") or [], "source_revision_id": f.get("source_revision_id"), "ai_wording": f["answer"],
                "needs": "A decision is recorded with your name and makes this draft out of date." if kind == "decision" else "A comment is not authoritative."}
    if purpose == "question_reply":
        kind = f["kind"]
        if kind == "decision" and "knowledge.confirm" not in perms:
            raise PurposeError("a decision needs a person with permission to confirm; save it as a comment instead", 409)
        if kind == "comment" and "knowledge.answer" not in perms:
            raise PurposeError("your account may not write answers to questions", 403)
        return {"action": "question_reply", "question_id": f["question_id"], "claim_id": f["claim_id"], "kind": kind, "answer": f["answer"],
                "rationale": f["rationale"], "wish_or_fact": f["wish_or_fact"], "handoff_change": bool(f.get("asks_source_change")),
                "needs": ("A decision is recorded with your name. It confirms knowledge; it does not change any source file." if kind == "decision"
                          else "An answer is a comment: it is not knowledge until a person with authority confirms a rule.")
                + (" To change the source itself, start a change request." if f.get("asks_source_change") else "")}
    if purpose == "draft_feedback":
        return {"action": "feedback", "draft_job_id": f["draft_job_id"], "reactions": f["reactions"], "clarification": f["clarification"],
                "needs": "Recorded as comments on the open points of the draft; a new draft needs a second person to allow it."}
    return {"action": "whatif", "case": f["case"], "path": f["rule_key"], "value": f["value"], "label": f["label"],
            "needs": "Pressing Run creates a simulation job. Nothing is changed."}
