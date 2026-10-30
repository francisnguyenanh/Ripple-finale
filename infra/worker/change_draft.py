"""Job kind "change.draft", executor_id "change.draft.v1" -- GenAI grounded change draft.

One provider call per attempt. The SERVER supplies authorized evidence (stored observations that carry the old/new
number or that an extraction candidate cites, with the candidate annotations as extracted) and the action limits; the
MODEL analyses the request against it: which evidence items are affected and how, a summary, questions. The model never
writes a path, a current value or a proposed value:
  * current value  = the stored observation of the evidence ref (current pinned revision);
  * proposed value = the confirmed intent;
  * a typed `proposal_edit` is materialized by the server, only for a YAML scalar whose stored value equals the
    intent's confirmed old value; current == new is forced to NO_OP, anything else is AMBIGUITY; other kinds are MANUAL.
The model can only downgrade (never raise) what the server can verify. Every evidence ref the model does not classify is
reported `unaddressed`; the draft is then PARTIAL, never complete. Nothing here applies anything (draft only)."""
from __future__ import annotations

from infra.worker.provider_response import attach_after_answer, require_normal_finish

import hashlib
import json
import pathlib
import re

from core.change_intelligence.v2.brief_form import normalise_brief_form
from infra.adapters.v2_postgres import IntentNotFound, TenantScopeError
from infra.worker import extraction
from infra.worker.budget_config import model_for_role
from infra.worker.change_brief import (
    ChangeBriefExecutor, _item, _kind, _label, _lockey, _num, _numbers_in, _revalidate, _tok,
)
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget,
    ProviderExecutionError, ProviderInvalidResponseError, ProviderRateLimitError, ProviderTimeoutError,
    is_timeout_exception,
)
from infra.worker.gap_answer import _model_object, load_frozen_config as _gap_config
from infra.worker.live_analysis import generate_and_close, _client_for_model, _field, _token_count

THINKING_LEVEL = "LOW"
EXECUTOR_ID = "change.draft.v1"
ROLE = "change.draft"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "change_draft.json"
CLAIM = ("AI-assisted draft: a model analysed the request against stored evidence; the server verified every citation "
         "and derived current/proposed values from stored state and the confirmed intent. Draft only; it applies "
         "nothing and is not complete while anything is unaddressed, manual or ambiguous.")
MAX_REFS = 60
_MODEL_KEYS = {"summary", "interpretation", "items", "questions_for_human", "scope_examined"}
_ACTIONS = ("edit", "manual", "no_op", "exclude", "ambiguity")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    gap_raw = _gap_config()[1]
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n") + gap_raw.encode()).hexdigest()


def validate_payload(payload) -> str | None:
    """Shared by the route and the executor."""
    if not isinstance(payload, dict):
        return "payload_not_an_object"
    if not isinstance(payload.get("intent_id"), str) or not payload["intent_id"]:
        return "intent_id_required"
    ids = payload.get("extraction_job_ids")
    if ids is not None and (not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids)):
        return "extraction_job_ids_invalid"          # optional: omitted = the server collects them for the revision
    rv = payload.get("revises_draft_job_id")
    if rv is not None and (not isinstance(rv, str) or not rv):
        return "revises_draft_job_id_invalid"
    for key in ("finding_job_ids", "scope_artifact_ids"):
        v = payload.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(i, str) and i for i in v)):
            return f"{key}_invalid"
    return None


REQUIRED_LOCATION = "global"


def _assert_global_endpoint() -> None:
    """The draft role runs on the Vertex GLOBAL endpoint only (the model is served there, the price snapshot is the
    global one). A routed account that is not `global`, or a non-Vertex backend, fails before any client is built;
    there is no silent fallback to another region or backend."""
    from ai_module.accounts import get_account
    from ai_module.router import AIRouter
    route = AIRouter().routing["text_generation"]
    if route.get("backend") != "vertex":
        raise NonRetryableExecutionError("endpoint_not_vertex", f"backend={route.get('backend')!r}")
    region = get_account(route["account_id"]).get("default_region")
    if region != REQUIRED_LOCATION:
        raise NonRetryableExecutionError("endpoint_not_global", f"default_region={region!r}")


def _assert_vertex_backend() -> None:
    """R21: a role allowed a Vertex multi-region location (`budget_config.LOCATION_ROLES`) still requires the Vertex backend of the routed account; there is no
    fallback to another backend."""
    from ai_module.router import AIRouter
    route = AIRouter().routing["text_generation"]
    if route.get("backend") != "vertex":
        raise NonRetryableExecutionError("endpoint_not_vertex", f"backend={route.get('backend')!r}")


def _clip(text, n):
    return text if not isinstance(text, str) or len(text) <= n else text[:n] + "..."


def _canon_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)


def _check_shape(obj: dict) -> None:
    def bad(why):
        raise NonRetryableExecutionError("schema_violation", why)
    if set(obj) != _MODEL_KEYS:
        bad("keys")
    if not isinstance(obj["summary"], str) or not obj["summary"].strip():
        bad("summary")
    if not isinstance(obj["interpretation"], dict):
        bad("interpretation")
    if not isinstance(obj["items"], list):
        bad("items")
    for it in obj["items"]:
        if (not isinstance(it, dict) or set(it) != {"ref", "action", "rationale", "quote"}
                or not all(isinstance(v, str) for v in it.values()) or it["action"] not in _ACTIONS):
            bad("item")
    for key in ("questions_for_human", "scope_examined"):
        if not isinstance(obj[key], list) or any(not isinstance(x, str) for x in obj[key]):
            bad(key)


_OPEN_STATUSES = ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED")
CARRIED_MODEL_EXCLUSION = "carried_unresolved_model_exclusion_is_not_a_resolution"
CARRIED_MISSING = "carried_unresolved_missing_from_revision"


def open_over_chain(chain: list) -> dict:
    """What is still open after a CHAIN of revises (oldest draft first): `chain` = [(draft_job_id, result)]. An item stays open until a later draft resolves it with a
    basis the server can check (value now equals the new value / a proposed edit); a model "exclude", an omission or "unaddressed" is NOT a basis, so something
    left open in the first draft is still open after any number of revises. Each entry remembers the draft where it was FIRST left open."""
    ident = lambda i: (i.get("artifact_id"), _lockey(i.get("locator")))   # noqa: E731
    items: dict = {}
    unaddr: dict = {}
    for draft_id, res in chain:
        seen = set()
        for x in res.get("items") or []:
            k = ident(x)
            seen.add(k)
            if x.get("status") in _OPEN_STATUSES:
                prior = items.get(k)
                items[k] = {**json.loads(json.dumps(x)), "_first_open_in": (prior or {}).get("_first_open_in") or draft_id}
            elif x.get("status") == "NO_OP":              # the stored value already equals the new value: a fact the server read. A PROPOSED edit is only a proposal until applied and verified
                items.pop(k, None)
        for u in res.get("unaddressed") or []:
            k = (u.get("logical_path"), u.get("location"))
            unaddr[k] = {**u, "_first_open_in": (unaddr.get(k) or {}).get("_first_open_in") or draft_id}
        for k, x in list(unaddr.items()):
            it = next((y for y in res.get("items") or [] if (y.get("logical_path"), _label(y.get("locator"))) == k), None)
            if it is not None and it.get("status") == "NO_OP":
                unaddr.pop(k, None)
    return {"items": list(items.values()), "unaddressed": list(unaddr.values())}


def _outside_map(used: list) -> dict:
    """artifact id -> the confirmed decision (who, which, why) that named the file as NOT changed by this request."""
    out: dict = {}
    for u in used or []:
        for f in u.get("files") or []:
            if f.get("role") == "not_changed":
                out.setdefault(f.get("artifact_id"), {"decision_id": u["decision_id"], "by": u["actor_id"], "epoch": u["epoch"],
                                                      "reason": _clip(u.get("rationale") or u.get("answer") or "", 300)})
    return out


def apply_dispositions(base: dict, used: list) -> None:
    """What KIND of open each open entry is, decided by the server from facts, never by the model's silence or exclusion:
    `outside_this_request` (a confirmed decision of a named person says the file is not changed by this request; it is NOT resolved),
    `potentially_affected` (the same rule states a different value; set when the item was classified), otherwise `unresolved`."""
    outside = _outside_map(used)
    for x in list(base["items"]) + list(base["unaddressed"]):
        is_open = x.get("status") in _OPEN_STATUSES or "status" not in x            # unaddressed entries have no status
        if not is_open:
            continue
        basis = outside.get(x.get("artifact_id"))
        if basis:
            x["disposition"], x["disposition_basis"] = "outside_this_request", basis
        else:
            x.setdefault("disposition", "unresolved")


def carry_unresolved(base: dict, prev: dict, used: list, prev_id: str) -> list:
    """A REVISED draft never loses what the draft before it left open. The server keeps it; the model is not asked to repeat it and its silence or
    its own "exclude" is not a resolution. An item is identified by what it really is (artifact + location, never the draft-local code E0xx).
    It stays open (`AMBIGUITY`/`MANUAL_CHANGE`/`UNSUPPORTED`, or `unaddressed`) unless the revision resolves it with a basis the server can check
    (the stored value now equals the new value; a merely PROPOSED edit is `proposed_remediation`, still open until applied and verified). A person's confirmed decision that names the file as "not changed in this request"
    does not resolve it either: it only labels it `outside_this_request` (decision id and person shown). Returns the carried entries (also kept in `base`)."""
    carried: list = []
    ident = lambda i: (i.get("artifact_id"), _lockey(i.get("locator")))   # noqa: E731
    outside = {}
    for u in used or []:
        for f in u.get("files") or []:
            if f.get("role") == "not_changed":
                outside.setdefault(f.get("artifact_id"), {"decision_id": u["decision_id"], "by": u["actor_id"], "epoch": u["epoch"],
                                                          "reason": _clip(u.get("rationale") or u.get("answer") or "", 300)})

    def label(item, from_):
        basis = outside.get(item.get("artifact_id"))
        item["carried_from"] = from_
        item["disposition"] = "outside_this_request" if basis else (item.get("disposition") or "unresolved")
        if basis:
            item["disposition_basis"] = basis
        carried.append({"ref": item.get("ref"), "logical_path": item.get("logical_path"), "artifact_id": item.get("artifact_id"), "status": item.get("status"),
                        "disposition": item["disposition"], "carried_from": from_})

    new_items = {ident(x): x for x in base["items"]}
    by_loc = {(x.get("logical_path"), _label(x.get("locator"))): x for x in base["items"]}
    new_un = {(u.get("logical_path"), u.get("location")): u for u in base["unaddressed"]}
    for pi in prev.get("items") or []:
        if pi.get("status") not in _OPEN_STATUSES:
            continue
        from_ = {"draft_job_id": pi.get("_first_open_in") or prev_id, "ref": pi.get("ref"), "status": pi.get("status"), "reason": pi.get("reason")}
        n = new_items.get(ident(pi))
        if n is None:
            listed = new_un.get((pi.get("logical_path"), _label(pi.get("locator"))))
            if listed is not None:                              # the new draft did not classify it: it is listed as unaddressed, with where it came from
                label(listed, from_)
                continue
            copy = {k: v for k, v in json.loads(json.dumps(pi)).items() if k != "_first_open_in"}
            copy.update(reason=CARRIED_MISSING)
            base["items"].append(copy)
            label(copy, from_)
        elif n.get("status") == "EXCLUDED":
            from_["model_now"] = {"action": n.get("model_action"), "reason": n.get("reason"), "rationale": n.get("rationale")}
            n.update(status=pi["status"], reason=CARRIED_MODEL_EXCLUSION)
            label(n, from_)
        elif n.get("status") == "PROPOSED":
            label(n, from_)                                     # a remediation is PROPOSED for something that was open: it stays open until the edit is applied and verified
            n["disposition"] = "proposed_remediation"
            carried[-1]["disposition"] = "proposed_remediation"
        elif n.get("carried_from") is None and n.get("status") in _OPEN_STATUSES:
            label(n, from_)                                     # still open in the new draft: keep the lineage visible
    for pu in prev.get("unaddressed") or []:
        key = (pu.get("logical_path"), pu.get("location"))
        from_ = {"draft_job_id": pu.get("_first_open_in") or prev_id, "ref": pu.get("ref"), "status": "UNADDRESSED", "reason": "not_classified_by_the_model"}
        it = by_loc.get(key)
        if it is not None and it.get("status") == "EXCLUDED":
            from_["model_now"] = {"action": it.get("model_action"), "reason": it.get("reason"), "rationale": it.get("rationale")}
            it.update(status="AMBIGUITY", reason=CARRIED_MODEL_EXCLUSION)
            label(it, from_)
        elif it is None and key in new_un:
            label(new_un[key], from_)                           # still unaddressed: keep the lineage visible
        elif it is None:
            copy = {k: v for k, v in pu.items() if k != "_first_open_in"}
            base["unaddressed"].append(copy)
            label(copy, from_)
    base["carried_unresolved"] = carried
    return carried


class ChangeDraftExecutor:
    external = True
    executor_id = EXECUTOR_ID

    # ------------------------------------------------------------------ prepare
    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        payload = ctx.job["payload"]
        err = validate_payload(payload)
        if err:
            raise NonRetryableExecutionError(err)
        frozen, sha = load_config()
        gap_frozen = _gap_config()[0]
        patterns = [re.compile(p, re.I) for p in gap_frozen["injection_patterns"]]
        store, tenant, ws = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"]
        rev = ctx.job.get("pinned_source_revision_id")
        rc = ctx.request_context
        rc.update(draft_config_sha=sha, draft_patterns=patterns, draft_pre=None)
        try:
            intent = store.get_intent(tenant, ws, payload["intent_id"])
        except IntentNotFound as exc:
            raise NonRetryableExecutionError("intent_not_found") from exc
        base = self._base(ctx, intent, sha)
        rc["draft_base"] = base
        if intent["status"] != "CONFIRMED":
            return self._pre(ctx, "INTENT_NOT_CONFIRMED", ["intent_not_confirmed"])
        norm = normalise_brief_form(intent.get("structured_form") or {"old_value": None})
        f = norm["fields"]
        if f is None or intent.get("value") != f["new_value"]:
            return self._pre(ctx, "INTENT_NOT_UNDERSTOOD", ["intent_form_not_understood"] + norm["problems"])
        base["intent"]["fields"] = f
        rc["draft_fields"] = f
        # parts of the request the product stores but does NOT use anywhere downstream: said as "not covered", never left to look applied
        if intent.get("exclusions"):
            base["not_covered"].append({"artifact_id": None, "logical_path": None, "reason": "recorded only, NOT applied: the exclusions you stated ("
                                        + _clip("; ".join(str(x) if isinstance(x, str) else json.dumps(x, default=str) for x in intent["exclusions"]), 200)
                                        + ") are not used by the draft, the simulation or the apply; a person has to handle them"})
        if intent.get("effective_time"):
            base["not_covered"].append({"artifact_id": None, "logical_path": None, "reason": "recorded only, NOT applied: the effective date you stated ("
                                        + _clip(intent["effective_time"] if isinstance(intent["effective_time"], str) else json.dumps(intent["effective_time"], default=str), 80)
                                        + ") is not used by the draft, the simulation or the apply; the new value takes effect when the change is applied"})
        if not rev:
            return self._pre(ctx, "NO_EVIDENCE", ["no_source_revision"])
        try:
            view = store.source_revision_view(tenant, ws, rev)
            obs_all = store.source_observations(tenant, ws, rev)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("source_artifact_unavailable") from exc
        arts = {a["artifact_id"]: a for a in view["artifacts"]}
        scope_ids = payload.get("scope_artifact_ids")
        if isinstance(scope_ids, list):
            if any(i not in arts for i in scope_ids):
                raise NonRetryableExecutionError("source_artifact_unavailable", "scope outside the pinned revision")
            arts = {i: arts[i] for i in scope_ids}
        ext_ids = payload.get("extraction_job_ids")
        if ext_ids is None:
            from infra.v2_current_extraction import valid_extraction_jobs
            ext_ids = [j["job_id"] for j in valid_extraction_jobs(store, tenant, ws, rev, set(arts))]      # pinned to this revision, or the file is byte-identical since it was read
            base["extraction_jobs_collected"] = ext_ids
            if not ext_ids:
                return self._pre(ctx, "NO_EVIDENCE", ["no_completed_extraction_for_the_current_revision"])
        cands_by_obs = self._candidates(ctx, ext_ids, arts, base)
        pack, rendered = self._pack(arts, obs_all, cands_by_obs, f, patterns)
        base["evidence_refs"] = len(pack)
        if not pack:
            return self._pre(ctx, "NO_EVIDENCE", ["no_authorized_evidence_for_request"])
        if len(pack) > MAX_REFS:
            raise NonRetryableExecutionError("budget_exceeded", f"{len(pack)} evidence refs exceed {MAX_REFS}")
        find_ids = payload.get("finding_job_ids")
        if find_ids is None:                      # omitted: the server uses the completed finding jobs of this revision
            from infra.worker.verification import _completed_jobs
            find_ids = [j["job_id"] for j in _completed_jobs(store, tenant, ws, "finding")
                        if j["pinned_source_revision_id"] == rev]
            base["finding_jobs_collected"] = find_ids
        base["coverage"] = self._coverage_of_inputs(ctx, rev, arts, ext_ids, find_ids)
        findings = self._findings(ctx, {"finding_job_ids": find_ids}, base,
                                  {c["candidate_id"] for v in cands_by_obs.values() for c in v})
        req_obj = {"raw_request": _clip(intent.get("raw_request"), 2000),
                   "confirmed_intent": {k: f[k] for k in ("subject", "scope", "unit", "currency",
                                                           "old_value", "new_value", "applies_to")}}
        used = self._clarifications(ctx, intent["intent_id"], patterns, base)
        if used:
            # Only HUMAN-CONFIRMED answers (durable decisions, epoch <= this job's pin). Comments and model suggestions are
            # never read here. They are assumptions stated by a named person, not source evidence: citations still have to
            # come from the evidence list.
            req_obj["confirmed_clarifications"] = [
                {"question": u["question"], "confirmed_answer": u["answer"], "confirmed_by": u["actor_id"],
                 **({"files_named_by_the_person": [{"file": f["name"], "role": f["role"]} for f in u["files"]]} if u.get("files") else {})} for u in used]
            req_obj["analyst_note"] = ("confirmed_clarifications are assumptions decided by the named person for THIS request; "
                                       "treat them as given, never as source evidence, and cite only from the evidence list.")
        request_txt = _canon_json(req_obj)
        prompt = (frozen["prompt_template"].replace("{request}", request_txt)
                  .replace("{findings}", findings).replace("{evidence}", rendered))
        bound = extraction._input_token_bound(prompt, frozen["output_schema"])
        if bound > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError(
                "budget_exceeded", f"draft request exceeds input ceiling (bound {bound} > {config.max_input_tokens_per_call}; "
                                   f"{len(pack)} evidence refs)")
        base["pack_hash"] = hashlib.sha256(_canon_json(rendered).encode()).hexdigest()
        rc.update(draft_pack=pack, draft_arts=arts, draft_frozen=frozen)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    @staticmethod
    def _clarifications(ctx, intent_id, patterns, base) -> list:
        job = ctx.job
        try:
            rows = ctx.store.active_gap_clarifications(job["tenant_id"], job["workspace_id"], intent_id,
                                                       job.get("pinned_decision_epoch"))
        except Exception:                                    # a read problem must never become a silently different draft
            raise NonRetryableExecutionError("clarifications_unreadable")
        used = []
        for r in rows:
            sc = r["scope"] or {}
            answer, question = str(sc.get("answer") or "").strip(), str(sc.get("question") or "").strip()
            if not answer or any(p.search(answer) or p.search(question) for p in patterns):
                base["unsupported"].append({"kind": "clarification", "ref": r["decision_id"],
                                            "reason": "empty_or_flagged_as_instruction"})
                continue
            used.append({"decision_id": r["decision_id"], "epoch": r["epoch"], "actor_id": r["actor_id"],
                         "target": r["target"], "question": _clip(question, 300), "answer": _clip(answer, 600),
                         "rationale": _clip(r.get("rationale") or "", 300),
                         "files": [{"name": f.get("name"), "logical_path": f.get("logical_path"), "artifact_id": f.get("artifact_id"), "role": f.get("role")}
                                   for f in (sc.get("files") or []) if isinstance(f, dict)][:12],
                         "ai_wording": _clip(str(sc.get("ai_wording") or ""), 600) or None})
        base["clarifications_used"] = used
        prev_id = job["payload"].get("revises_draft_job_id")
        if prev_id:
            try:
                prev = (ctx.store.job_record(job["tenant_id"], job["workspace_id"], prev_id).get("execution_result") or {})
            except Exception:
                prev = {}
            before = {c.get("decision_id") for c in prev.get("clarifications_used") or []}
            base["revision"] = {"revises_draft_job_id": prev_id, "previous_clarification_ids": sorted(x for x in before if x),
                                "new_clarification_ids": [c["decision_id"] for c in used if c["decision_id"] not in before]}
        return used

    def _base(self, ctx, intent, sha) -> dict:
        job = ctx.job
        return {"executor_id": EXECUTOR_ID, "draft_version": "1.0.0", "claim": CLAIM, "draft_only": True,
                "applies_nothing": True, "provider_calls": 0, "config_sha256": sha,
                "live_call_provenance": "ripple_runtime",
                "pinned_input": {"pinned_source_revision_id": job.get("pinned_source_revision_id"),
                                 "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
                                 "pinned_decision_epoch": job.get("pinned_decision_epoch")},
                "intent": {"intent_id": intent["intent_id"], "status": intent["status"], "fields": None},
                "summary": None, "interpretation": None, "items": [], "edits": [], "unaddressed": [],
                "unsupported": [], "rejected": [], "questions_for_human": [], "open_items": [], "not_covered": [],
                "mismatches": [], "clarifications_used": [], "evidence_refs": 0, "extraction_jobs_collected": None, "finding_jobs_collected": None, "injection_flagged": False, "coverage": None}

    def _pre(self, ctx, outcome, reasons) -> ExecutionRequest:
        """No model call is justified: NEEDS_CONFIRMATION, zero provider calls."""
        base = ctx.request_context["draft_base"]
        base.update(status="NEEDS_CONFIRMATION", outcome=outcome, reasons=list(reasons))
        ctx.request_context["draft_pre"] = base
        return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0, external=True)

    @staticmethod
    def _candidates(ctx, ext_ids, arts, base) -> dict:
        """(artifact_id, locator key) -> extraction candidate annotations, exactly as extracted (no judgement)."""
        out: dict = {}
        for jid in dict.fromkeys(ext_ids):
            try:
                rec = ctx.store.job_record(ctx.job["tenant_id"], ctx.job["workspace_id"], jid)
            except Exception:
                base["unsupported"].append({"kind": "extraction_job", "ref": jid, "reason": "job_not_found"})
                continue
            if rec.get("kind") != "extraction" or rec.get("execution_status") != "COMPLETED":
                base["unsupported"].append({"kind": "extraction_job", "ref": jid,
                                            "reason": "not_a_completed_extraction_job"})
                continue
            for c in (rec.get("execution_result") or {}).get("candidates") or []:
                aid = c.get("artifact_revision_id")
                if not isinstance(c, dict) or aid not in arts:
                    continue
                ann = {k: c.get(k) for k in ("candidate_id", "subject", "predicate", "value", "unit", "scope",
                                              "applicability", "operator")}
                for ev in c.get("evidence") or []:
                    out.setdefault((aid, _lockey(ev.get("locator"))), []).append(ann)
        return out

    @staticmethod
    def _pack(arts, obs_all, cands_by_obs, f, patterns):
        """Raw authorized evidence only: observations cited by an extraction candidate, or carrying the old/new number."""
        wanted = (f["old_value"], f["new_value"])
        subject_toks = _tok(f["subject"])      # relevance is generic token overlap with the confirmed request subject
        by_art: dict = {}
        for o in obs_all:
            if o["artifact_id"] in arts:
                by_art.setdefault(o["artifact_id"], []).append(o)
        pack: dict = {}
        lines: list = []
        n = 0
        for aid in sorted(arts, key=lambda i: arts[i].get("logical_path") or i):
            path = arts[aid].get("logical_path") or aid
            for o in sorted(by_art.get(aid, []), key=lambda o: _lockey(o.get("locator"))):
                text = extraction._observation_text(o)
                if text is None:
                    continue
                key = (aid, _lockey(o.get("locator")))
                raw = o.get("raw_value")
                nums = [_num(raw)] if _num(raw) is not None else _numbers_in(text)
                carries_value = any(w in nums for w in wanted)
                related = False
                for c in cands_by_obs.get(key, []):
                    ctoks = _tok(c.get("subject")) | _tok(c.get("predicate")) | _tok(_label(o.get("locator")))
                    if len(subject_toks & ctoks) >= min(2, len(subject_toks)):
                        related = True
                if not (carries_value or related):
                    continue
                n += 1
                ref = f"E{n:03d}"
                pack[ref] = {"artifact_id": aid, "path": path, "locator": o.get("locator"), "obs": o, "text": text,
                             "kind": _kind(path), "raw": raw, "cands": cands_by_obs.get(key, []),
                             "related": related, "carries_value": carries_value,      # the server's own reasons for the entry: same rule as the request / holds the old or new number
                             "flagged": any(p.search(text) for p in patterns)}
                lines.append(_canon_json({"ref": ref, "path": path, "file_kind": _kind(path),
                                          "location": _label(o.get("locator")), "text": _clip(text, 400),
                                          "stored_value": raw if isinstance(raw, (int, float, str)) else None,
                                          "extracted_as": [{k: v for k, v in c.items() if v is not None}
                                                           for c in cands_by_obs.get(key, [])][:3]}))
        return pack, "\n".join(lines)

    @staticmethod
    def _findings(ctx, payload, base, cand_ids) -> str:
        scratch = {"unsupported": base["unsupported"], "open_items": []}
        ChangeBriefExecutor._open_findings(ctx, payload, scratch, cand_ids)
        base["open_items"] = scratch["open_items"]
        ranked = sorted(scratch["open_items"], key=lambda o: not o.get("touches_selection"))
        return "\n".join(_clip(_canon_json({k: o[k] for k in ("finding_id", "type", "reason", "finding")}), 300)
                         for o in ranked[:8]) or "(none stored)"

    # ------------------------------------------------------------------ execute
    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        pre = ctx.request_context.get("draft_pre")
        if pre is not None:
            return self._finish(pre)
        config = ctx.config
        # The drafting role must be pinned explicitly (budget config `role_model_ids`); it never falls back to the
        # default model nor borrows the extraction role's model.
        if ROLE not in (getattr(config, "role_model_ids", None) or {}):
            raise NonRetryableExecutionError("role_model_not_pinned", f"role_model_ids has no {ROLE!r} entry")
        model_id = model_for_role(config, ROLE)
        # Conservative whole-request guard (instructions + schema + evidence + margin), repeated at the call site and
        # BEFORE budget is consumed or any client is built.
        if (extraction._input_token_bound(request.prompt, request.schema)
                > config.max_input_tokens_per_call):
            raise NonRetryableExecutionError("budget_exceeded", "draft request exceeds input ceiling")
        _assert_global_endpoint()
        # The draft is a classification of a bounded evidence list: it does not need deep reasoning. A first real run spent 11,796 of its 12,288 tokens THINKING and
        # 485 on the answer (finish_reason MAX_TOKENS, nothing usable). The thinking level is bounded here; the token cap and the reservation are unchanged.
        call_config = {"max_output_tokens": ctx.max_output_tokens, "temperature": 0.0,
                       "response_mime_type": "application/json", "thinking_config": {"thinking_level": THINKING_LEVEL}}
        timeout_s = getattr(config, "request_timeout_seconds", None)
        if timeout_s:
            call_config["http_options"] = {"timeout": int(timeout_s * 1000)}
        calls.consume()
        try:
            client = _client_for_model(model_id)
            response = generate_and_close(client, model=model_id, contents=request.prompt, config=call_config)
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if callable(code):
                code = code()
            if code == 429:
                raise ProviderRateLimitError("provider rate limited") from exc
            if is_timeout_exception(exc, code):
                raise ProviderTimeoutError("provider timed out") from exc
            raise ProviderExecutionError("provider request failed") from exc
        actual = _field(response, "model_version") or model_id
        if not isinstance(actual, str) or actual not in config.model_prices:
            raise NonRetryableExecutionError("unpriced_model")
        if actual != model_id:
            raise NonRetryableExecutionError("served_model_differs_from_pinned_role_model", f"{actual}")
        require_normal_finish(response, model_id=model_id, requested_cap=getattr(ctx, "max_output_tokens", None))
        try:
            usage = _field(response, "usage_metadata")
            in_t = _token_count(usage, "prompt_token_count")
            out_t = _token_count(usage, "candidates_token_count")
            th_t = _token_count(usage, "thoughts_token_count")
            total = _token_count(usage, "total_token_count")
            if total is not None and in_t is not None and out_t is not None:
                th_t = max(th_t or 0, total - in_t - out_t)
            try:
                text = _field(response, "text")
            except (ValueError, IndexError, AttributeError) as exc:
                raise ProviderInvalidResponseError("provider returned no text") from exc
            obj = _model_object(text)
            _check_shape(obj)
            self._stale_check(ctx)
            result = self._validate(ctx, obj)
            result["provider_calls"] = 1
            result["endpoint"] = {"backend": "vertex", "location": REQUIRED_LOCATION, "pinned_model": model_id,
                                  "served_model": actual}
        except (ProviderExecutionError, NonRetryableExecutionError) as exc:
            # the provider answered: keep what it reported (priced as reported, not at the ceiling) and the safe metadata, then fail as before
            raise attach_after_answer(exc, response, model_id=model_id, requested_cap=getattr(ctx, "max_output_tokens", None), stage="parse_or_validate")
        out = self._finish(result)
        return ExecutionOutcome(result=out.result, model_id=actual, input_tokens=in_t, output_tokens=out_t,
                                thought_tokens=th_t)

    @staticmethod
    def _coverage_of_inputs(ctx, rev, arts, ext_ids, find_ids) -> dict:
        """What this draft was BASED ON, said plainly: which files have a current extraction, whether any finding (conflict/gap check) exists for this
        revision, and what was ignored because it belongs to an older revision. "No finding" is not "no conflict": nothing checked for one."""
        from infra.worker.verification import _completed_jobs
        store, tenant, ws = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"]
        have = set()
        for jid in ext_ids or []:
            try:
                aid = (store.job_record(tenant, ws, jid).get("payload") or {}).get("artifact_id")
            except Exception:
                aid = None
            if aid in arts:
                have.add(aid)
        older = [j for j in _completed_jobs(store, tenant, ws, "finding") if j["pinned_source_revision_id"] != rev]
        missing = sorted((a.get("logical_path") or aid) for aid, a in arts.items() if aid not in have)
        return {"files_in_scope": len(arts), "files_with_current_extraction": len(have), "files_without_extraction": missing,
                "findings_for_this_revision": len(find_ids or []), "findings_from_older_revisions_ignored": len(older),
                "relations": "not read by the draft; they are computed for the impact analysis (stage 3)",
                "statement": ("A conflict and gap check (finding) exists for this revision." if find_ids else
                              "No conflict and gap check (finding) exists for this revision. That is NOT the same as 'no conflict': nothing checked the sources against each other.")
                + (f" {len(older)} older finding(s) from earlier revisions were ignored." if older else "")}

    @staticmethod
    def _stale_check(ctx) -> None:
        head = ctx.store.head(ctx.job["tenant_id"], ctx.job["workspace_id"])
        if head.source_revision_id != ctx.job.get("pinned_source_revision_id"):
            raise NonRetryableExecutionError("stale_source_revision", "head moved since the evidence was pinned")

    @staticmethod
    def _finish(base: dict) -> ExecutionOutcome:
        base = json.loads(_canon_json(base))
        base.pop("draft_hash", None)
        base["draft_hash"] = hashlib.sha256(_canon_json(base).encode()).hexdigest()
        return ExecutionOutcome(result=base, model_id=None)

    # ------------------------------------------------------------------ validate / materialize
    def _validate(self, ctx: ExecutionContext, obj: dict) -> dict:
        rc = ctx.request_context
        base, pack, f = rc["draft_base"], rc["draft_pack"], rc["draft_fields"]
        old, new = f["old_value"], f["new_value"]
        base["summary"] = _clip(obj["summary"].strip(), 1200)
        base["questions_for_human"] = [_clip(q, 300) for q in obj["questions_for_human"][:10]]
        interp = obj["interpretation"]
        base["interpretation"] = {k: interp.get(k) for k in ("old_value", "new_value", "scope", "currency")}
        for k, want in (("old_value", old), ("new_value", new)):
            got = _num(interp.get(k)) if interp.get(k) is not None else None
            if got != want:
                base["mismatches"].append({"field": k, "model": interp.get(k), "intent": want,
                                           "used": "intent", "reason": "model_intent_mismatch"})
        seen: set = set()
        for it in obj["items"]:
            ref = it["ref"]
            e = pack.get(ref)
            if e is None:
                base["rejected"].append({"ref": ref, "reason": "ref_not_in_evidence"})
                continue
            if ref in seen:
                base["rejected"].append({"ref": ref, "reason": "duplicate_ref"})
                continue
            ok, why, _o = _revalidate({"locator": e["locator"], "quote": it["quote"]}, [e["obs"]])
            if not ok:
                base["unsupported"].append({"kind": "citation", "ref": ref, "action": it["action"], "reason": why})
                continue
            seen.add(ref)
            base["items"].append(self._materialize(e, ref, it, old, new))
        base["injection_flagged"] = any(e["flagged"] for e in pack.values())
        base["unaddressed"] = [{"ref": r, "logical_path": e["path"], "location": _label(e["locator"]), "artifact_id": e["artifact_id"],
                                "text": _clip(e["text"], 200), **({"disposition": "potentially_affected"} if e.get("related") else {})}
                               for r, e in pack.items() if r not in seen]
        for x in base["items"]:
            if x["status"] == "PROPOSED":
                base["edits"].append({"artifact_id": x["artifact_id"], "logical_path": x["logical_path"],
                                      "tier": "MODEL_SELECTED", "locator": x["locator"], "status": "PROPOSED",
                                      "kind": "yaml_edit", "old": x["current"], "new": new,
                                      "proposal_edit": x["proposal_edit"], "ref": x["ref"], "candidate_ids": x["candidate_ids"],
                                      "citation": x["citation"], "rationale": x["rationale"]})
        self._coverage(base, f, pack, seen)
        with_evidence = {e["artifact_id"] for e in pack.values()}
        for aid, a in sorted(rc["draft_arts"].items(), key=lambda kv: kv[1].get("logical_path") or kv[0]):
            path = a.get("logical_path") or aid
            if aid not in with_evidence:
                base["not_covered"].append({"artifact_id": aid, "logical_path": path,
                                            "reason": "no_evidence_retrieved_from_this_artifact"})
            if _kind(path) == "python":
                base["not_covered"].append({"artifact_id": aid, "logical_path": path,
                                            "reason": "python code is observed as text blocks (functions, methods, class headers) and literal assignments; it is not analysed or edited, so a rule found in code is evidence for a person to act on, not a supported edit"})
        apply_dispositions(base, base.get("clarifications_used") or [])
        prev_id = ctx.job["payload"].get("revises_draft_job_id")
        if prev_id:
            chain, cur, guard = [], prev_id, 0
            while cur and guard < 20:                           # the whole line of revises, newest first, then reversed
                guard += 1
                try:
                    rec = ctx.store.job_record(ctx.job["tenant_id"], ctx.job["workspace_id"], cur)
                except Exception:                               # a read problem must never become a silently different draft
                    raise NonRetryableExecutionError("previous_draft_unreadable")
                chain.append((cur, rec.get("execution_result") or {}))
                cur = (rec.get("payload") or {}).get("revises_draft_job_id")
            carry_unresolved(base, open_over_chain(list(reversed(chain))), base.get("clarifications_used") or [], prev_id)
        unresolved = (base["unaddressed"] or base["unsupported"] or base["rejected"] or base["questions_for_human"]
                      or any(x["status"] in ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED") for x in base["items"])
                      or base["not_covered"])
        decided = any(x["status"] in ("PROPOSED", "NO_OP") for x in base["items"])
        base["status"] = "PARTIAL" if unresolved else ("DRAFT_READY" if decided else "NEEDS_CONFIRMATION")
        base["outcome"] = "ANALYSED"
        base["reasons"] = []
        return base

    @staticmethod
    def _materialize(e: dict, ref: str, it: dict, old, new) -> dict:
        raw, text = e["raw"], e["text"]
        nums = [_num(raw)] if _num(raw) is not None else _numbers_in(text)
        state = "at_new" if new in nums else ("at_old" if old in nums else "other")
        act = it["action"]
        loc = e["locator"]
        x = {"ref": ref, "artifact_id": e["artifact_id"], "logical_path": e["path"], "locator": loc,
             "model_action": act, "rationale": _clip(it["rationale"], 400), "server_state": state,
             "current": _num(raw) if _num(raw) is not None else None, "status": None, "reason": None,
             "kind": "manual", "proposal_edit": None,
             "candidate_ids": [c["candidate_id"] for c in e["cands"] if c.get("candidate_id")],
             "citation": {"quote": it["quote"], "locator": loc, "artifact_id": e["artifact_id"],
                          "logical_path": e["path"], "revalidated": True}}
        if e["flagged"]:        # evidence that reads like an instruction cannot steer the outcome: never edited, never silently excluded
            x.update(status="UNSUPPORTED", reason="evidence_text_matches_injection_pattern")
            return x
        if act == "exclude":
            if e.get("related") and state in ("at_old", "other"):
                # The server read this passage as belonging to the SAME rule as the request (candidate subject/predicate overlap, `related`) and its stored value is not the new value.
                # The model's own "exclude" is not a basis to close it: it stays open as POTENTIALLY AFFECTED (not "conflict", not "resolved"). It is not an edit either.
                x.update(status="AMBIGUITY", disposition="potentially_affected",
                         reason=("excluded_by_model_but_the_same_rule_states_a_different_value" if state == "other"
                                 else "excluded_by_model_but_holds_the_old_value_of_the_same_rule"))
                return x
            x.update(status="EXCLUDED", reason="model_excluded_with_rationale",
                     exclusion_basis="server_checked_not_the_same_rule" if (e.get("cands") and not e.get("related")) else "model_only")
            if state == "at_old":
                x["reason"] = "model_excluded_item_holding_the_old_value"
            return x
        if act == "ambiguity":
            x.update(status="AMBIGUITY", reason="model_could_not_decide")
            return x
        if state == "at_new":                                   # server fact wins over edit/manual claims
            x.update(status="NO_OP", reason="already_equals_new_value" if act != "edit"
                     else "model_said_edit_but_already_new")
            return x
        if state == "other":
            x.update(status="AMBIGUITY", reason="stored_value_differs_from_confirmed_old_value")
            return x
        # state == at_old: affected for real
        if act == "no_op":
            x.update(status="AMBIGUITY", reason="model_said_no_op_but_value_is_old")
            return x
        item = _item(artifact_id=e["artifact_id"], path=e["path"], tier="MODEL_SELECTED",
                     obs=e["obs"], text=text, f={"old_value": old, "new_value": new}, basis={"ref": ref})
        if act == "edit" and item["kind"] == "yaml_edit":
            x.update(status="PROPOSED", kind="yaml_edit", proposal_edit=item["proposal_edit"])
        else:
            x.update(status="MANUAL_CHANGE", reason=("not_a_supported_automatic_edit_kind" if act == "edit"
                                                    else "model_marked_manual"))
        return x

    @staticmethod
    def _coverage(base, f, pack, seen) -> None:
        pool: set = set()
        for r in seen:
            e = pack[r]
            pool |= _tok(e["path"]) | _tok(e["text"])
            for c in e["cands"]:
                sc = c.get("scope") if isinstance(c.get("scope"), dict) else {}
                for k, v in sc.items():
                    pool |= _tok(k) | _tok(v)
        for t in f["applies_to"]:
            if not _tok(t) <= pool:
                base["not_covered"].append({"artifact_id": None, "logical_path": None,
                                            "reason": f"applies_to:{t}: no classified evidence item addresses it"})
