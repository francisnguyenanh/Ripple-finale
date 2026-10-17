"""Durable impact graph traversal + bounded investigation, job kind
"impact", executor_id "impact.analyse.v1".

Contract:

  - Inputs: a confirmed intent id (opaque here -- this executor treats
    `payload.intent_id` as an opaque pinned label, not a resolvable object),
    `payload.candidate_ids` (already-persisted "extraction" RuleCandidate
    ids, resolved tenant/workspace-scoped via the SAME shared primitive
    `RelationGraphExecutor`/`FindingsExecutor` already use --
    `store.find_candidates_by_id`), an optional
    `payload.target_candidate_id` (the rule actually being changed; defaults
    to the FIRST resolved id in `candidate_ids` when omitted), an optional
    `payload.relation_job_ids` (already-persisted "relation" job ids whose
    `execution_result.proposed_edges` with `lifecycle_state == "PROPOSED"`
    are this repo's real, closest "confirmed relation" analogue), and an optional `payload.existing_finding_ids` ("finding" job
    finding ids to carry forward as EXISTING conflicts, never re-derived).

  - Tier semantics reused verbatim from `core.change_intelligence.impact`
    (DIRECT/INDIRECT/UNKNOWN) -- not re-invented here.

  - Traversal: the target candidate is DIRECT. Any OTHER candidate the real,
    already-decided identity graph (`RelationGraphExecutor`)
    proposed as `SAME_RULE`/`PROPOSED` with the target, directly or
    transitively through a chain of PROPOSED edges, is also DIRECT (it is
    the same underlying rule, restated). A candidate the identity graph
    already decided is `DIFFERENT_RULE` against the target (structural scope/
    subject/predicate mismatch -- REJECTED lifecycle) is EXCLUDED, with the
    real decision's own reason carried into the report -- this is the
    negative-control path (an unrelated file citing the same literal
    number must not be flagged). A candidate for which the identity graph
    recorded `UNKNOWN` (unit/scope not resolvable) stays UNKNOWN, never
    promoted either way. A candidate with NO relation evidence covering the
    pair at all (no relation_job_ids given, the pair fell outside a relation
    job's own bounded comparison cap, or a referenced relation job could not
    be read) is a genuine "candidate dependency not yet in the confirmed
    graph" -- the bounded-AI-investigation case. This executor is
    deterministic (`external = False`, no live model account/budget wiring
    exists for a v2 job today outside "analysis"/"extraction" -- see
    `infra/worker/budget_config.py`: this module adds no new shared
    critical key), so it does NOT make a live retrieval/model call; it
    reports such candidates as UNKNOWN with an explicit
    `"reason": "no_confirmed_relation_evidence_bounded_investigation_not_run"`
    rather than ever silently completing or silently promoting them to a
    definite dependency. This is a recorded, conservative scope decision,
    not a silent gap.

  - Candidate ids that do not resolve to a real, persisted candidate at all
    (never extracted, or extracted in a different tenant/workspace --
    `find_candidates_by_id` is itself tenant/workspace scoped, so a
    cross-tenant id simply comes back missing, never resolved across
    tenants) are EXCLUDED with reason "candidate_not_found"; never silently
    dropped from the report.

  - A `relation_job_id` that does not resolve in THIS tenant/workspace, is
    not `kind == "relation"`, or is not yet `COMPLETED`, is recorded in
    `excluded_relation_jobs` with a reason and simply contributes no edges --
    it never raises out of the executor (a bounded, resilient traversal, not
    a crash on a bad or foreign reference).

  - Existing conflicts: `existing_finding_ids` are resolved via the shared
    `store.find_finding_by_id` primitive (no second
    criticality/conflict mechanism) and carried forward verbatim as
    "existing" conflicts. This executor's OWN traversal does not invent new
    CONFLICT findings -- conflict/GAP detection is the "finding" job's job;
    recorded here as an explicit, conservative scope boundary
    (`introduced_conflicts` is always `[]`), not a fabricated second
    mechanism. A referenced finding id that does not resolve is reported in
    `unresolved_finding_ids`, never silently dropped.

  - Caller-supplied conclusion fields (`direct`/`indirect`/`unknown`/
    `coverage`/`impacted_files`) are NEVER read from the payload into the
    result -- the report is derived exclusively from the real, persisted
    graph/candidate/finding data this executor itself resolves (P3 "bypassed
    evidence guard" invariant). This executor never mutates source, the
    confirmed graph, or any finding -- read-only throughout.

  - Coverage: `payload.context_truncated`/`payload.retrieval_cutoff_reached`
    (booleans the caller DECLARES about its own upstream retrieval/context
    step -- this executor cannot independently observe a truncation that
    happened before its own input arrived) are honoured as an explicit
    UNKNOWN-coverage flag, never silently dropped; `coverage.complete` is
    only ever true when nothing was excluded/unresolved AND no truncation
    was declared.

  - Staleness: `payload.expected_decision_epoch`, when given, is checked at
    the ROUTE layer (`infra/v2_policy_routes.py::create_job`) BEFORE this
    executor ever runs -- a stale epoch never reaches a worker attempt at
    all. Every job (any kind) already pins `pinned_source_revision_id`/
    `pinned_knowledge_revision_id`/`pinned_decision_epoch` from the workspace
    head AT CREATION time (`PostgresV2Store._create_job_core`, unchanged by
    this task); this executor denormalizes those three pins into its own
    `execution_result["pinned_input"]` so a reader of the report does not
    need a second lookup to see what it was computed against. Because each
    job's `execution_result` is immutable once written (no UPDATE path
    exists on `v2_jobs.execution_result` outside the worker's own
    admit-once claim), and every report is read by its own `job_id` (never
    a shared "current" pointer a late job could overwrite), there is no
    promotion race to guard here: a late-finishing job simply produces its
    own report, pinned to whatever epoch it was created under, and a reader
    comparing that pin against `store.head()` at read time can always tell
    it is stale. This mirrors the `intent_stale`/`stale_fields`
    pattern (comparison happens at READ time against the pin, not by a
    second mutation at write time).
"""

from __future__ import annotations

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from core.change_intelligence.impact import DIRECT, INDIRECT, UNKNOWN

#: Same real ceiling shape as RelationGraphExecutor's own bounded traversal
#: (`infra/worker/relations.py::_MAX_PAIR_COMPARISONS`) -- a confirmed graph
#: large enough to exceed this must still terminate the BFS, never hang the
#: worker; omitted nodes are counted, not silently skipped.
_MAX_BFS_NODES = 2000

#: `indirect` carries ARTIFACT-LEVEL
#: dependency entries found by a deterministic scan of the pinned revision's observations. RELATED_CANDIDATE /
#: DIFFERENT_RULE edges are NOT used (they carry no discriminating field). An artifact other than the target's is
#: INDIRECT when one of its observations
#:   value_occurrence  contains the target candidate's CURRENT numeric value as a whole number token (canonical
#:                     Decimal after NFKC, separators, scale suffixes, currency symbols; 30 != 3, 750k == 750000)
#:                     AND agrees on the unit/currency when the target has one (occurrence words include the unit
#:                     words; key names such as `threshold_jpy` count) AND, for a small value (|v| < 1000) or a
#:                     unit-less value, shares at least one identity word (>= 3 letters, non-unit) with the
#:                     target's subject/predicate. A small value with a unit needs BOTH unit and shared word.
#:                     Only RULE-bearing artifacts are read: source_role "observational" or a data/log extension
#:                     (csv, tsv, jsonl, log) is evidence, not a rule (labelling rubric v1).
#:   reference         mentions the target artifact's file name, or the target evidence's key path / symbol, as a
#:                     whole token (case-sensitive, >= 4 characters).
#: Evidence path = the observation locator. Scope = `payload.scope_artifact_ids` when given (validated against the
#: revision), else the artifacts sharing the TARGET artifact's bundle prefix (first logical-path
#: segment) plus the artifacts of `payload.candidate_ids`; other bundles are never scanned. For an xlsx artifact
#: the identity words of a small value may also come from the SAME ROW (same sheet, same row number) of other cells of
#: that workbook (never another row, never the column header). `payload.judge_job_ids` = completed `impact.judge`
#: jobs; each judgment is RE-VALIDATED here (quote is a substring of the stored observation text, artifact in scope and
#: not already deterministic, target matches) and reported as `indirect_model`, never merged into `indirect`. Bounded by
#: _MAX_SCAN_OBSERVATIONS (omissions counted in coverage). DIRECT/UNKNOWN/EXCLUDED semantics are unchanged.
_MAX_SCAN_OBSERVATIONS = 5000
_DATA_EXTENSIONS = frozenset({"csv", "tsv", "jsonl", "log"})
_SMALL_VALUE = 1000
_MIN_REFERENCE_LEN = 4


def _artifact_ext(path) -> str:
    name = str(path or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _is_rule_bearing(artifact: dict) -> bool:
    return (artifact.get("source_role") != "observational"
            and _artifact_ext(artifact.get("logical_path") or artifact.get("artifact_id")) not in _DATA_EXTENSIONS)


def _signed_numbers(text) -> frozenset:
    """Canonical SIGNED Decimal strings of a text (rule-1 comparison). Same tokenisation as gap_answer._canon, but a
    '-', U+2212 or '+' glued to the digits is part of the value when it is NOT preceded by a word character, digit,
    '.' or ')' (so `2026-10-01`, `10-20`, `well-70` carry no sign). '+70' == '70'; '-0' == '0'."""
    import re as _re
    import unicodedata
    from decimal import Decimal
    from infra.worker.gap_answer import _NUMBER, _SCALE, _SYMBOLS
    t = unicodedata.normalize("NFKC", str(text)).lower().replace("−", "-")
    for k, v in _SYMBOLS.items():
        t = t.replace(k, v)
    out = set()
    for m in _NUMBER.finditer(t):
        d = Decimal(_re.sub(r"[,  ]", "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
        if m.group(3):
            d *= _SCALE[m.group(3)]
        s = m.start()
        if s >= 1 and t[s - 1] in "-+" and (s == 1 or not (t[s - 2].isalnum() or t[s - 2] in "_.)")):
            if t[s - 1] == "-" and d != 0:
                d = -d
        out.add(format(d.normalize(), "f"))
    return frozenset(out)


def _target_number(candidate: dict):
    """The target's CURRENT value as a canonical SIGNED number string, or None (non-numeric values use references only)."""
    from infra.worker.gap_answer import _canon
    v = candidate.get("value")
    if isinstance(v, bool) or v is None:
        return None
    nums, words = _canon(str(v))
    if len(nums) != 1 or words:
        return None
    signed = _signed_numbers(str(v))
    return next(iter(signed)) if len(signed) == 1 else None


def _target_references(candidate: dict, target_artifact: dict) -> set:
    import json as _json
    refs = set()
    name = str(target_artifact.get("logical_path") or "").rsplit("/", 1)[-1]
    if len(name) >= _MIN_REFERENCE_LEN:
        refs.add(name)
    for ev in candidate.get("evidence") or []:
        try:
            loc = _json.loads(ev.get("locator") or "{}")
        except (TypeError, ValueError):
            continue
        for key in ("key_path", "symbol"):
            if isinstance(loc.get(key), str) and len(loc[key]) >= _MIN_REFERENCE_LEN:
                refs.add(loc[key])
    return refs


def bundle_prefix(artifact: dict) -> str:
    """First logical-path segment (the bundle), or the whole path for a file without a directory."""
    return str(artifact.get("logical_path") or artifact.get("artifact_id") or "").split("/", 1)[0]


def _row_key(o: dict):
    """(artifact, sheet, row number) of a workbook cell observation, else None."""
    import json as _json
    import re as _re
    loc = o.get("locator")
    if isinstance(loc, str):
        try:
            loc = _json.loads(loc)
        except ValueError:
            return None
    if not isinstance(loc, dict) or not isinstance(loc.get("sheet"), str) or not isinstance(loc.get("cell"), str):
        return None
    m = _re.fullmatch(r"\$?[A-Za-z]{1,3}\$?(\d+)", loc["cell"])
    return (o["artifact_id"], loc["sheet"], int(m.group(1))) if m else None


MIN_QUOTE_LEN = 4


def quote_supported(quote, observations) -> bool:
    """Validator shared by the judge job and impact.analyse: a non-trivial quote that is an EXACT substring of one
    stored observation's text (the shown form or the raw value). No normalisation, no fuzzy match."""
    from infra.worker import extraction
    if not isinstance(quote, str) or len(quote.strip()) < MIN_QUOTE_LEN:
        return False
    for o in observations:
        for text in (extraction._observation_text(o), extraction._value_text(o.get("raw_value"))):
            if text is not None and quote in text:
                return True
    return False


def _code_digit_groups(text: str) -> str:
    """Python writes 1_000_000; the shared tokenizer reads `1`, `000`, `000` and would miss it. Only for the text of a `code_block`."""
    import re as _re
    return _re.sub(r"(?<=\d)_(?=\d{3}(?!\d))", ",", text)


def scan_indirect_artifacts(*, target: dict, target_artifact: dict, artifacts: dict, observations: list,
                            scope_artifact_ids: set) -> tuple[list, int]:
    """Pure: -> (artifact-level INDIRECT entries sorted by artifact id, omitted observation count)."""
    import re as _re
    from decimal import Decimal
    from infra.worker import extraction
    from infra.worker.gap_answer import _canon
    number = _target_number(target)
    unit_words = _canon(target.get("unit"))[1] if target.get("unit") else frozenset()
    ident = {w for w in _canon(f"{target.get('subject') or ''} {str(target.get('predicate') or '').replace('_', ' ')}")[1]
             if len(w) >= 3} - set(unit_words)
    small = number is not None and abs(Decimal(number)) < _SMALL_VALUE
    ref_res = [_re.compile(r"(?<![\w.])" + _re.escape(ref) + r"(?![\w])") for ref in sorted(_target_references(target, target_artifact))]
    row_words: dict = {}
    for o in observations:
        key = _row_key(o)
        art = artifacts.get(o["artifact_id"])
        if key is None or art is None or _artifact_ext(art.get("logical_path") or art.get("artifact_id")) != "xlsx":
            continue
        txt = extraction._value_text(o.get("raw_value"))
        if txt is not None:
            row_words.setdefault(key, []).append((str(o.get("observation_id")), _canon(txt)[1]))
    found: dict = {}
    omitted = scanned = 0
    for o in sorted(observations, key=lambda x: (x["artifact_id"], str(x.get("observation_id")))):
        aid = o["artifact_id"]
        art = artifacts.get(aid)
        if (aid == target_artifact.get("artifact_id") or aid not in scope_artifact_ids or art is None
                or not _is_rule_bearing(art) or aid in found):
            continue
        text = extraction._observation_text(o)
        if text is None:
            continue
        scanned += 1
        if scanned > _MAX_SCAN_OBSERVATIONS:
            omitted += 1
            continue
        rule = None
        is_code = o.get("kind") == "code_block"
        if number is not None:
            nums, words = _canon(text)
            if number in _signed_numbers(_code_digit_groups(text) if is_code else text):
                unit_ok = unit_words <= words
                own = _row_key(o)
                row = set()
                if own is not None:
                    for oid, w in row_words.get(own, ()):
                        if oid != str(o.get("observation_id")):
                            row |= w
                shared = bool(ident & (words | row))
                if is_code:
                    # Code has few prose words, and a docstring can repeat one ("standard shipping policy" vs "standard manager threshold"): the SAME number in a code block
                    # counts only when at least two identifying words are shared (all of them when the rule has fewer), and the large-number shortcut does not apply.
                    shared = len(ident & (words | row)) >= min(2, len(ident))
                    ok = shared and (unit_ok or not unit_words)
                else:
                    ok = (unit_ok and (shared or not small)) if unit_words else shared
                if ok:
                    rule = "value_occurrence"
        if rule is None and any(rx.search(text) for rx in ref_res):
            rule = "reference"
        if rule is not None:
            found[aid] = {"artifact_id": aid, "logical_path": art.get("logical_path") or aid, "candidate_id": None,
                          "rule": rule, "locator": o.get("locator"), "path": ["target", rule, aid],
                          "evidence": [{"locator": o.get("locator"), "text": text[:200]}]}
    return [found[k] for k in sorted(found)], omitted


class ImpactAnalysisExecutor:
    """See module docstring. Zero provider calls (`external = False`): every
    input is already-persisted extraction/relation/finding job output, so no
    live model account/budget config is needed to run it -- same posture as
    `RelationGraphExecutor`/`FindingsExecutor`."""

    external = False
    executor_id = "impact.analyse.v1"

    @staticmethod
    def _scan(ctx, job, payload, found, target_id, candidate_ids):
        """-> (indirect, omitted, scope, artifacts, observations, target_artifact). Unreadable -> empty scope."""
        tenant_id, workspace_id, rev = job["tenant_id"], job["workspace_id"], job.get("pinned_source_revision_id")
        empty = ([], 0, set(), {}, [], None)
        if not rev:
            return empty
        try:
            view = ctx.store.source_revision_view(tenant_id, workspace_id, rev)
            observations = ctx.store.source_observations(tenant_id, workspace_id, rev)
        except Exception:
            return empty          # unreadable revision: no INDIRECT claim, never a guess
        artifacts = {a["artifact_id"]: a for a in view["artifacts"]}
        target = found[target_id]
        tart = artifacts.get(target.get("artifact_revision_id"))
        if tart is None:
            return empty
        declared = payload.get("scope_artifact_ids")
        if isinstance(declared, list):
            scope = {a for a in declared if isinstance(a, str) and a in artifacts}
        else:
            bundle = bundle_prefix(tart)
            scope = ({aid for aid, a in artifacts.items() if bundle_prefix(a) == bundle}
                     | {found[c].get("artifact_revision_id") for c in candidate_ids if c in found}) & set(artifacts)
        indirect, omitted = scan_indirect_artifacts(target=target, target_artifact=tart, artifacts=artifacts,
                                                    observations=observations, scope_artifact_ids=scope)
        return indirect, omitted, scope, artifacts, observations, tart

    @staticmethod
    def _model_judgments(ctx, job, payload, target_id, indirect, scope, artifacts, observations, tart):
        """R3c intake. Returns (indirect_model, {requested, accepted, rejected{code: n}}). Every stored judgment is
        re-validated against the stored observations; the stored `accepted` flag is never trusted. A valid negative
        judgment (depends false) is requested but neither accepted nor a failure."""
        ids = [j for j in dict.fromkeys(payload.get("judge_job_ids") or []) if isinstance(j, str)]
        rejected: dict = {}
        out: list = []

        def reject(code):
            rejected[code] = rejected.get(code, 0) + 1
        flagged = {e["artifact_id"] for e in indirect}
        seen: set = set()
        for jid in sorted(ids):
            try:
                rec = ctx.store.job_record(job["tenant_id"], job["workspace_id"], jid)
            except Exception:
                reject("job_not_found")
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "impact.judge":
                reject("job_wrong_kind")
                continue
            if rec.get("execution_status") != "COMPLETED":
                reject("job_not_completed")
                continue
            res = rec.get("execution_result")
            if (not isinstance(res, dict) or not isinstance(res.get("artifact_id"), str)
                    or not isinstance(res.get("target_candidate_id"), str) or not isinstance(res.get("depends"), bool)):
                reject("job_malformed")
                continue
            aid = res["artifact_id"]
            if target_id is None or tart is None or res["target_candidate_id"] != target_id:
                reject("target_mismatch")
                continue
            art = artifacts.get(aid)
            if art is None or aid not in scope or aid == tart.get("artifact_id"):
                reject("artifact_out_of_scope")
                continue
            if aid in flagged:
                reject("already_deterministic")
                continue
            if not _is_rule_bearing(art):
                reject("not_rule_bearing")
                continue
            if res["depends"] is False:
                continue
            quote = res.get("evidence_quote")
            if not isinstance(quote, str) or not quote.strip():
                reject("quote_missing")
                continue
            if not quote_supported(quote, [o for o in observations if o["artifact_id"] == aid]):
                reject("quote_not_substring")
                continue
            if aid in seen:
                reject("duplicate_judgment")
                continue
            seen.add(aid)
            out.append({"artifact_id": aid, "logical_path": art.get("logical_path") or aid, "job_id": jid,
                        "evidence_quote": quote})
        out.sort(key=lambda e: e["artifact_id"])
        return out, {"requested": len(ids), "accepted": len(out), "rejected": rejected}

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        candidate_ids = payload.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            raise NonRetryableExecutionError("candidate_ids_required",
                                             "payload.candidate_ids must be a list")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        tenant_id, workspace_id = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]

        candidate_ids = list(dict.fromkeys(payload.get("candidate_ids") or []))
        found, missing = ctx.store.find_candidates_by_id(
            tenant_id, workspace_id, candidate_ids)

        target_id = payload.get("target_candidate_id")
        if not isinstance(target_id, str) or not target_id:
            target_id = next((cid for cid in candidate_ids if cid in found), None)

        # --- confirmed relation-job intake: bounded, resilient to bad refs.
        relation_job_ids = list(dict.fromkeys(payload.get("relation_job_ids") or []))
        excluded_relation_jobs: list[dict] = []
        # pair -> (decision, reason), symmetric.
        pair_decisions: dict[frozenset, tuple[str, str]] = {}
        for rjid in relation_job_ids:
            try:
                rel_job = ctx.store.job_record(tenant_id, workspace_id, rjid)
            except Exception:
                excluded_relation_jobs.append(
                    {"relation_job_id": rjid, "reason": "relation_job_not_found"})
                continue
            if rel_job.get("kind") != "relation":
                excluded_relation_jobs.append(
                    {"relation_job_id": rjid, "reason": "relation_job_wrong_kind"})
                continue
            if rel_job.get("execution_status") != "COMPLETED":
                excluded_relation_jobs.append(
                    {"relation_job_id": rjid, "reason": "relation_job_not_completed"})
                continue
            result = rel_job.get("execution_result") or {}
            for decision in (result.get("identity_decisions") or []):
                a, b = decision.get("candidate_a_id"), decision.get("candidate_b_id")
                if a is None or b is None:
                    continue
                pair_decisions[frozenset((a, b))] = (
                    decision.get("decision"), decision.get("reason"))

        # --- BFS over confirmed SAME_RULE ("PROPOSED"/DIRECT-equivalence) edges.
        direct_ids: set[str] = set()
        excluded: list[dict] = []
        unknown: list[dict] = []
        indirect: list[dict] = []
        omitted_scan = 0
        omitted_bfs_nodes = 0
        if target_id is not None:
            direct_ids.add(target_id)
            frontier = [target_id]
            visited = {target_id}
            while frontier:
                current = frontier.pop()
                for cid in candidate_ids:
                    if cid == current or cid not in found:
                        continue
                    pair = frozenset((current, cid))
                    decision, _reason = pair_decisions.get(pair, (None, None))
                    if decision == "SAME_RULE" and cid not in visited:
                        if len(visited) >= _MAX_BFS_NODES:
                            omitted_bfs_nodes += 1
                            continue
                        visited.add(cid)
                        direct_ids.add(cid)
                        frontier.append(cid)

        scope, artifacts, observations, tart = set(), {}, [], None
        if target_id is not None and target_id in found:
            indirect, omitted_scan, scope, artifacts, observations, tart = self._scan(
                ctx, job, payload, found, target_id, candidate_ids)
        indirect_model, model_judgments = self._model_judgments(
            ctx, job, payload, target_id if target_id in found else None, indirect, scope, artifacts, observations, tart)
        for cid in candidate_ids:
            if cid not in found:
                excluded.append({"candidate_id": cid, "reason": "candidate_not_found"})
                continue
            if cid == target_id or cid in direct_ids:
                continue
            if target_id is None:
                unknown.append({"candidate_id": cid,
                                "reason": "no_target_candidate_resolved"})
                continue
            pair = frozenset((target_id, cid))
            decision, reason = pair_decisions.get(pair, (None, None))
            if decision == "DIFFERENT_RULE":
                excluded.append({"candidate_id": cid, "reason": reason or "different_rule"})
            elif decision == "UNKNOWN":
                unknown.append({"candidate_id": cid, "reason": reason or "identity_unknown"})
            else:
                # No relation evidence covers this pair at all -- the task
                # card's "candidate dependency not yet in the confirmed
                # graph" case. Bounded AI/retrieval investigation is not
                # wired in this round (see module docstring); reported
                # honestly as UNKNOWN, never promoted.
                unknown.append({
                    "candidate_id": cid,
                    "reason": "no_confirmed_relation_evidence_bounded_investigation_not_run"})

        direct = [{"candidate_id": cid,
                  "path": ["target"] if cid == target_id else ["target", "same_rule", cid],
                  "evidence": found[cid].get("evidence") or []}
                 for cid in candidate_ids if cid in direct_ids]

        # --- existing conflicts (carried forward, never re-derived here).
        existing_finding_ids = list(dict.fromkeys(payload.get("existing_finding_ids") or []))
        existing_conflicts: list[dict] = []
        unresolved_finding_ids: list[str] = []
        for finding_id in existing_finding_ids:
            finding = ctx.store.find_finding_by_id(tenant_id, workspace_id, finding_id)
            if finding is None:
                unresolved_finding_ids.append(finding_id)
            else:
                existing_conflicts.append(finding)

        truncated = bool(payload.get("context_truncated")
                         or payload.get("retrieval_cutoff_reached"))
        complete = (not truncated and not missing and not excluded
                   and not excluded_relation_jobs and not unresolved_finding_ids
                   and omitted_bfs_nodes == 0 and omitted_scan == 0
                   and not model_judgments["rejected"])
        # Two different questions, never one flag (J-review point 5):
        #   traversal_complete - did the traversal itself finish over everything it was given (nothing excluded as
        #                        unresolved, no unusable relation job, no truncation, no omitted nodes/observations)?
        #                        `complete` is kept as an exact ALIAS of it (the readiness gate in whatif.py reads it).
        #   impact_complete    - is the IMPACT fully resolved? Only when the traversal is complete AND no candidate is
        #                        UNKNOWN (UNKNOWN = not covered by relation evidence, never "unaffected").
        coverage = {
            "complete": complete,
            "traversal_complete": complete,
            "unknown_count": len(unknown),
            "impact_complete": bool(complete and not unknown),
            "meaning": "complete/traversal_complete: the traversal finished over its inputs; impact_complete: additionally "
                       "no rule is UNKNOWN. UNKNOWN is not 'unaffected'.",
            "truncated": truncated,
            "omitted_relation_jobs": excluded_relation_jobs,
            "omitted_bfs_nodes": omitted_bfs_nodes,
            "omitted_scan_observations": omitted_scan,
            "unresolved_candidate_ids": missing,
            "unresolved_finding_ids": unresolved_finding_ids,
            "model_judgments": model_judgments,
        }

        result = {
            "executor_id": self.executor_id,
            "intent_id": payload.get("intent_id"),
            "target_candidate_id": target_id,
            "direct": direct,
            "indirect": indirect,  # artifact-level entries, see scan_indirect_artifacts
            "indirect_model": indirect_model,  # re-validated model judgments, never merged into `indirect`
            "unknown": unknown,
            "excluded": excluded,
            "coverage": coverage,
            "existing_conflicts": existing_conflicts,
            "introduced_conflicts": [],  # never invented here; see docstring.
            "pinned_input": {
                "pinned_source_revision_id": job.get("pinned_source_revision_id"),
                "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
                "pinned_decision_epoch": job.get("pinned_decision_epoch"),
            },
            "tiers": {"DIRECT": DIRECT, "INDIRECT": INDIRECT, "UNKNOWN": UNKNOWN},
        }
        return ExecutionOutcome(result=result, model_id=None)
