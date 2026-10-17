"""Pitch change.brief: deterministic, read-only draft brief, job kind "change.brief", executor_id "change.brief.v1".

Claim wording: AI extraction supports finding rules and producing a change proposal with citations through a
deterministic downstream. This executor is that deterministic downstream step: zero provider calls
(`external = False`), no model, no generated prose; it is NOT AI analysis and NOT an AI-written description.

Inputs (payload): `intent_id` (a CONFIRMED structured intent, see core/change_intelligence/v2/brief_form.py),
`extraction_job_ids` (COMPLETED "extraction" jobs from ANY extraction executor: the model one or the D0 AI-off one),
optional `finding_job_ids` (COMPLETED "finding" jobs; their findings are listed as unchanged open items),
optional `scope_artifact_ids` (impact scan scope; default = every artifact of the pinned revision).

What it does
  1. Selection stage (no edits): every extraction candidate whose subject/predicate tokens, scope, currency match the
     intent is listed with its source, value and quote, side by side (approval.yaml 1,000,000 next to
     approval_matrix.xlsx 500,000). It never chooses the authoritative source. A candidate already at the new value is
     NO_OP. Unless the intent carries `target_candidate_id` (the human's confirmed source) the status is
     NEEDS_CONFIRMATION (NOT_FOUND / SINGLE_MATCH_UNCONFIRMED / MULTIPLE_MATCHES). Edits are derived only after that.
  2. Confirmed target: each cited quote is re-validated verbatim against the stored observations (same quote forms as
     extraction `_resolve_evidence`). The existing impact executor is run in process for the target (direct and
     indirect tiers; logic not duplicated). Edits are derived from locators for YAML only (key path, old -> new,
     shaped like the proposal executor's typed yaml edit); markdown / python / xlsx items are published as MANUAL;
     an item already at the new value is NO_OP.
  3. Composes the draft; the output is validated against BRIEF_SCHEMA before it is stored. Unsupported items are
     listed, never hidden; things the product extraction path does not read are listed as NOT COVERED.
It never applies anything: approval/apply stay the existing human review path.
"""
from __future__ import annotations

import json
import re

import jsonschema

from core.change_intelligence.v2.brief_form import normalise_brief_form, parse_exact_number
from infra.adapters.v2_postgres import IntentNotFound
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget,
)

EXECUTOR_ID = "change.brief.v1"
BRIEF_VERSION = "1.0.0"
CLAIM = ("AI extraction supports finding rules and producing a change proposal with citations through a "
         "deterministic downstream. This brief is composed deterministically (no model call, no generated text) "
         "and is a draft: it applies nothing.")
_MAX_LIST = 50

_STOP = frozenset({"the", "a", "an", "of", "for", "per", "in", "to", "and", "or", "on", "by", "rule", "rules"})
#: Closed synonym table (documented in REGISTRATION.md): the only vocabulary this matcher knows.
_SYN = {"threshold": "limit", "cap": "limit", "ceiling": "limit", "maximum": "limit", "max": "limit"}
_CURRENCIES = {"usd", "jpy", "eur", "gbp", "vnd"}


def _tok(text) -> set:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    out = set()
    for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()):
        if not t or t in _STOP or t.isdigit():
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(_SYN.get(t, t))
    return out


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return parse_exact_number(v) if isinstance(v, str) else None


def _numbers_in(text) -> list:
    out = []
    for m in re.finditer(r"(?<![\d.])\d[\d,_]*(?:\.\d+)?", str(text or "")):
        n = parse_exact_number(m.group(0).replace("_", "").rstrip(",").rstrip("."))
        if n is None:
            n = parse_exact_number(m.group(0).replace("_", "").replace(",", ""))
        if n is not None:
            out.append(n)
    return out


def _kind(path: str) -> str:
    p = (path or "").lower()
    if p.endswith((".yaml", ".yml")):
        return "yaml"
    if p.endswith((".md", ".markdown")):
        return "markdown"
    if p.endswith(".py"):
        return "python"
    if p.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    return "other"


def _lockey(loc) -> str:
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except ValueError:
            return loc
    return json.dumps(loc, sort_keys=True)


def _label(loc) -> str | None:
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except ValueError:
            return None
    if not isinstance(loc, dict):
        return None
    for k in ("key_path", "symbol"):
        if isinstance(loc.get(k), str) and loc[k]:
            return loc[k]
    if isinstance(loc.get("sheet"), str) and isinstance(loc.get("cell"), str):
        return f"{loc['sheet']}!{loc['cell']}"
    return None


def _pool(cand: dict, path: str) -> set:
    pool = _tok(cand.get("subject")) | _tok(cand.get("predicate")) | _tok(path.replace("/", " ").replace(".", " "))
    sc = cand.get("scope") if isinstance(cand.get("scope"), dict) else {}
    for k, v in sc.items():
        pool |= _tok(k)
        for x in (v if isinstance(v, list) else [v]):
            pool |= _tok(x)
    for ev in cand.get("evidence") or []:
        pool |= _tok(_label(ev.get("locator")))
    return pool


def _checks(cand: dict, f: dict, path: str) -> dict:
    pool = _pool(cand, path)
    sc = cand.get("scope") if isinstance(cand.get("scope"), dict) else {}
    failed = []
    if not _tok(f["subject"]) <= pool:
        failed.append("subject_tokens_not_all_present")
    for k, v in f["scope"].items():
        have = sc.get(k)
        vals = [str(x).lower() for x in (have if isinstance(have, list) else [have])] if have is not None else []
        if vals:
            if v.lower() not in vals:
                failed.append(f"scope_mismatch:{k}")
        elif not _tok(v) <= pool:
            failed.append(f"scope_not_evidenced:{k}")
    cur = f["currency"].lower()
    unit = str(cand.get("unit") or "").lower()
    quote_text = " ".join(str(e.get("quote") or "") for e in cand.get("evidence") or []).lower()
    if unit in _CURRENCIES and unit != cur:
        failed.append("currency_mismatch")
    elif not (unit == cur or cur in pool or re.search(rf"\b{cur}\b", quote_text)
              or any(str(x).lower() == cur for x in sc.values())):
        failed.append("currency_not_evidenced")
    if cand.get("applicability") not in (None, "RECOGNIZED"):
        failed.append("applicability_not_recognized")
    if f.get("unit"):
        unit_state = "stated" if _tok(f["unit"]) & (pool | _tok(quote_text)) else "not_stated_in_rule"
    else:
        unit_state = "not_requested"
    applies = {t: ("covered" if _tok(t) & (pool | _tok(quote_text)) else "not_evidenced") for t in f["applies_to"]}
    return {"failed": failed, "unit": unit_state, "applies_to": applies}


def _revalidate(ev: dict, observations: list):
    """-> (ok, reason, observation). Same quote forms as extraction `_resolve_evidence`; exact, no fuzzy match."""
    from infra.worker import extraction
    key = _lockey(ev.get("locator"))
    obs = [o for o in observations if _lockey(o.get("locator")) == key]
    if not obs:
        return False, "locator_not_found", None
    o = obs[0]
    quote = extraction._clean_quote(ev.get("quote") or "")
    needle = extraction._normalise(quote)
    if not needle:
        return False, "quote_empty", o
    if extraction._observation_label(o) is not None:
        ok = needle in extraction._quote_forms(o)
    else:
        text = extraction._observation_text(o)
        ok = text is not None and len(needle) >= 8 and needle in extraction._normalise(text)
    return (True, None, o) if ok else (False, "quote_not_in_stored_observation", o)


def _status_for(raw, text, old, new):
    """-> (status, reason) for one located value: PROPOSED (== old), NO_OP (== new), REVIEW (anything else)."""
    nums = [_num(raw)] if _num(raw) is not None else _numbers_in(text)
    if old in nums:
        return "PROPOSED", None
    if new in nums:
        return "NO_OP", "already_equals_new_value"
    return "REVIEW", "old_value_not_found_at_locator"


def _item(*, artifact_id, path, tier, obs, text, f, basis):
    kind = _kind(path)
    loc = obs.get("locator") if obs else None
    raw = obs.get("raw_value") if obs else None
    status, reason = _status_for(raw, text, f["old_value"], f["new_value"])
    item = {"artifact_id": artifact_id, "logical_path": path, "tier": tier, "locator": loc, "basis": basis,
            "status": status, "reason": reason, "kind": "manual", "old": None, "new": None,
            "proposal_edit": None, "text": (str(text)[:200] if text is not None else None)}
    if (kind == "yaml" and isinstance(loc, dict) and isinstance(loc.get("key_path"), str) and status == "PROPOSED"
            and isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw == f["old_value"]):
        item.update(kind="yaml_edit", old=raw, new=f["new_value"],
                    proposal_edit={"path": loc["key_path"], "expected_current": raw, "proposed": f["new_value"]})
    elif status == "NO_OP":
        item["new"] = f["new_value"]
    elif status == "PROPOSED":
        item["status"] = "MANUAL_CHANGE"      # the old value is there, but this kind/shape is not auto-derived
        item["new"] = f["new_value"]
    if item["kind"] == "manual":
        item["manual_note"] = ("NO_OP: already equals the new value" if status == "NO_OP" else
                               f"MANUAL: not derived automatically; a human edits this location "
                               f"({kind} artifacts are not auto-derived)")
    return item


def _summary(c, job_id, executor_id, path, checks, validated, rejected) -> dict:
    return {"candidate_id": c.get("candidate_id"), "extraction_job_id": job_id, "extraction_executor_id": executor_id,
            "artifact_id": c.get("artifact_revision_id"), "logical_path": path, "subject": c.get("subject"),
            "predicate": c.get("predicate"), "value": c.get("value"), "unit": c.get("unit"), "scope": c.get("scope"),
            "applicability": c.get("applicability"),
            "citations": validated, "unsupported_citations": rejected,
            "unit_state": checks["unit"], "applies_to": checks["applies_to"]}


class ChangeBriefExecutor:
    external = False
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        p = ctx.job["payload"]
        if not isinstance(p.get("intent_id"), str) or not p["intent_id"]:
            raise NonRetryableExecutionError("intent_id_required")
        ids = p.get("extraction_job_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            raise NonRetryableExecutionError("extraction_job_ids_required")
        return ExecutionRequest(prompt=None, schema={"type": "object", "executor": EXECUTOR_ID},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        from infra.worker import extraction
        job, store = ctx.job, ctx.store
        tenant, ws = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]
        rev = job.get("pinned_source_revision_id")
        base = {"executor_id": EXECUTOR_ID, "brief_version": BRIEF_VERSION, "claim": CLAIM, "draft_only": True,
                "applies_nothing": True, "provider_calls": 0,
                "pinned_input": {"pinned_source_revision_id": rev,
                                 "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
                                 "pinned_decision_epoch": job.get("pinned_decision_epoch")},
                "rule": None, "impact": None, "edits": [], "candidates": [], "not_selected": [],
                "unsupported": [], "not_covered": [], "open_items": [], "extraction_jobs": []}
        try:
            intent = store.get_intent(tenant, ws, payload["intent_id"])
        except IntentNotFound as exc:
            raise NonRetryableExecutionError("intent_not_found") from exc
        base["intent"] = {"intent_id": intent["intent_id"], "status": intent["status"], "fields": None}
        if intent["status"] != "CONFIRMED":
            return self._done(base, "NEEDS_CONFIRMATION", "INTENT_NOT_CONFIRMED", ["intent_not_confirmed"])
        norm = normalise_brief_form(intent.get("structured_form") or {"old_value": None})
        f = norm["fields"]
        if f is None or intent.get("value") != f["new_value"]:
            return self._done(base, "NEEDS_CONFIRMATION", "INTENT_NOT_UNDERSTOOD",
                              ["intent_form_not_understood"] + norm["problems"])
        base["intent"]["fields"] = f

        view = store.source_revision_view(tenant, ws, rev) if rev else {"artifacts": []}
        arts = {a["artifact_id"]: a for a in view["artifacts"]}
        obs_all = store.source_observations(tenant, ws, rev) if rev else []
        obs_by_art: dict = {}
        for o in obs_all:
            obs_by_art.setdefault(o["artifact_id"], []).append(o)

        # --- candidates from the given extraction jobs (any extraction executor)
        cands: list = []
        covered_arts: dict = {}
        for jid in dict.fromkeys(payload["extraction_job_ids"]):
            try:
                rec = store.job_record(tenant, ws, jid)
            except Exception:
                base["unsupported"].append({"kind": "extraction_job", "ref": jid, "reason": "job_not_found"})
                continue
            res = rec.get("execution_result") or {}
            if rec.get("kind") != "extraction" or rec.get("execution_status") != "COMPLETED":
                base["unsupported"].append({"kind": "extraction_job", "ref": jid,
                                            "reason": "not_a_completed_extraction_job"})
                continue
            xid = res.get("executor_id")
            art_id = (rec.get("payload") or {}).get("artifact_id")
            base["extraction_jobs"].append({"job_id": jid, "executor_id": xid, "artifact_id": art_id,
                                            "candidates": len(res.get("candidates") or [])})
            covered_arts.setdefault(art_id, xid)
            for c in res.get("candidates") or []:
                if isinstance(c, dict) and c.get("candidate_id"):
                    cands.append((c, jid, xid))
        # --- coverage: stated, never hidden
        for aid, a in sorted(arts.items()):
            path = a.get("logical_path") or aid
            k = _kind(path)
            if k == "other":
                continue
            if aid not in covered_arts:
                base["not_covered"].append({"artifact_id": aid, "logical_path": path,
                                            "reason": "no_extraction_job_given"})
            elif str(covered_arts[aid] or "").startswith("extraction.d0") and k in ("xlsx", "python"):
                base["not_covered"].append({"artifact_id": aid, "logical_path": path,
                                            "reason": "d0_scope_is_yaml_and_markdown"})
            if k == "python":
                base["not_covered"].append({
                    "artifact_id": aid, "logical_path": path,
                    "reason": "python code is observed as text blocks (functions, methods, class headers) and literal assignments; it is not analysed or edited, so a rule found in code is evidence for a person to act on, not a supported edit"})
        base["not_covered"].sort(key=lambda d: (d["logical_path"], d["reason"]))

        # --- selection stage (value-agnostic, side by side)
        subj_match: list = []
        near: list = []
        by_id: dict = {}
        for c, jid, xid in cands:
            aid = c.get("artifact_revision_id")
            path = (arts.get(aid) or {}).get("logical_path") or str(aid)
            ch = _checks(c, f, path)
            validated, rejected = [], []
            for ev in c.get("evidence") or []:
                ok, why, _o = _revalidate(ev, obs_by_art.get(aid, []))
                if ok:
                    validated.append({"quote": ev.get("quote"), "locator": ev.get("locator"), "artifact_id": aid,
                                      "logical_path": path, "revalidated": True})
                else:
                    rejected.append({"quote": ev.get("quote"), "locator": ev.get("locator"), "reason": why})
            s = _summary(c, jid, xid, path, ch, validated, rejected)
            val = _num(c.get("value"))
            s["value_relation"] = ("at_old_value" if val == f["old_value"] else
                                   "already_new_value" if val == f["new_value"] else "other_value")
            by_id[c["candidate_id"]] = (c, s, ch)
            if not ch["failed"]:
                subj_match.append(s)
            elif _tok(f["subject"]) & _pool(c, path) and (
                    {x.split(":")[0] for x in ch["failed"]} <= {"currency_not_evidenced", "scope_not_evidenced"}
                    or val in (f["old_value"], f["new_value"])):
                near.append({**s, "failed_checks": ch["failed"]})
        subj_match.sort(key=lambda s: (s["logical_path"], str(s["candidate_id"])))
        near.sort(key=lambda s: (s["logical_path"], str(s["candidate_id"])))
        base["candidates"] = subj_match[:_MAX_LIST]
        base["not_selected"] = near[:_MAX_LIST]
        base["match"] = {"matched_count": len(subj_match)}
        self._open_findings(ctx, payload, base, {s["candidate_id"] for s in subj_match})

        tid = f["target_candidate_id"]
        if tid is None:
            outcome = ("NOT_FOUND" if not subj_match else
                       "SINGLE_MATCH_UNCONFIRMED" if len(subj_match) == 1 else "MULTIPLE_MATCHES")
            return self._done(base, "NEEDS_CONFIRMATION", outcome, ["target_candidate_not_confirmed"], f=f)
        sel = next((s for s in subj_match if s["candidate_id"] == tid), None)
        if sel is None:
            why = ["target_candidate_not_a_matching_candidate"]
            if tid in by_id:
                why += [f"failed:{x}" for x in by_id[tid][2]["failed"]]
            return self._done(base, "NEEDS_CONFIRMATION", "TARGET_NOT_MATCHING", why, f=f)
        if sel["value_relation"] == "other_value":
            return self._done(base, "NEEDS_CONFIRMATION", "TARGET_VALUE_DIFFERS_FROM_CONFIRMED_OLD",
                              [f"target_value={sel['value']!r} confirmed_old={f['old_value']!r}"], f=f)
        if not sel["citations"]:
            base["unsupported"].append({"kind": "rule", "ref": tid, "reason": "no_citation_revalidated"})
            base["unsupported"] += [{"kind": "citation", "ref": tid, **u} for u in sel["unsupported_citations"]]
            return self._done(base, "NEEDS_CONFIRMATION", "QUOTE_NOT_REVALIDATED", ["no_citation_revalidated"], f=f)
        if sel["artifact_id"] not in arts:
            return self._done(base, "NEEDS_CONFIRMATION", "CANDIDATE_REVISION_NOT_CURRENT",
                              ["candidate_artifact_not_in_pinned_revision"], f=f)
        c = by_id[tid][0]
        base["rule"] = {k: sel[k] for k in ("candidate_id", "extraction_job_id", "extraction_executor_id", "subject",
                                            "predicate", "scope", "value", "unit", "citations",
                                            "unsupported_citations")}
        base["not_selected"] = ([s for s in subj_match if s["candidate_id"] != tid] + base["not_selected"])[:_MAX_LIST]
        base["unsupported"] += [{"kind": "citation", "ref": tid, **u} for u in sel["unsupported_citations"]]

        # --- impact: the existing executor, in process, for the confirmed target
        impact = self._impact(ctx, payload, arts, tid)
        base["impact"] = {k: impact[k] for k in ("target_candidate_id", "direct", "indirect", "excluded",
                                                 "unknown", "coverage")}
        # --- edits
        claimed: dict = {}
        for cid, (cc, _s, _ch) in by_id.items():
            for ev in cc.get("evidence") or []:
                claimed.setdefault((cc.get("artifact_revision_id"), _lockey(ev.get("locator"))), []).append(cid)
        seen: set = set()
        items: list = []
        aid = c.get("artifact_revision_id")
        for ev in c.get("evidence") or []:
            ok, _why, o = _revalidate(ev, obs_by_art.get(aid, []))
            if not ok or o is None:
                continue
            seen.add((aid, _lockey(o["locator"])))
            items.append(_item(artifact_id=aid, path=sel["logical_path"], tier="DIRECT", obs=o,
                               text=extraction._observation_text(o), f=f,
                               basis={"rule_candidate_id": tid, "quote": ev.get("quote")}))
        for e in impact["indirect"]:
            iaid, k = e["artifact_id"], (e["artifact_id"], _lockey(e.get("locator")))
            if k in seen:
                continue
            seen.add(k)
            others = [x for x in claimed.get(k, []) if x != tid]
            o = next((x for x in obs_by_art.get(iaid, []) if _lockey(x.get("locator")) == k[1]), None)
            text = ((e.get("evidence") or [{}])[0]).get("text")
            if others:
                oc = by_id[others[0]][1]
                is_match = any(s["candidate_id"] == others[0] for s in subj_match)
                items.append({"artifact_id": iaid, "logical_path": e.get("logical_path"), "tier": "INDIRECT",
                              "locator": e.get("locator"),
                              "status": "OTHER_MATCHING_RULE_NOT_SELECTED" if is_match else "OTHER_RULE_NOT_THIS_CHANGE",
                              "kind": "manual", "old": None, "new": None, "proposal_edit": None,
                              "text": (str(text)[:200] if text else None),
                              "reason": f"occurrence belongs to candidate {others[0]} ({oc['subject']}: {oc['predicate']})",
                              "basis": {"impact_rule": e.get("rule"), "other_candidate_id": others[0]},
                              "manual_note": "not edited by this brief; shown so it is not hidden"})
                continue
            items.append(_item(artifact_id=iaid, path=e.get("logical_path") or iaid, tier="INDIRECT", obs=o,
                               text=text, f=f, basis={"impact_rule": e.get("rule"), "rule_candidate_id": tid}))
        # same-subject candidates already at the new value (e.g. a workbook): reported NO_OP, never edited
        for s in subj_match:
            if s["candidate_id"] != tid and s["value_relation"] == "already_new_value":
                for cit in s["citations"]:
                    k = (s["artifact_id"], _lockey(cit["locator"]))
                    if k in seen:
                        continue
                    seen.add(k)
                    items.append({"artifact_id": s["artifact_id"], "logical_path": s["logical_path"],
                                  "tier": "RELATED_RULE", "locator": json.loads(_lockey(cit["locator"])),
                                  "status": "NO_OP", "kind": "manual", "old": None, "new": f["new_value"],
                                  "proposal_edit": None, "text": cit["quote"], "reason": "already_equals_new_value",
                                  "basis": {"rule_candidate_id": s["candidate_id"], "quote": cit["quote"]},
                                  "manual_note": "NO_OP: already equals the new value"})
        items.sort(key=lambda d: (d["logical_path"] or "", _lockey(d.get("locator"))))
        base["edits"] = items
        for t in f["applies_to"]:
            if not any(s["applies_to"].get(t) == "covered" for s in subj_match):
                base["not_covered"].append({"artifact_id": None, "logical_path": None,
                                            "reason": f"applies_to:{t}: not evidenced by any matching rule"})
        return self._done(base, "DRAFT_READY", "FOUND", [], f=f)

    @staticmethod
    def _impact(ctx, payload, arts, tid):
        from infra.worker.impact import ImpactAnalysisExecutor
        scope = payload.get("scope_artifact_ids")
        if not isinstance(scope, list):
            scope = sorted(arts)
        job = dict(ctx.job)
        job["payload"] = {"intent_id": payload["intent_id"], "candidate_ids": [tid], "target_candidate_id": tid,
                          "scope_artifact_ids": scope,
                          "existing_finding_ids": [x for x in payload.get("existing_finding_ids") or []
                                                   if isinstance(x, str)]}
        sub = ExecutionContext(store=ctx.store, job=job, config=ctx.config)
        ex = ImpactAnalysisExecutor()
        return ex.execute(sub, ex.prepare(sub), ProviderCallBudget(0)).result

    @staticmethod
    def _open_findings(ctx, payload, base, cand_ids):
        """Findings (e.g. C02 scope_mismatch) are listed verbatim as unchanged open items; never resolved here."""
        for jid in dict.fromkeys(payload.get("finding_job_ids") or []):
            try:
                rec = ctx.store.job_record(ctx.job["tenant_id"], ctx.job["workspace_id"], jid)
            except Exception:
                base["unsupported"].append({"kind": "finding_job", "ref": jid, "reason": "job_not_found"})
                continue
            res = rec.get("execution_result") or {}
            if rec.get("kind") != "finding" or rec.get("execution_status") != "COMPLETED":
                base["unsupported"].append({"kind": "finding_job", "ref": jid,
                                            "reason": "not_a_completed_finding_job"})
                continue
            for fd in (res.get("findings") or []):
                if not isinstance(fd, dict):
                    continue
                text = json.dumps(fd, sort_keys=True, default=str)
                base["open_items"].append({
                    "finding_job_id": jid, "finding_id": fd.get("finding_id"),
                    "type": fd.get("kind") or fd.get("type"), "reason": fd.get("reason") or fd.get("rule"),
                    "status": "OPEN_UNCHANGED", "touches_selection": any(c in text for c in cand_ids),
                    "finding": fd})
            for sc in (res.get("scope_comparisons") or []):
                if isinstance(sc, dict):
                    base["open_items"].append({
                        "finding_job_id": jid, "finding_id": None, "type": sc.get("kind") or "scope_comparison",
                        "reason": sc.get("reason") or sc.get("verdict") or sc.get("relation"),
                        "status": "OPEN_UNCHANGED", "touches_selection": False, "finding": sc,
                        "scope_policy": res.get("scope_policy")})
        base["open_items"] = base["open_items"][:_MAX_LIST]

    @staticmethod
    def _done(base, status, outcome, reasons, f=None):
        base["status"] = status
        base["match"] = {**(base.get("match") or {}), "outcome": outcome}
        base["reasons"] = list(reasons)
        if f is not None and base["intent"]["fields"] is None:
            base["intent"]["fields"] = f
        result = json.loads(json.dumps(base, default=str))
        try:
            jsonschema.Draft7Validator(BRIEF_SCHEMA).validate(result)
        except jsonschema.ValidationError as exc:
            raise NonRetryableExecutionError("brief_schema_invalid", exc.message[:200]) from exc
        return ExecutionOutcome(result=result, model_id=None)


BRIEF_SCHEMA = {
    "type": "object",
    "required": ["executor_id", "brief_version", "claim", "draft_only", "applies_nothing", "provider_calls", "status",
                 "match", "reasons", "intent", "candidates", "not_selected", "rule", "impact", "edits",
                 "unsupported", "not_covered", "open_items", "extraction_jobs", "pinned_input"],
    "properties": {
        "executor_id": {"const": EXECUTOR_ID},
        "draft_only": {"const": True}, "applies_nothing": {"const": True}, "provider_calls": {"const": 0},
        "status": {"enum": ["DRAFT_READY", "NEEDS_CONFIRMATION"]},
        "match": {"type": "object", "required": ["outcome"], "properties": {"outcome": {"enum": [
            "FOUND", "NOT_FOUND", "SINGLE_MATCH_UNCONFIRMED", "MULTIPLE_MATCHES", "INTENT_NOT_CONFIRMED",
            "INTENT_NOT_UNDERSTOOD", "TARGET_NOT_MATCHING", "TARGET_VALUE_DIFFERS_FROM_CONFIRMED_OLD",
            "QUOTE_NOT_REVALIDATED", "CANDIDATE_REVISION_NOT_CURRENT"]}}},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "candidates": {"type": "array", "items": {"type": "object", "required": [
            "candidate_id", "logical_path", "value", "value_relation", "citations", "unsupported_citations"]}},
        "edits": {"type": "array", "items": {"type": "object", "required": [
            "artifact_id", "tier", "status", "kind", "locator"], "properties": {
                "status": {"enum": ["PROPOSED", "MANUAL_CHANGE", "NO_OP", "REVIEW", "OTHER_MATCHING_RULE_NOT_SELECTED",
                                    "OTHER_RULE_NOT_THIS_CHANGE"]},
                "kind": {"enum": ["yaml_edit", "manual"]},
                "tier": {"enum": ["DIRECT", "INDIRECT", "RELATED_RULE"]}}}},
        "unsupported": {"type": "array"}, "not_covered": {"type": "array"}, "open_items": {"type": "array"},
    },
    "allOf": [
        {"if": {"properties": {"status": {"const": "DRAFT_READY"}}},
         "then": {"properties": {"match": {"properties": {"outcome": {"const": "FOUND"}}},
                                 "rule": {"type": "object"}, "impact": {"type": "object"}}}},
        {"if": {"properties": {"status": {"const": "NEEDS_CONFIRMATION"}}},
         "then": {"properties": {"edits": {"maxItems": 0}, "rule": {"type": "null"}}}},
    ],
}
