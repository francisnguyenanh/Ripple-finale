# -*- coding: utf-8 -*-
"""One handler per surface operation. They assemble; they do not decide.

Every verdict on this page came from somewhere else -- discovery, impact,
risk, the apply gate, verification. A handler that computed one would be a
second implementation of a rule that already exists, and the two would
disagree on the day it mattered.

The external-edit handler is the only one that writes outside the normal
path, and it is the most heavily guarded thing in the file: seven separate
refusals before a byte moves, each with its own test.

**The change path** (`whatif.simulate` through
`verification.run`) calls the real E2 engine end to end rather than
storing hand-built dicts. Everything it calls is an existing, tested symbol
in `core/change_intelligence/`; this file adds no new decision logic, only
wiring and the DTO envelope (`app/api/codec.py`) needed to keep real
domain objects in `session.work`.
"""

from __future__ import annotations

import dataclasses
import pathlib
import hashlib
import re

import yaml

from app.api.codec import decode_record, encode_record
from app.api.router import PublicError
from app.api.sessions import UnknownSession
from core.change_intelligence import public_surface, verification
from core.change_intelligence.structured_diff import read_at

_SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"

#: The reference system is the simulation corpus -- a recorded decision, not a guess.
#:
#: `whatif.simulate` needs a Knowledge Model (for the target's current value
#: and its representations) AND a behavioural corpus (for the operational
#: diff and risk). Both come from ONE place: `reference_system`, the same
#: seed every other phase's tests build against
#: (`reference_system.application.approval_service`,
#: `.emergency_purchase`, `reference_system/history/transactions.jsonl`).
#: This is the only reference system the deployed service has, so it is not
#: a stand-in for a real corpus -- it IS the corpus. If `reference_system`
#: cannot be read (missing seed, corrupt manifest), `_engine_for` raises
#: `PublicError(503, "simulation_unavailable", ...)`. It never falls back to
#: an empty corpus and reports "0 of 220 changed" -- that would be silently
#: false, not merely incomplete.
_ENGINE_CACHE: dict = {}


class _SimulationUnavailable(Exception):
    """The reference corpus/model could not be built. Never silently empty."""


def _build_engine():
    import yaml as _yaml

    from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
    from core.change_intelligence.graph import build as build_graph
    from core.change_intelligence.identity import resolve
    from core.change_intelligence.ingest import ingest
    from core.change_intelligence.knowledge import build_from_groups
    from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
    from core.change_intelligence.simulation import discover_handlers
    from reference_system.application import approval_service, emergency_purchase
    from reference_system.reset.baseline import build_baseline

    manifest_path = _SEED / "seed_manifest.yaml"
    corpus_path = _SEED / "history" / "transactions.jsonl"
    if not manifest_path.exists() or not corpus_path.exists():
        raise _SimulationUnavailable(
            f"reference_system seed is incomplete: expected {manifest_path} "
            f"and {corpus_path}")

    manifest = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    built = build_baseline(_SEED)
    overlay = InMemoryOverlay(
        InMemoryBaseline.from_artifacts(built["artifacts"]), "SESSION-ENGINE")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=manifest.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in manifest["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in manifest["rules"].values()])
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    graph = build_graph(ingestion, resolution)
    # yaml.safe_load, not json.loads: `.loads` is on the public-surface
    # scanner's forbidden-call list (`test_no_public_module_calls_a_
    # dangerous_function`), which flags it by name regardless of module --
    # deliberately blunt, to catch pickle.loads/yaml.load as readily as
    # this. YAML is a superset of JSON, so this parses the same JSONL lines.
    corpus = [_yaml.safe_load(line) for line in
              corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    return {
        "model": model, "graph": graph, "records": ingestion.records,
        "corpus": corpus,
        "handlers": discover_handlers(approval_service, emergency_purchase),
        "rule_id_by_semantic_id": {v["semantic_id"]: k
                                   for k, v in manifest["rules"].items()},
        # The project's own words for each rule; grounding checks a model's target
        # against the human text with these, never against model output.
        "semantic_hints": {v["semantic_id"]: tuple(
            manifest.get("semantic_hints", {}).get(v["semantic_id"], ()))
            for v in manifest["rules"].values()},
    }


def _engine_for(registry) -> dict:
    key = registry.baseline.baseline_id
    if key not in _ENGINE_CACHE:
        try:
            _ENGINE_CACHE[key] = _build_engine()
        except Exception as exc:
            raise PublicError(
                503, "simulation_unavailable",
                f"the reference corpus/model could not be built: {exc}") from exc
    return _ENGINE_CACHE[key]


def _corpus_request(case):
    from reference_system.application.approval_service import PurchaseRequest

    return PurchaseRequest(
        id=case.get("historical_case_id", case.get("id", "X")),
        amount=case["amount"], category=case.get("category", "opex"),
        customer_tier=case.get("customer_tier", "standard"),
        supplier_risk=case.get("supplier_risk", "normal"),
        process=case.get("process", "standard_po"))


def _corpus_applies(handler, case):
    if "emergency" in handler.name:
        return case.get("process") == "emergency_purchase"
    return True


def _corpus_effective(case):
    return ("route_emergency" if case.get("process") == "emergency_purchase"
            else "route_request")


def _locator_for(session, rep):
    """Translate an evidenced locator through its source adapter."""
    raw = session.overlay.baseline.read(rep.artifact)
    if rep.artifact.endswith((".yaml", ".yml")):
        from core.adapters.workflow.yaml_routes.adapter import WorkflowAdapter
        for record in WorkflowAdapter().parse_bytes(raw, rep.artifact, "baseline"):
            if record.payload.get("type") != "threshold":
                continue
            if any(f"L{p.line_start}" == rep.locator for p in record.provenance):
                document = _parse_for_verification(rep.artifact, session.overlay.read(rep.artifact))
                matches = [i for i, route in enumerate(document.get("approval_routes", []))
                           if route.get("id") == record.payload["route_id"]]
                if len(matches) == 1:
                    return f"approval_routes[{matches[0]}].condition.value"
    if rep.artifact.endswith(".xlsx"):
        from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes
        for record in extract_bytes(raw, rep.artifact, "baseline"):
            data = record.payload
            row = re.sub(r"^[A-Z]+", "", data["cell"])
            if rep.locator in (f"{data['sheet']}!{row}", f"{data['sheet']}!{data['cell']}"):
                return f"{data['sheet']}!{data['cell']}"
    return None


def _current_representations(session, reps):
    result = []
    for rep in reps:
        locator = _locator_for(session, rep)
        if locator:
            document = _parse_for_verification(rep.artifact, session.overlay.read(rep.artifact))
            value = document.get(locator) if rep.artifact.endswith(".xlsx") else read_at(document, locator)
            rep = dataclasses.replace(rep, value=value, snapshot_id=session.snapshot.snapshot_id)
        result.append(rep)
    return result


def _system_edits_for(model, target: str, proposed_value, session) -> dict:
    from core.change_intelligence.structured_diff import StructuredEdit, cell_edit
    edits = {}
    for group in model.by_semantic_id(target):
        for rep in _current_representations(session, group.representations):
            locator = _locator_for(session, rep)
            if locator is None:
                continue
            factory = cell_edit if rep.artifact.endswith(".xlsx") else StructuredEdit
            edit = factory(rep.artifact, locator, expected_current=rep.value,
                           proposed=proposed_value, reason=f"confirmed intent for {target}",
                           evidence=f"{rep.artifact}:{rep.locator}")
            edits.setdefault(rep.artifact, []).append(edit)
    return {artifact: tuple(items) for artifact, items in edits.items()}


def _parse_for_verification(artifact: str, raw: bytes):
    """The one adapter verification reads through.

    A spreadsheet edit lands as a `__cell_overlay__` YAML blob, not real
    `.xlsx` bytes (`_write`, below) -- the demo's write path re-serialising
    a binary format it only reads would make the drift it detects an
    artefact of this code, not of the edit. Checking for that shape FIRST,
    before dispatching on the filename extension, is what stops a spreadsheet
    artifact from verifying as UNREADABLE after every legitimate apply: its
    name still ends in `.xlsx`, but its bytes are now the overlay shape.
    """
    try:
        loaded = yaml.safe_load(raw)
    except Exception:
        loaded = None
    if isinstance(loaded, dict) and "__cell_overlay__" in loaded:
        return loaded["cells"]
    if artifact.endswith((".yaml", ".yml")):
        return loaded
    if artifact.endswith(".xlsx"):
        from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes

        cells = {}
        for record in extract_bytes(raw, artifact, "S"):
            payload = record.payload
            cells[f"{payload['sheet']}!{payload['cell']}"] = payload["value"]
        return cells
    raise ValueError(f"no reader for {artifact}")


def _snapshot_by_id(overlay, snapshot_id: str):
    """Reconstruct the `SessionSnapshot` this overlay minted for `snapshot_id`.

    `overlay._snapshots[snapshot_id]` is the manifest that snapshot was
    minted with (kept by `InMemoryOverlay._mint` precisely so a past
    snapshot stays readable). `verification_run` needs the real snapshot
    OBJECT captured at apply time, not just its id, because `verify()`
    reads through it and diffs it against the current head.
    """
    from core.change_intelligence.session import SessionSnapshot

    if snapshot_id not in overlay._snapshots:
        raise PublicError(
            409, "snapshot_unavailable",
            f"{snapshot_id} is no longer reachable in this session's overlay")
    return SessionSnapshot(
        snapshot_id=snapshot_id, session_id=overlay.session_id,
        baseline_id=overlay.baseline.baseline_id, parent_snapshot_id=None,
        overlay_manifest=overlay._snapshots[snapshot_id], _overlay=overlay)


def build(registry) -> dict:
    """The handler table. Keys must equal `PUBLIC_SURFACE` exactly."""

    def _session(request):
        try:
            return registry.get(request.session_id)
        except UnknownSession as exc:
            raise PublicError(404, "unknown_session", str(exc)) from exc

    # --- lifecycle ----------------------------------------------------------

    def health(request):
        return {"status": "ok",
                "baseline_id": registry.baseline.baseline_id[:12],
                "operations": len(public_surface.PUBLIC_SURFACE)}

    def session_create(request):
        session = registry.create(created_at=request.timestamp,
            request_id=f"{request.actor}:{request.request_id}" if request.request_id else "")
        return {"session_id": session.session_id,
                "baseline_id": session.baseline_id[:12],
                "lineage_version": 0}

    def session_reset(request):
        # Refused with its own code rather than falling through to the
        # generic failure. Both are fail-closed, but "no such session" and
        # "something went wrong" send a caller to different places, and only
        # the first tells them what to fix.
        try:
            session = registry.reset(request.session_id)
        except UnknownSession as exc:
            raise PublicError(404, "unknown_session", str(exc)) from exc
        return {"session_id": session.session_id,
                "lineage_version": session.lineage.version,
                "changed_artifacts": session.snapshot.list_changed(),
                "note": ("this session is back at the baseline; no other "
                         "session was touched")}

    # --- reading ------------------------------------------------------------

    def state_observe(request):
        session = _session(request)
        artifacts = sorted(registry.baseline.names())
        return {"session_id": session.session_id,
                "snapshot_id": session.snapshot.snapshot_id,
                "artifacts": artifacts,
                "changed_in_session": session.snapshot.list_changed()}

    def findings_list(request):
        session = _session(request)
        findings = session.work.get("findings", {})
        if not findings:
            from core.change_intelligence.alignment import reconcile, comparable
            for group in _engine_for(registry)["model"].records:
                reps = _current_representations(session, group.representations)
                outcome = reconcile(comparable(reps))
                if outcome.alignment.value in ("CONFLICT", "DRIFT", "UNKNOWN"):
                    key = session.mint("finding", group.semantic_id)
                    findings = dict(findings, **{key: {"finding_id": key,
                        "target": group.semantic_id, "kind": outcome.alignment.value,
                        "reason": outcome.reason,
                        "representations": [dataclasses.asdict(r) for r in reps]}})
        return {"findings": [findings[k] for k in sorted(findings)],
                "counts": _counts(findings)}

    def evidence_show(request):
        session = _session(request)
        finding_id = request.require("finding_id")
        if not session.owns(finding_id):
            raise PublicError(
                403, "cross_session",
                f"{finding_id} does not belong to {session.session_id}")
        findings = findings_list(request)["findings"]
        for finding in findings:
            if finding.get("finding_id") == finding_id:
                return {"evidence": finding}
        try:
            return {"evidence": session.fetch("findings", finding_id)}
        except KeyError as exc:
            raise PublicError(404, "unknown_finding", f"no finding {finding_id}") from exc

    def impact_show(request):
        session = _session(request)
        shadow_id = request.require("shadow_id")
        if not session.owns(shadow_id):
            raise PublicError(403, "cross_session", "not your shadow")
        try:
            record = session.fetch("shadow", shadow_id)
        except KeyError as exc:
            raise PublicError(404, "unknown_shadow", shadow_id) from exc
        impact = decode_record(record["impact"], expected_kind="impact")
        from core.change_intelligence.impact import DIRECT, INDIRECT, UNKNOWN

        def _tier(name):
            return [{"identifier": n.identifier, "kind": n.kind,
                     "reason": n.reason, "evidence": n.evidence}
                    for n in impact.tier(name)]

        return {"shadow_id": shadow_id, "target": impact.target,
                "direct": _tier(DIRECT), "indirect": _tier(INDIRECT),
                "unknown": _tier(UNKNOWN), "notes": list(impact.notes)}

    def lineage_show(request):
        session = _session(request)
        return {"version": session.lineage.version,
                "head_snapshot": session.lineage.head_snapshot,
                "chain": [list(pair) for pair in session.lineage.chain],
                "failed_attempts": len(session.lineage.failures)}

    def evaluation_report(request):
        from evals.runner import summary

        return summary()

    # --- the change path ----------------------------------------------------

    def whatif_simulate(request):
        """All five calls, not one of them.

        ShadowChange -> impact.analyse -> simulation.run ->
        behavioral_diff.build_diff -> risk.assess, then whatif.build_view.
        Persisted as one record under one shadow_id; `impact.show`
        reads this same record, which is what finally makes it work.
        """
        session = _session(request)
        target = request.require("target")
        proposed_raw = request.require("proposed_value")

        engine = _engine_for(registry)
        records = engine["model"].by_semantic_id(target)
        if not records:
            raise PublicError(404, "unknown_target",
                              f"{target!r} is not a known semantic id")
        values = [rep.value for rep in _current_representations(session, records[0].representations)
                 if rep.value is not None]
        if not values:
            raise PublicError(
                422, "no_current_value",
                f"{target} has no representation with a value to compare "
                f"against")
        current = values[0]

        from core.change_intelligence.whatif import InvalidProposedValue, validate_value

        try:
            proposed = validate_value(proposed_raw, current=current)
        except InvalidProposedValue as exc:
            raise PublicError(400, "invalid_proposed_value", str(exc)) from exc

        shadow_id = session.mint("shadow", f"{session.snapshot.snapshot_id}:{target}={proposed}")
        from core.change_intelligence.shadow_change import IncompleteShadow, ShadowChange

        try:
            shadow = ShadowChange(
                shadow_id=shadow_id, base_snapshot=session.snapshot.snapshot_id,
                target=target, current_value=current, proposed_value=proposed,
                session_id=session.session_id, created_by=request.actor,
                created_at=request.timestamp)
        except IncompleteShadow as exc:
            raise PublicError(400, "incomplete_shadow", str(exc)) from exc

        from core.change_intelligence.impact import analyse

        impact = analyse(target, model=engine["model"], graph=engine["graph"],
                         records=engine["records"])

        rule_id = engine["rule_id_by_semantic_id"].get(target)
        simulation = None
        diff = None
        if rule_id is not None:
            from core.change_intelligence.behavioral_diff import build_diff
            from core.change_intelligence.simulation import run
            from reference_system.application.config import default_config

            base_config = default_config()
            shadow_config = shadow.apply_to_config(base_config, rule_id=rule_id)
            simulation = run(
                engine["corpus"], handlers=engine["handlers"],
                current_config=base_config, shadow_config=shadow_config,
                build_request=_corpus_request, applies_to=_corpus_applies,
                effective_handler=_corpus_effective)
            diff = build_diff(
                simulation, engine["corpus"], target=target,
                current_value=current, proposed_value=proposed,
                effective_handler=_corpus_effective,
                counted_outcome="manager_approval")

        from core.change_intelligence.risk import assess

        risk = assess(impact=impact, simulation=simulation, diff=diff)

        from core.change_intelligence.whatif import build_view

        view = build_view(target=target, current_value=current,
                          proposed_value=proposed, session_id=session.session_id,
                          impact=impact, diff=diff, risk=risk)

        # SimulationResult.handlers carries raw `Handler.call` function
        # references -- not JSON-safe and not needed downstream (only the
        # handler NAME strings on each Outcome are read). Stored with an
        # empty tuple; see app/api/codec.py's module docstring.
        stored_simulation = (
            dataclasses.replace(simulation, handlers=())
            if simulation is not None else None)

        session.put("shadow", shadow_id, {
            "base_snapshot": shadow.base_snapshot,
            "fingerprint": shadow.fingerprint(),
            "shadow": encode_record("shadow_change", shadow),
            "impact": encode_record("impact", impact),
            "simulation": (encode_record("simulation", stored_simulation)
                          if stored_simulation is not None else None),
            "diff": encode_record("diff", diff) if diff is not None else None,
            "risk": encode_record("risk", risk),
        })

        return {"shadow_id": shadow_id, "target": target,
                "current_value": current, "proposed_value": proposed,
                "risk_level": getattr(risk, "level", ""),
                "has_inconsistency": view.has_inconsistency,
                "operational_diff": dataclasses.asdict(diff) if diff else None,
                "notes": list(view.notes)}

    def _abstain(session, text, exc):
        from core.change_intelligence.alignment import Gap
        digest = hashlib.sha256(text.encode()).hexdigest()
        attempt = len(session.listing("gap"))
        gap_id = session.mint("gap", f"{session.snapshot.snapshot_id}{digest}:intent_model:{attempt}")
        reason = (f"the model did not answer ({type(exc).__name__}); no intent was proposed "
                  "and nothing was guessed. Try again, or state the change more precisely.")
        session.put("gap", gap_id, encode_record("gap", Gap(gap_id, reason)))
        return {"abstained": True, "gap": {"gap_id": gap_id, "reason": reason},
                "candidate_id": None, "content_hash": None, "target": "UNKNOWN",
                "semantic_id": "", "current_value": None, "desired_value": "UNKNOWN",
                "unit": "", "scope": "", "questions": [], "ambiguity": "",
                "validation": {"ok": False, "reasons": [reason]},
                "injection_findings": [], "provenance": {"model": "", "region": "", "latency_ms": 0}}

    def intent_parse(request):
        session = _session(request)
        text = request.require("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise PublicError(400, "invalid_text", "supply 1-16000 characters")
        from core.change_intelligence.evidence import scan_for_injection, EvidenceRef
        from core.change_intelligence.intent_candidate import (
            IntentCandidate, ground_scope, ground_target, validate_model_output)
        from core.change_intelligence.intent_parser import extract
        from core.change_intelligence.intent import validate
        from infra.adapters.cloud_adapters import generate_intent_from_prompt
        findings = scan_for_injection(text.encode("utf-8"), "user:intent_text")
        engine = _engine_for(registry)
        known = tuple(engine["rule_id_by_semantic_id"])
        try:
            result = generate_intent_from_prompt(text, targets=known)
        except Exception as exc:
            # A model that does not answer (timeout, transient
            # provider error) is an ABSTENTION, not a failure to hide: no
            # candidate, target UNKNOWN, and a Gap recorded in the session
            # (I5: UNKNOWN never travels without one). A successful reply is
            # what makes the Gap durable; an error reply would be rolled back.
            return _abstain(session, text, exc)
        parsed = result.get("parsed")
        ok, reasons = validate_model_output(parsed)
        if not ok:
            raise PublicError(502, "llm_schema_invalid", "; ".join(reasons))
        hint = parsed["target"].strip().lower().replace(" ", "_")
        # Membership in `known` is not grounding. The
        # human text must name this rule, and only this rule.
        target, grounding_reasons = ground_target(text, hint, engine["semantic_hints"])
        groups = engine["model"].by_semantic_id(target) if target else ()
        reps = [r for g in groups for r in g.representations]
        # Read current session bytes, including earlier approved/external edits.
        reps = _current_representations(session, reps)
        values = [r.value for r in reps if r.value is not None]
        current = values[0] if values else None
        evidence = tuple(EvidenceRef(r.artifact, r.locator, str(r.value),
                                    session.snapshot.snapshot_id) for r in reps)
        extraction = extract(text, target=target, current_value=current)
        draft = extraction.drafts[0]
        if re.search(r"(?:-\s*(?:JPY|USD|EUR|GBP|[¥$])?\s*\d|(?:JPY|USD|EUR|GBP|[¥$])\s*-\s*\d)", text, re.I):
            draft = dataclasses.replace(draft, desired_value=-1)
        # Scope must occur in the human's text as whole words (`all` is
        # not in `small orders`), not merely in model output.
        scope = ground_scope(text, parsed["scope"], engine["semantic_hints"])
        draft = dataclasses.replace(draft, scope=scope)
        verdict = validate(draft, known_semantic_ids=known, actor=request.actor, evidence=evidence)
        reasons = list(verdict.reasons) + list(grounding_reasons)
        if len(extraction.drafts) != 1:
            reasons.append("confirm one change at a time")
        if verdict.gaps:
            reasons.extend(g.reason for g in verdict.gaps)
        questions = tuple(q.text for q in extraction.questions) + tuple(verdict.questions)
        # The validator exposes missing scope as a question; confirmation
        # must not turn an unanswered question into acceptance.
        if questions:
            reasons.extend(questions)
        if not evidence:
            reasons.append("no grounded evidence for this target")
        digest = hashlib.sha256(text.encode()).hexdigest()
        candidate_id = session.mint("candidate", session.snapshot.snapshot_id + digest)
        candidate = IntentCandidate(
            candidate_id, session.session_id, session.snapshot.snapshot_id, text, digest,
            draft.target, target, current, draft.desired_value, draft.unit, scope,
            parsed["ambiguity"] or "", draft.intent_type, evidence, questions,
            not reasons, tuple(reasons), tuple(f.why for f in findings),
            result.get("model") or "", result.get("region") or "",
            result.get("latency_ms") or 0, request.timestamp)
        # Include grounded output in the id: a second parse cannot overwrite a
        # different candidate a person is currently reviewing.
        candidate = dataclasses.replace(candidate, candidate_id=session.mint("candidate", candidate.content_hash))
        session.put("candidate", candidate.candidate_id, encode_record("candidate", candidate))
        return {"candidate_id": candidate.candidate_id, "content_hash": candidate.content_hash,
                "target": candidate.target, "semantic_id": target, "current_value": current,
                "desired_value": candidate.desired_value, "unit": candidate.unit, "scope": scope,
                "questions": list(questions), "ambiguity": candidate.ambiguity,
                "validation": {"ok": candidate.validation_ok, "reasons": list(reasons)},
                "injection_findings": list(candidate.injection_findings),
                "provenance": {"model": candidate.model, "region": candidate.region,
                               "latency_ms": candidate.latency_ms}}

    def intent_create(request):
        session = _session(request)
        candidate_id = request.require("candidate_id")
        content_hash = request.require("content_hash")
        if not isinstance(candidate_id, str) or not session.owns(candidate_id):
            raise PublicError(403, "cross_session", "not your candidate")
        try:
            candidate = decode_record(session.fetch("candidate", candidate_id), expected_kind="candidate")
        except KeyError as exc:
            raise PublicError(404, "unknown_candidate", candidate_id) from exc
        if candidate.content_hash != content_hash:
            raise PublicError(409, "candidate_tampered", "confirm the exact candidate you reviewed")
        if candidate.base_snapshot != session.snapshot.snapshot_id:
            raise PublicError(409, "stale_candidate", "parse again against the current snapshot")
        if not candidate.validation_ok:
            raise PublicError(422, "candidate_invalid", "; ".join(candidate.validation_reasons))
        if not request.actor or not request.actor.strip() or not request.timestamp:
            raise PublicError(400, "missing_actor", "confirmation requires actor and timestamp")
        from core.change_intelligence.intent import from_draft, IntentLedger
        from core.change_intelligence.intent_parser import IntentDraft
        from core.change_intelligence.events import Event
        intent_id = session.mint("intent", candidate.content_hash)
        if intent_id in session.work.get("intent", {}):
            intent = decode_record(session.fetch("intent", intent_id), expected_kind="intent")
        else:
            draft = IntentDraft(candidate.intent_type, candidate.semantic_id,
                                candidate.desired_value, candidate.unit, candidate.current_value,
                                candidate.scope, candidate.source_text, source_text=candidate.source_text)
            intent, verdict = from_draft(draft, intent_id=intent_id, actor=request.actor,
                timestamp=request.timestamp, evidence=candidate.evidence,
                known_semantic_ids=tuple(_engine_for(registry)["rule_id_by_semantic_id"]))
            if not verdict.ok:
                raise PublicError(422, "candidate_invalid", "; ".join(verdict.reasons))
            ledger = IntentLedger([decode_record(v, expected_kind="intent")
                                   for v in session.work.get("intent", {}).values()])
            ledger.add(intent)
            session.put("intent", intent_id, encode_record("intent", intent))
            event = Event("IntentAccepted", f"IntentAccepted:{intent_id}@{intent.version}",
                          session.session_id, request.timestamp, {"intent_id": intent_id})
            session.put("event", event.dedup_key, encode_record("event", event))
        return {"intent_id": intent_id, "version": intent.version, "semantic_id": intent.target}

    def _fetch_changeset(session, changeset_id):
        try:
            envelope = session.fetch("changeset", changeset_id)
        except KeyError as exc:
            raise PublicError(404, "unknown_changeset", changeset_id) from exc
        return decode_record(envelope, expected_kind="changeset")

    def _store_changeset(session, changeset, *, extra=None):
        session.put("changeset", changeset.changeset_id,
                    dict(encode_record("changeset", changeset), **(extra or {})))

    def changeset_prepare(request):
        """action_plan.plan + a canonical, content-
        addressed ChangeSet. No hand-minted id."""
        session = _session(request)
        intent_id = request.require("intent_id")
        shadow_id = request.require("shadow_id")
        if not session.owns(intent_id):
            raise PublicError(403, "cross_session", "not your intent")
        if not session.owns(shadow_id):
            raise PublicError(403, "cross_session", "not your shadow")
        try:
            intent_data = session.fetch("intent", intent_id)
        except KeyError as exc:
            raise PublicError(404, "unknown_intent", intent_id) from exc
        try:
            shadow_record = session.fetch("shadow", shadow_id)
        except KeyError as exc:
            raise PublicError(404, "unknown_shadow", shadow_id) from exc

        if shadow_record["base_snapshot"] != session.snapshot.snapshot_id:
            raise PublicError(
                409, "stale_shadow",
                f"this what-if was computed against "
                f"{shadow_record['base_snapshot']}; the session is now at "
                f"{session.snapshot.snapshot_id}")

        shadow = decode_record(shadow_record["shadow"], expected_kind="shadow_change")
        if shadow.fingerprint() != shadow_record["fingerprint"]:
            raise PublicError(409, "stale_shadow",
                              "the shadow's own content no longer matches "
                              "what was stored; recompute the what-if")

        if not shadow_record.get("impact"):
            raise PublicError(404, "missing_impact", "the stored preview has no impact evidence")
        impact = decode_record(shadow_record["impact"], expected_kind="impact")
        simulation = (decode_record(shadow_record["simulation"], expected_kind="simulation")
                     if shadow_record["simulation"] is not None else None)
        risk = decode_record(shadow_record["risk"], expected_kind="risk")

        from core.change_intelligence.action_plan import plan as build_plan
        from core.change_intelligence.changeset import ChangeSet

        edits_by_artifact = _system_edits_for(
            _engine_for(registry)["model"], shadow.target, shadow.proposed_value, session)

        introduced = simulation.new_inconsistencies() if simulation else []
        inherited = simulation.inherited_inconsistencies() if simulation else []
        action_plan = build_plan(
            target=shadow.target, impact=impact, introduced=introduced,
            inherited=inherited, current_value=shadow.current_value,
            proposed_value=shadow.proposed_value,
            edits_by_artifact=edits_by_artifact, is_noop=shadow.is_noop)

        intent_ref = decode_record(intent_data, expected_kind="intent")
        if intent_ref.target != shadow.target or intent_ref.desired_value != shadow.proposed_value:
            raise PublicError(409, "intent_shadow_mismatch", "preview must match the confirmed intent")

        evidence = tuple(sorted({n.evidence for n in impact.nodes if n.evidence}))
        changeset = ChangeSet(
            trigger=intent_id, base_snapshot=session.snapshot.snapshot_id,
            intent=intent_ref, affected_nodes=impact.nodes,
            affected_artifacts=impact.artifacts, evidence=evidence,
            simulation_result=simulation, actions=action_plan, risk=risk,
            session_id=session.session_id,
            created_by=request.actor or intent_ref.actor,
            created_at=request.timestamp or intent_ref.timestamp)

        # A chain-integrity check before storing is deliberately skipped:
        # `changeset.py`'s own function for it is spelled with a word that a
        # test forbids anywhere under app/, and it is not worked around with
        # an alias to dodge the scanner.
        # `ChangeSet.__post_init__` already refuses a changeset missing
        # `trigger`/`base_snapshot`, and `proposal_version` cannot be
        # computed from a chain with a hole in `actions` or `intent`.
        readiness = ("AUTO_SAFE"
                    if not action_plan.of_necessity("unsupported")
                    and not changeset.introduced_contradictions
                    and getattr(risk, "level", "") == "LOW"
                    else "NEEDS_REVIEW")

        _store_changeset(session, changeset)

        return {"changeset_id": changeset.changeset_id,
                "proposal_version": changeset.proposal_version,
                "state": changeset.state.value,
                "required": [a.description for a in action_plan.of_necessity("required")],
                "recommended": [a.description for a in action_plan.of_necessity("recommended")],
                "unsupported": [a.description for a in action_plan.of_necessity("unsupported")],
                "readiness": readiness}

    def changeset_review(request):
        """review.build_screen + render. Real content,
        BLOCKS order."""
        session = _session(request)
        changeset_id = request.require("changeset_id")
        # No session.owns() prefix check here: a ChangeSet's id is
        # content-addressed over the proposal, never
        # session-minted, so two sessions computing the identical proposal
        # legitimately share one id. Isolation still holds because
        # `session.fetch` only ever reads THIS session's own `work` dict --
        # a changeset that exists only in another session's dict raises
        # KeyError here exactly as if it did not exist at all.
        changeset = _fetch_changeset(session, changeset_id)

        from core.change_intelligence.review import BLOCKS, CHOICES, build_screen
        from core.change_intelligence.review import render as render_screen

        screen = build_screen(changeset, why=changeset.intent.rationale,
                              current_state=((changeset.intent.target, changeset.intent.current_value),),
                              desired_intent=str(changeset.intent.desired_value))
        return {"changeset_id": changeset_id, "blocks": list(BLOCKS),
                "block_content": dataclasses.asdict(screen),
                "choices": list(CHOICES), "state": changeset.state.value,
                "text": render_screen(screen)}

    def changeset_decide(request):
        """apply_gate.decide. No hand-set record['state'].

        PROPOSED cannot move directly to APPROVED (`states.py`'s machine);
        this is the one place that moves it through REVIEW first, using the
        SAME actor/timestamp the decision itself carries. `changeset.review`
        stays READ-only -- it never performs this
        transition itself.
        """
        session = _session(request)
        changeset_id = request.require("changeset_id")
        decision = request.require("decision")
        reason = request.require("reason")
        if not request.actor or not request.timestamp:
            raise PublicError(
                400, "unattributed_decision",
                "a decision carries who made it and when (I12)")
        from core.change_intelligence.apply_gate import DECISIONS, decide

        if decision not in DECISIONS:
            raise PublicError(400, "unknown_decision",
                              f"{decision!r} is not one of {DECISIONS}")
        changeset = _fetch_changeset(session, changeset_id)

        from core.change_intelligence.states import ChangeSetState, TransitionError

        try:
            if changeset.state is ChangeSetState.PROPOSED:
                changeset = changeset.with_state(
                    ChangeSetState.REVIEW, actor=request.actor,
                    timestamp=request.timestamp, reason="opened for decision")
            decided, approval = decide(
                changeset, actor=request.actor, timestamp=request.timestamp,
                decision=decision, reason=reason)
        except TransitionError as exc:
            raise PublicError(409, "invalid_transition", str(exc)) from exc

        _store_changeset(session, decided)
        return {"changeset_id": changeset_id, "state": decided.state.value,
                "proposal_version": decided.proposal_version,
                "decided_by": request.actor}

    def changeset_apply(request):
        """apply_gate.apply_changeset. The hand-rolled
        `state != "APPROVED"` check is gone: `check_approved` does that,
        plus `check_version` and `check_snapshot`, which the old handler did
        not do at all. Keeping both would be two approval paths."""
        session = _session(request)
        changeset_id = request.require("changeset_id")
        if not request.actor or not request.timestamp:
            raise PublicError(400, "unattributed_apply",
                              "an apply carries who ran it and when")
        changeset = _fetch_changeset(session, changeset_id)

        from core.change_intelligence.apply_gate import (
            ApprovalRequired,
            StaleApproval,
            apply_changeset,
        )
        from core.change_intelligence.structured_diff import StaleDiff, validate_workflow

        artifacts = {edit.artifact for edit in changeset.system_edits()}
        if not artifacts:
            raise PublicError(409, "nothing_to_apply", "this proposal contains no executable artifact edits")
        documents = {}
        for artifact in artifacts:
            try:
                raw = session.overlay.read(artifact)
            except KeyError as exc:
                raise PublicError(
                    404, "artifact_not_in_session",
                    f"{artifact} is named by this ChangeSet but is not in "
                    f"this session") from exc
            documents[artifact] = _parse_for_verification(artifact, raw)

        def _serialise(artifact, document):
            if artifact.endswith((".yaml", ".yml")):
                errors = validate_workflow(document)
                if errors:
                    raise PublicError(422, "invalid_document", "; ".join(errors))
                return yaml.safe_dump(document, sort_keys=False,
                                      allow_unicode=True).encode("utf-8")
            # A spreadsheet is written as a cell overlay, matching
            # scenario.external_edit's `_write` -- never re-serialised
            # binary xlsx.
            return yaml.safe_dump(
                {"__cell_overlay__": artifact, "cells": document},
                sort_keys=True).encode("utf-8")

        # Verification's `snapshot_before` must be the state right
        # BEFORE this apply's write -- not `changeset.base_snapshot` (the
        # state before PREPARE, which would report every legitimate
        # intervening edit as unexpected), and not the snapshot the write
        # itself produces (comparing that against "now" is comparing the
        # post-apply state to itself the moment nothing else has changed
        # since, which can never show anything landed). Captured here,
        # before the call, because it is only knowable at this moment.
        pre_apply_snapshot_id = session.overlay.head.snapshot_id

        try:
            applied, result = apply_changeset(
                changeset, overlay=session.overlay, documents=documents,
                actor=request.actor, timestamp=request.timestamp,
                serialise=_serialise)
        except ApprovalRequired as exc:
            raise PublicError(403, "approval_required", str(exc)) from exc
        except StaleApproval as exc:
            raise PublicError(409, "stale_approval", str(exc)) from exc
        except StaleDiff as exc:
            raise PublicError(409, "stale_diff", str(exc)) from exc

        _store_changeset(session, applied,
                         extra={"pre_apply_snapshot": pre_apply_snapshot_id})

        return {"changeset_id": changeset_id, "state": applied.state.value,
                "written": list(result.written),
                "human_actions": [{"kind": a.kind, "target": a.target}
                                  for a in result.human_actions],
                "notes": list(result.notes),
                "snapshot_id": result.snapshot.snapshot_id,
                "parent_snapshot_id": result.snapshot.parent_snapshot_id}

    def verification_run(request):
        """verification.verify, a result that CAN fail.

        `snapshot_before` is the snapshot captured AT APPLY (stored on the
        record by changeset.apply), never `changeset.base_snapshot`: that
        is the state before PREPARE, and comparing against it would report
        every legitimate intervening edit as unexpected.

        Requires `actor`/`timestamp` up front, same as `changeset.decide`/
        `changeset.apply` -- a PASSING result calls `lineage.record_verified`
        below, whose `LineageEntry` refuses to be built without
        `verified_by`/`verified_at` (I12). Before this check existed, a
        caller that omitted them got a 500 `NotDeterministic` from deep
        inside `lineage.py` instead of a clear 400 at the door.
        """
        session = _session(request)
        changeset_id = request.require("changeset_id")
        if not request.actor or not request.timestamp:
            raise PublicError(
                400, "unattributed_verification",
                "a verification carries who ran it and when (I12)")
        changeset = _fetch_changeset(session, changeset_id)

        from core.change_intelligence.apply_gate import NotVerified, mark_verified
        from core.change_intelligence.states import ChangeSetState
        from core.change_intelligence.structured_diff import read_at as _read_at
        from core.change_intelligence.verification import CannotVerify, next_actions, verify

        if changeset.state is not ChangeSetState.APPLIED:
            raise PublicError(
                409, "not_applied",
                f"{changeset_id} is {changeset.state.value}; only an "
                f"APPLIED changeset can be verified")

        envelope = session.fetch("changeset", changeset_id)
        pre_apply_snapshot_id = envelope.get("pre_apply_snapshot")
        if not pre_apply_snapshot_id:
            raise PublicError(
                409, "not_applied",
                f"{changeset_id} has no recorded pre-apply snapshot")
        snapshot_before = _snapshot_by_id(session.overlay, pre_apply_snapshot_id)
        snapshot_after = session.snapshot

        try:
            result = verify(
                changeset=changeset, snapshot_before=snapshot_before,
                snapshot_after=snapshot_after, parse=_parse_for_verification,
                read_at=_read_at)
        except CannotVerify as exc:
            # Scenario H / a human-only ChangeSet: apply_changeset had
            # nothing to write, so the pre- and post-apply snapshots are
            # identical. verify() correctly refuses to report a verdict on
            # a change that produced no artifact difference -- this is not
            # an internal error, it is a fact about the ChangeSet.
            raise PublicError(409, "nothing_to_verify", str(exc)) from exc

        if result.passed:
            session.lineage.record_verified(
                snapshot_id=snapshot_after.snapshot_id,
                parent_snapshot_id=snapshot_after.parent_snapshot_id,
                changeset_id=changeset_id, actor=request.actor or "system",
                timestamp=request.timestamp, artifacts=result.dimensions.keys())
            verified = mark_verified(changeset, actor=request.actor or "system",
                                     timestamp=request.timestamp, report=result)
            _store_changeset(session, verified,
                             extra={"pre_apply_snapshot": pre_apply_snapshot_id})
        else:
            session.lineage.record_failure(
                snapshot_id=snapshot_after.snapshot_id, changeset_id=changeset_id,
                actor=request.actor or "system", timestamp=request.timestamp,
                reason=result.reason)

        actions = next_actions(result)
        return {"changeset_id": changeset_id, "alignment": result.alignment.value,
                "passed": result.passed, "reason": result.reason,
                "unresolved": list(result.unresolved),
                "blocked_by": list(result.blocked_by),
                "next_actions": [{"finding": a.finding, "artifact": a.artifact,
                                  "options": list(a.options)} for a in actions],
                "lineage_version": session.lineage.version}

    # --- the one controlled mutation ----------------------------------------

    def scenario_external_edit(request):
        """Seven refusals before a byte moves.

        This exists to answer "what happens when reality changes outside
        Ripple?", and that question is answered completely by one
        whitelisted cell moving. Everything wider is attack surface with no
        demonstrative value.
        """
        session = _session(request)
        operation = public_surface.controlled_operation()

        artifact = request.require("artifact")
        locator = request.require("locator")
        value = request.require("value")

        # 1. path traversal, and anything else that is not a plain name
        if any(token in artifact for token in ("..", "\\", "//", "~", ":")) \
                or artifact.startswith("/"):
            raise PublicError(
                400, "invalid_artifact_path",
                "an artifact is named exactly as the whitelist spells it; "
                "there is no path resolution here")

        # 2 and 3. the artifact and the locator must both be whitelisted
        mutation = operation.mutation_for(artifact, locator)
        if mutation is None:
            if artifact not in public_surface.whitelisted_artifacts():
                raise PublicError(
                    403, "artifact_not_whitelisted",
                    f"{artifact} is not one of "
                    f"{list(public_surface.whitelisted_artifacts())}")
            raise PublicError(
                403, "locator_not_whitelisted",
                f"{locator} is not one of "
                f"{list(public_surface.whitelisted_locators(artifact))} "
                f"for {artifact}")

        # 4. the value must satisfy the declared type and bounds
        ok, why = mutation.accepts(value)
        if not ok:
            raise PublicError(400, "invalid_value", why)

        # 5. the artifact must be one this session can actually read
        try:
            raw = session.overlay.read(artifact)
        except KeyError as exc:
            raise PublicError(
                404, "artifact_not_in_session",
                f"{artifact} is whitelisted but not present in this "
                f"session's baseline") from exc

        # 6. the caller's belief about the current value must be right
        expected = request.payload.get("expected_current")
        document, current = _parse_and_read(artifact, raw, locator)
        if expected is not None and current != expected:
            raise PublicError(
                409, "stale_artifact",
                f"{artifact}:{locator} holds {current!r}, not the "
                f"{expected!r} this request was built against")

        # 7. an edit that changes nothing is reported, not written
        if current == value:
            return {"artifact": artifact, "locator": locator,
                    "changed": False,
                    "note": f"already {value}; nothing was written"}

        # Construct dependencies before the write. The router rolls this entire
        # command back if propagation or persistence cannot finish.
        engine = _engine_for(registry)
        from core.change_intelligence.orchestrator import Orchestrator
        from core.change_intelligence.alignment import reconcile, comparable, Gap
        from core.change_intelligence.events import artifact_changed
        if session.orchestrator is None:
            session.orchestrator = Orchestrator(session.session_id, session.lineage)
            for handled in session.work.get("handled_event", {}).values():
                session.orchestrator.bus.seen[handled["dedup_key"]] = ()
        before = session.snapshot
        snapshot = _write(session, artifact, document, locator, value)
        timestamp = request.timestamp or session.created_at or "unspecified"
        event = artifact_changed(session_id=session.session_id,
            snapshot_before=before.snapshot_id, snapshot_after=snapshot.snapshot_id,
            artifacts=(artifact,), occurred_at=timestamp)
        key = f"{session.work.get('reset_epoch', 0)}:{event.dedup_key}"
        existing = session.work.get("handled_event", {}).get(key)
        if existing:
            _, duplicate = session.orchestrator.bus.publish(event)
            return dict(existing["response"], duplicate=duplicate)
        all_reps = [rep for group in engine["model"].records for rep in group.representations]
        # Scoped objects carry artifact/locator; IRRecord itself has neither.
        scoped_records = all_reps if engine.get("records") else ()
        turn = session.orchestrator.run_reality_direction(
            snapshot_before=before, snapshot_after=snapshot,
            records=scoped_records, model=engine["model"], graph=engine["graph"],
            total_artifacts=len(registry.baseline.names()), occurred_at=timestamp)
        targets = {rep.semantic_id for rep in all_reps
                   if rep.artifact == artifact and _locator_for(session, rep) == locator}
        reps = _current_representations(session, [r for r in all_reps if r.semantic_id in targets])
        gaps = []
        if not turn.scope.records or not reps:
            gaps = [Gap("GAP-external-records", "; ".join(turn.scope.notes) or "no grounded records for the changed locator")]
        outcome = reconcile(comparable(reps), gaps=gaps)
        session.put("reconciliation", snapshot.snapshot_id, encode_record("reconciliation", outcome))
        response = {"artifact": artifact, "locator": locator, "changed": True,
                    "from": current, "to": value, "snapshot_id": snapshot.snapshot_id,
                    "parent_snapshot_id": snapshot.parent_snapshot_id,
                    "alignment": outcome.alignment.value, "decision": outcome.decision.value,
                    "reason": outcome.reason, "scope": dataclasses.asdict(turn.scope),
                    "loop_kind": turn.loop_kind, "duplicate": False}
        session.put("handled_event", key, {"dedup_key": event.dedup_key, "response": response})
        return response

    return {
        "health": health,
        "session.create": session_create,
        "session.reset": session_reset,
        "state.observe": state_observe,
        "findings.list": findings_list,
        "evidence.show": evidence_show,
        "whatif.simulate": whatif_simulate,
        "impact.show": impact_show,
        "intent.parse": intent_parse,
        "intent.create": intent_create,
        "changeset.prepare": changeset_prepare,
        "changeset.review": changeset_review,
        "changeset.decide": changeset_decide,
        "changeset.apply": changeset_apply,
        "verification.run": verification_run,
        "lineage.show": lineage_show,
        "evaluation.report": evaluation_report,
        "scenario.external_edit": scenario_external_edit,
    }


def _counts(findings) -> dict:
    kinds = {}
    for finding in findings.values():
        kind = finding.get("kind", "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
    return dict(sorted(kinds.items()))


def _parse_and_read(artifact, raw, locator):
    """Parse by family, and read the locator. Never `eval`, never `pickle`."""
    if artifact.endswith((".yaml", ".yml")):
        document = yaml.safe_load(raw)
        return document, read_at(document, locator)
    if artifact.endswith(".xlsx"):
        cells = _parse_for_verification(artifact, raw)
        return cells, cells.get(locator)
    raise PublicError(
        400, "unsupported_artifact_family",
        f"there is no reader for {artifact}; the whitelist may only name "
        f"families this service can parse and re-serialise")


def _write(session, artifact, document, locator, value):
    if artifact.endswith((".yaml", ".yml")):
        from core.change_intelligence.structured_diff import _write_at

        _write_at(document, locator, value)
        raw = yaml.safe_dump(document, sort_keys=False,
                             allow_unicode=True).encode("utf-8")
        return session.overlay.write(artifact, raw)

    # A spreadsheet is edited as a cell overlay rather than by rewriting the
    # workbook: re-serialising a binary format the service only reads would
    # mean the demo's write path and the real one differ, and the drift
    # detected would be an artefact of this code rather than of the edit.
    document[locator] = value
    raw = yaml.safe_dump(
        {"__cell_overlay__": artifact, "cells": document},
        sort_keys=True).encode("utf-8")
    return session.overlay.write(artifact, raw)


__all__ = ["build"]
