"""One read model for "which findings are in force now": used by the Questions list, the knowledge overview and the completion gate of a change, so they cannot disagree.

Three things were mixed before and are kept apart here:
  * CURRENT   a finding whose involved readings (candidates) all belong to an extraction that still describes the current source (pinned to the head, or the file is
              byte-identical since it was read: `infra/v2_current_extraction.py`). The same problem found by several finding runs is ONE issue (same kind, reason and
              readings); the first finding id represents it (later runs are copies); a decision on any copy resolves it.
  * HISTORICAL a finding about a reading that was REPLACED because its file changed. Kept as history, never counted as open. Said whether the new reading was checked
              again (`rechecked`); if not it is "not re-checked yet", never silently dropped.
  * UNCHECKED  current readings that no finding run has looked at. They are not "no problem": they are reported as not checked.

Relation to ONE change (for the completion gate) uses what the change itself recorded, no file or request names:
  * the place(s) the proposal edited (`edits[].path` found as a key path of a reading in the target file),
  * the impact analysis behind the proposal: its DIRECT readings. (A code span a text scan only FLAGGED is a hint, not a proven dependency: NEEDS_REVIEW.)
  A severe (HIGH/CRITICAL, authority required), current, unresolved finding on those places BLOCKS the result. Any other current finding is NEEDS_REVIEW: shown, counted, not
  treated as unrelated, but it does not by itself stop a person from deciding the result. (A file an AI judged as depending on the change is NEEDS_REVIEW too: an AI
  judgement is not a proven dependency.) If the change did not record its edit or impact, the old, stricter rule applies: every severe finding on the change's files blocks.
  Three kinds of "not blocking" are told apart, because they are not the same:
    POSSIBLE_EFFECT   there is EVIDENCE the place may be affected (a file an AI judged as depending on the change, with a quote the server re-checked, or a text-scan hint) and the finding is
                      serious and open: it is never ignored; the person who decides the result must name each one (acknowledge) and it is recorded with the decision.
    NEEDS_REVIEW      the effect on this change is not determined (no evidence either way). Shown and counted in the knowledge status; the system never says "unaffected".
    (no class)        a finding with positive evidence of no effect does not exist: the analysis cannot produce it.
Read-only, runs on the caller's cursor."""
from __future__ import annotations

import json
import re

from infra.v2_current_extraction import completed_jobs, revision_rank, valid_extraction_jobs_cur

SEVERE = ("HIGH", "CRITICAL")

PLAIN = {
    "missing_scope": "The AI could not tell which orders this rule applies to (its scope is not stated in the source).",
    "scope_intersection_undecidable": "Two statements about the same thing overlap and the sources do not say which one applies where.",
    "referenced_candidate_not_found": "A reading this check relied on no longer exists.",
    "insufficient_evidence": "The source does not say enough to tell where this rule applies.",
}
TODO = {
    "GAP": "A person who owns this knowledge states where the rule applies (confirm the rule in Questions & Decisions and answer the question), or the source file is edited to say it.",
    "CONFLICT": "A person who owns this knowledge picks which statement is authoritative (Questions & Decisions: Findings).",
    "INSUFFICIENT_CONTEXT": "A person who owns this knowledge says which statement applies where (Questions & Decisions).",
}


# ---- "the same kind of limit stated somewhere else" (a SIGNAL, not a dependency): a missing relation edge is not evidence of no effect
_GENERIC = frozenset("""a an the of to for and or in on by is are be with from at as it this that all any each per over under above below
    must shall may should rule rules routing config configuration setting settings threshold thresholds limit limits jpy yen amount amounts value values net goods order orders
    request requests new old greater less equal more than or""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def concept_tokens(*texts) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for w in re.findall(r"[A-Za-z][A-Za-z]+", str(t or "").replace("_", " ")):
            w = _stem(w.lower())
            if len(w) >= 4 and w not in _GENERIC:
                out.add(w)
    return out


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return isinstance(v, str) and v.strip().replace(",", "").replace(".", "", 1).isdigit()


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _locator(candidate: dict):
    for ev in candidate.get("evidence") or []:
        if isinstance(ev, dict) and ev.get("locator") is not None:
            return _json(ev["locator"])
    return None


def _overlap(a, b) -> bool:
    """Same place in a file: equal key path, same function, or overlapping lines. Unknown shapes never overlap."""
    a, b = _json(a), _json(b)
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("key_path") and a.get("key_path") == b.get("key_path"):
        return True
    if a.get("qualname") and a.get("qualname") == b.get("qualname"):
        return True
    try:
        return (a.get("line_start") is not None and b.get("line_start") is not None
                and int(a["line_start"]) <= int(b["line_end"]) and int(b["line_start"]) <= int(a["line_end"]))
    except (TypeError, ValueError, KeyError):
        return False


def _claim_view(cid, info, paths):
    c = info["c"]
    return {"candidate_id": cid, "subject": c.get("subject"), "predicate": c.get("predicate"), "value": c.get("value"), "unit": c.get("unit"),
            "scope": c.get("scope"), "applicability": c.get("applicability"), "artifact_id": info["artifact"],
            "logical_path": paths.get(info["artifact"]), "read_at_revision": info["pin"], "current": info["valid"]}


class Model:
    """Plain attributes; `public()` is what routes may serialize (no quotes, no raw readings)."""

    def __init__(self):
        self.head = None
        self.claims: dict[str, dict] = {}
        self.paths: dict[str, str] = {}
        self.current: list[dict] = []
        self.historical: list[dict] = []
        self.unchecked: list[str] = []
        self.counts: dict = {}
        self.by_finding_id: dict[str, dict] = {}                   # every finding id of every run -> {"role": representative|repeat|history, "of": representative id, "resolved": bool}

    def public(self) -> dict:
        return {"head_source_revision_id": self.head, "findings": self.current, "historical": self.historical, "unchecked_claim_ids": self.unchecked, "counts": self.counts}


def build(cur, tenant: str, ws: str) -> Model:
    m = Model()
    cur.execute("SELECT source_revision_id FROM v2_workspaces WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws))
    row = cur.fetchone()
    m.head = row[0] if row else None
    rank = revision_rank(cur, tenant, ws)
    cur.execute("SELECT DISTINCT ON (artifact_id) artifact_id, logical_path FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY artifact_id, source_revision_id DESC", (tenant, ws))
    m.paths = {r[0]: r[1] for r in cur.fetchall()}
    ext = completed_jobs(cur, tenant, ws, "extraction")
    valid_ids = {j["job_id"] for j in valid_extraction_jobs_cur(cur, tenant, ws, m.head, jobs=ext)} if m.head else set()
    # For FINDINGS the doubt goes the other way from coverage. A reading is REPLACED only when the file was read AGAIN (a newer, valid reading of the same file exists). A file that
    # changed but was never read again still has that reading as the latest one the system knows: its findings stay in force (never optimistic: a problem is not dropped because
    # we are unsure), and "the file changed but was not read again" is reported by reanalysis_incomplete, not hidden here.
    has_valid_reading = {(j["payload"] or {}).get("artifact_id") for j in ext if j["job_id"] in valid_ids}
    for j in ext:
        aid = (j["payload"] or {}).get("artifact_id")
        in_force = j["job_id"] in valid_ids or aid not in has_valid_reading
        for c in (j["result"] or {}).get("candidates") or []:
            if isinstance(c, dict) and c.get("candidate_id"):
                m.claims[c["candidate_id"]] = {"job": j["job_id"], "artifact": aid, "pin": j["pinned_source_revision_id"], "valid": in_force, "c": c}
    # a reading a PERSON confirmed stays in force until a person decides otherwise: a finding that contradicts it is never dropped because the file was read again
    cur.execute("""SELECT d.target FROM v2_decisions d WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.action='confirm'
        AND NOT EXISTS (SELECT 1 FROM v2_decisions s WHERE s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))""", (tenant, ws))
    confirmed = {r[0] for r in cur.fetchall()}
    for cid in confirmed:
        if cid in m.claims:
            m.claims[cid]["valid"] = True
    cur.execute("""SELECT d.target FROM v2_decisions d WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.action IN ('resolve','accepted_unknown')
        AND NOT EXISTS (SELECT 1 FROM v2_decisions s WHERE s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))""", (tenant, ws))
    decided = {r[0] for r in cur.fetchall()}
    fjobs = completed_jobs(cur, tenant, ws, "finding")
    cur.execute("SELECT payload->>'job_id', min(id) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND payload ? 'job_id' GROUP BY 1", (tenant, ws))
    made = {r[0]: r[1] for r in cur.fetchall()}                    # the order jobs were created in (job ids are random): "the first finding of an issue" is the oldest
    looked_at = {cid for fj in fjobs for cid in ((fj["payload"] or {}).get("candidate_ids") or [])}
    checked_artifacts = {m.claims[c]["artifact"] for c in looked_at if c in m.claims and m.claims[c]["valid"]}
    groups: dict[tuple, list] = {}
    for fj in fjobs:
        for f in (fj["result"] or {}).get("findings") or []:
            if isinstance(f, dict) and f.get("finding_id"):
                involved = list(f.get("involved_claim_ids") or [])
                groups.setdefault((f.get("kind"), f.get("reason"), tuple(sorted(involved))), []).append((rank.get(fj["pinned_source_revision_id"], -1), made.get(fj["job_id"], 1 << 62), fj["job_id"], f))
    n_dupes = 0
    for sig, members in groups.items():
        members.sort(key=lambda x: (x[0], x[1], x[2]))
        rep = members[0][3]                                        # the ORIGINAL finding id stays the name of the issue (decisions and history point at it); later runs are copies
        involved = list(sig[2])
        infos = [m.claims.get(c) for c in involved]
        if involved and all(i and i["valid"] for i in infos):
            state = "current"
        elif any(i is None for i in infos) or not involved:
            state = "needs_review"                                 # it names a reading that cannot be found: said, not dropped
        else:
            state = "historical"
        ids = [x[3]["finding_id"] for x in members]
        view = {"finding_id": rep["finding_id"], "kind": rep.get("kind"), "reason": rep.get("reason"), "severity": rep.get("severity"),
                "authority_required": bool(rep.get("authority_required")), "involved_claim_ids": involved, "state": state,
                "resolved": any(i in decided for i in ids), "runs": len(ids), "duplicate_finding_ids": [i for i in ids if i != rep["finding_id"]],
                "claims": [_claim_view(c, m.claims[c], m.paths) for c in involved if c in m.claims],
                "plain": PLAIN.get(str(rep.get("reason")), "A check found something that needs a person."), "todo": TODO.get(str(rep.get("kind")), "A person who owns this knowledge looks at it.")}
        for i in ids:
            m.by_finding_id[i] = {"role": ("history" if state == "historical" else "representative" if i == rep["finding_id"] else "repeat"), "of": rep["finding_id"], "resolved": view["resolved"]}
        if state == "current":
            m.current.append(view)
            n_dupes += len(ids) - 1
        elif state == "historical":
            replaced = {m.claims[c]["artifact"] for c in involved if c in m.claims and not m.claims[c]["valid"]}
            view["rechecked"] = replaced <= checked_artifacts
            m.historical.append(view)
        else:
            m.current.append(view)
    m.unchecked = sorted(c for c, i in m.claims.items() if i["valid"] and c not in looked_at)
    open_cur = [v for v in m.current if not v["resolved"]]
    m.counts = {"current_issues": len(m.current), "current_open": len(open_cur), "current_resolved": len(m.current) - len(open_cur),
                "current_severe_open": sum(1 for v in open_cur if v["severity"] in SEVERE and v["authority_required"]),
                "repeated_runs_collapsed": n_dupes, "historical_issues": len(m.historical),
                "historical_not_rechecked": sum(1 for v in m.historical if not v.get("rechecked")), "unchecked_claims": len(m.unchecked)}
    return m


def slim(v: dict) -> dict:
    """What a page needs to name one issue in business words (no quotes, no raw reading)."""
    c = (v.get("claims") or [{}])[0]
    return {"finding_id": v["finding_id"], "kind": v.get("kind"), "reason": v.get("reason"), "severity": v.get("severity"), "plain": v.get("plain"), "todo": v.get("todo"),
            "file": (c.get("logical_path") or "").split("/")[-1] or None, "rule": c.get("subject"), "value": c.get("value"), "unit": c.get("unit"), "claim_id": c.get("candidate_id"),
            "runs": v.get("runs"), "why_review": v.get("why_review"), "state": v.get("state")}


def classify_for_change(cur, tenant: str, ws: str, model: Model, proposal_job_id, scope_artifact_ids) -> dict:
    """-> {basis, blocking[], needs_review[], resolved[]} for ONE change (see the module docstring)."""
    prop = None
    if proposal_job_id:
        cur.execute("SELECT payload, execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND kind='proposal'", (tenant, ws, proposal_job_id))
        prop = cur.fetchone()
    payload = _json(prop[0]) if prop else {}
    result = _json(prop[1]) if prop else {}
    target = (result or {}).get("target_artifact_id") or (payload or {}).get("target_artifact_id")
    edit_paths = [e.get("path") for e in ((result or {}).get("edits") or []) if isinstance(e, dict) and e.get("path")]
    impact = None
    imp_id = (result or {}).get("impact_job_id") or (payload or {}).get("impact_job_id")
    if imp_id:
        cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND kind='impact'", (tenant, ws, imp_id))
        r = cur.fetchone()
        impact = _json(r[0]) if r else None
    places: list[tuple[str, object]] = []                          # (artifact id, locator) the change touches or deterministically reaches
    for cid, info in model.claims.items():
        loc = _locator(info["c"])
        if target and info["artifact"] == target and isinstance(loc, dict) and loc.get("key_path") in edit_paths:
            places.append((info["artifact"], loc))
    located = bool(places)                                         # the edited place was found in a reading of the target file; if not, the whole target file counts (cannot localize)
    for item in ((impact or {}).get("direct") or []):
        info = model.claims.get(item.get("candidate_id")) if isinstance(item, dict) else None
        if info:
            places.append((info["artifact"], _locator(info["c"])))
    hints: list[tuple[str, object]] = []                          # places a deterministic text scan FLAGGED: a place to look, not a proven dependency (the page says so): needs review, never blocking
    for item in ((impact or {}).get("indirect") or []):
        if isinstance(item, dict) and item.get("locator") and isinstance(item.get("path"), list) and item["path"]:
            hints.append((item["path"][-1], item["locator"]))
    basis = "edit_and_impact" if (edit_paths and impact is not None) else "edit_only" if edit_paths else "scope_only"
    floor = located and basis != "scope_only"                      # on the place the request edits, the AI-assigned severity (modality) cannot lower the bar: any open finding there blocks
    out = {"basis": basis, "blocking": [], "needs_review": [], "possible_effect": [], "same_concept": [], "resolved": [], "unchecked_related": [], "unchecked_other": 0}
    judged_files = {i.get("artifact_id") for i in ((impact or {}).get("indirect_model") or []) if isinstance(i, dict)}
    scope = set(scope_artifact_ids or [])
    for cid in model.unchecked:                                    # a current reading nobody has checked: a serious finding could be hiding on a place the change touches
        info = model.claims[cid]
        if basis == "scope_only" or (not located and info["artifact"] == target):
            rel = info["artifact"] in scope or info["artifact"] == target
        else:
            rel = any(info["artifact"] == art and _overlap(_locator(info["c"]), loc) for art, loc in places)
        if rel:
            out["unchecked_related"].append(_claim_view(cid, info, model.paths))
        else:
            out["unchecked_other"] += 1
    def related(v) -> bool:
        arts = {c["artifact_id"] for c in v["claims"]}
        if basis == "scope_only" or (not located and target in arts):
            return bool(arts & scope) or (target in arts)
        return any(_overlap(_locator(model.claims[c["candidate_id"]]["c"]), loc) and model.claims[c["candidate_id"]]["artifact"] == art
                   for c in v["claims"] for art, loc in places if c["candidate_id"] in model.claims)

    for v in model.current:
        severe = v["severity"] in SEVERE and v["authority_required"]
        if v["resolved"]:
            if basis != "scope_only" or {c["artifact_id"] for c in v["claims"]} & scope:
                out["resolved"].append(v)
            continue
        if related(v) and (severe or floor):
            out["blocking"].append(v if severe else {**v, "why_review": "It is on the place this request edits: any open finding there blocks, whatever severity the AI's reading gave it."})
        elif related(v):
            # the place could not be located inside the file, so a finding on the file cannot be called unrelated because the AI read it as a lower modality: a person decides
            out["possible_effect"].append({**v, "why_review": "It concerns the file this change touches and the exact place could not be located; its severity (which follows the modality the AI read) does not decide that it is unrelated."})
        else:
            flagged = any(model.claims[c["candidate_id"]]["artifact"] == art and _overlap(_locator(model.claims[c["candidate_id"]]["c"]), loc)
                          for c in v["claims"] for art, loc in hints if c["candidate_id"] in model.claims)
            judged = any(c["artifact_id"] in judged_files for c in v["claims"])
            severe_v = v["severity"] in SEVERE and v["authority_required"]
            if flagged or judged:                                  # evidence of a possible effect is not cancelled by a lower severity: modality alone can not make the item disappear
                out["possible_effect"].append({**v, "why_review": ("An AI judged the file of this reading as depending on the change (its quote was re-checked in the file); an AI judgement is not a proven dependency, so it is not blocking, but it may affect the change." if judged else
                                                                  "A text scan flagged this place as possibly depending on the change; a hint, not a proven dependency, but it may affect the change.")})
            else:
                out["needs_review"].append({**v, "why_review": ("A text scan flagged this place as possibly depending on the change; that is a hint, not a proven dependency." if flagged else
                                                                "Whether it is related to this change is not known: the analysis never says a rule is unaffected.")})
    for v in model.historical:
        # a serious finding about an earlier reading of a place this change touches: it stops blocking only when the NEW reading has been checked again
        if (not v["resolved"]) and not v.get("rechecked") and ((v["severity"] in SEVERE and v["authority_required"]) or floor) and related(v):
            out["blocking"].append({**v, "not_rechecked": True, "why_review": "It was found on the earlier reading; the changed file has not been checked again."})
    # ---- readings elsewhere that state a limit of the SAME KIND (shared concept words with the request's target and the edited key, a number, a unit): no relation edge is needed and none is assumed
    concept = set()
    intent_id = (result or {}).get("intent_id") or (payload or {}).get("intent_id")
    row = None
    if intent_id:
        cur.execute("SELECT target FROM v2_intents WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s", (tenant, ws, intent_id))
        row = cur.fetchone()
    concept = concept_tokens(*(edit_paths or []), (row[0] if row else ""))
    new_vals = {_num(e.get("after")) for e in ((result or {}).get("edits") or []) if isinstance(e, dict)} - {None}
    old_vals = {_num(e.get("before")) for e in ((result or {}).get("edits") or []) if isinstance(e, dict)} - {None}
    if len(concept) >= 2 and basis != "scope_only":
        for cid, info in model.claims.items():
            c = info["c"]
            if not info["valid"] or info["artifact"] == target:
                continue
            blob = [c.get("subject"), c.get("predicate"), json.dumps(c.get("scope") or {}, default=str)] + [e.get("quote") for e in (c.get("evidence") or [])[:2] if isinstance(e, dict)]
            numbers = {_num(x) for x in re.findall(r"\d[\d,]{3,}", " ".join(str(b) for b in blob))} - {None}      # a limit is stated as a number somewhere in the reading
            if _is_number(c.get("value")):
                numbers.add(_num(c.get("value")))
            if not numbers:
                continue
            shared = sorted(concept_tokens(*blob) & concept)
            if len(shared) < 2 or numbers & new_vals:                 # not the same concept, or it already states the new value
                continue
            out["same_concept"].append({**_claim_view(cid, info, model.paths), "item_id": "reading:" + cid, "shared_words": shared,
                                        "stance": "old_value" if numbers & old_vals else "different_value", "numbers": sorted(int(n) for n in numbers if n == int(n))[:4]})
        out["same_concept"] = sorted(out["same_concept"], key=lambda x: (x["logical_path"] or "", str(x["subject"])))[:60]
    return out
