# -*- coding: utf-8 -*-
"""Run the system against the seed's HAND-WRITTEN ground truth and report.

Every key here comes from `seed_manifest.yaml`, which was written before
these measurements and is validated on every commit by
`tests/reference/test_seed_ground_truth.py`. **No key is derived from an
observation.** That sounds obvious and it is the single easiest mistake to
make in an evaluation harness: a first version of this file fell back to
`impact.artifacts` when a key was missing, which produced a recall of 1.00
that meant "the system agrees with itself".

Where the manifest has no key for something, the metric is reported as **not
measured** rather than scored against a convenient substitute. An absent
measurement is honest; a circular one is worse than none, because it looks
like evidence.

Run:  python -m evals.measure
"""

from __future__ import annotations

import json
import pathlib

import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.action_plan import plan
from core.change_intelligence.graph import build as build_graph
from core.change_intelligence.identity import resolve
from core.change_intelligence.impact import DIRECT, INDIRECT, analyse
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.simulation import discover_handlers, run
from evals.runner import SEED_CORPUS, build_report
from reference_system.application import approval_service, emergency_purchase
from reference_system.application.approval_service import PurchaseRequest
from reference_system.application.config import default_config
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[1] / "reference_system"


def _corpus():
    path = SEED / "history" / "transactions.jsonl"
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _request(case):
    return PurchaseRequest(
        id=case["historical_case_id"], amount=case["amount"],
        category=case.get("category", "opex"),
        customer_tier=case.get("customer_tier", "standard"),
        supplier_risk=case.get("supplier_risk", "normal"),
        process=case.get("process", "standard_po"))


def _applies(handler, case):
    if "emergency" in handler.name:
        return case.get("process") == "emergency_purchase"
    return True


def _effective(case):
    return ("route_emergency" if case.get("process") == "emergency_purchase"
            else "route_request")


def observe() -> tuple:
    """Run the system. Returns `(observations, manifest, counts)`."""
    manifest = yaml.safe_load(
        (SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))
    target = manifest["rules"]["BR-001"]["semantic_id"]

    built = build_baseline(SEED)
    overlay = InMemoryOverlay(
        InMemoryBaseline.from_artifacts(built["artifacts"]), "SESSION-EVAL")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda r, p, s: list(read_xlsx(r, p, s)),
        semantic_hints=manifest.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"]
                                for r, v in manifest["rules"].items()},
        known_semantic_ids=[v["semantic_id"]
                            for v in manifest["rules"].values()])
    model = build_from_groups(resolution, overlay.head,
                              history=ingestion.records)
    graph = build_graph(ingestion, resolution)

    impact = analyse(
        target, model=model, graph=graph, config=default_config(),
        records=ingestion.records,
        roles_by_outcome={"manager_approval": "Procurement Manager"},
        notification_targets={"manager_approval": "notify:pm"})

    corpus = _corpus()
    base = default_config()
    simulation = run(
        corpus,
        handlers=discover_handlers(approval_service, emergency_purchase),
        current_config=base,
        shadow_config=base.with_threshold("BR-001", 500_000),
        build_request=_request, applies_to=_applies,
        effective_handler=_effective)

    action_plan = plan(
        target=target, impact=impact,
        introduced=simulation.new_inconsistencies(),
        inherited=simulation.inherited_inconsistencies(),
        current_value=1_000_000, proposed_value=500_000)

    observations = {
        "impact_found": set(impact.artifacts),
        "conflicts_found": {r.semantic_id for r in model.conflicts()},
        #: Recorded so the report can SHOW the abstentions it declines to
        #: score. An unscored observation is still evidence.
        "gaps_raised": {g.gap_id for g in model.gaps},
    }

    counts = {
        "artifacts in baseline": len(built["artifacts"]),
        "knowledge records": len(model.records),
        "gaps (abstentions)": len(model.gaps),
        "conflicts in the model": len(model.conflicts()),
        "impact artifacts": len(impact.artifacts),
        "  of which DIRECT": len(impact.tier(DIRECT)),
        "  of which INDIRECT": len(impact.tier(INDIRECT)),
        "actions proposed": len(action_plan.actions),
        "  of which required": len(action_plan.of_necessity("required")),
        "simulation cases": simulation.total,
        "  outcome changes": len(simulation.changed),
        "introduced contradictions": len(simulation.new_inconsistencies()),
        "inherited contradictions": len(simulation.inherited_inconsistencies()),
    }
    return observations, manifest, counts, built, corpus, impact


def keys_from_manifest(manifest) -> dict:
    """The hand-written answers. Nothing here looks at what the system did."""
    impact_case = manifest["impact_cases"]["IMPACT-001"]
    return {
        #: DIRECT plus INDIRECT is the set of artifacts the change reaches.
        #: `not_impacted` is the seed's own negative control.
        "impact_expected": set(impact_case["direct"]) | set(
            impact_case["indirect"]),
        "conflicts_expected": {
            case["semantic_id"] for case in manifest["conflicts"].values()},
    }


#: Metrics the manifest has no independent key for. Named so their absence
#: is a stated fact rather than a gap somebody fills with a substitute.
NOT_MEASURED = {
    "correct abstention": (
        "the system raised 2 gaps and the manifest lists 2 ambiguous cases, "
        "but they are about DIFFERENT things -- the gaps are identity "
        "resolution (GAP-IDENTITY-...), the manifest's are prose ambiguity "
        "(AMB-001 'above' vs 'at or above'). No matching policy exists "
        "between the two id spaces. Scoring them against each other would "
        "give 0.00, which reads as 'it failed to abstain' when it abstained "
        "twice; inventing a mapping would give 1.00 for an agreement nobody "
        "checked. Both are worse than saying so"),
    "unnecessary-change rate": (
        "the manifest records which artifacts are impacted, not which "
        "ACTIONS are required; scoring the planner against its own "
        "necessity split would measure nothing"),
    "verification correctness": (
        "no hand-written verification outcome exists in the manifest yet"),
    "drift detection": (
        "the out-of-band edit list is exercised by the P8 loop tests rather "
        "than scored here"),
    "introduced-vs-inherited accuracy": (
        "the manifest does not classify the seeded contradictions, and the "
        "only other source is the simulation being scored"),
}


def main() -> int:
    observations, manifest, counts, built, corpus, impact = observe()
    keys = keys_from_manifest(manifest)

    report = build_report(
        observations, keys,
        baseline_id=built.get("baseline_id", ""),
        corpus_lineage=(f"seed as committed; {len(corpus)} transactions, "
                        f"{len(built['artifacts'])} artifacts"))
    print(report.rendered())

    print("  NOT MEASURED, and why")
    for name, why in sorted(NOT_MEASURED.items()):
        print(f"    {name}")
        print(f"        {why}")
    print()

    print("  ABSTENTIONS OBSERVED (not scored -- see above)")
    for gap in sorted(observations.get("gaps_raised", ())):
        print(f"    {gap}")
    print()

    print("  MISSED, and named")
    expected = {case["semantic_id"]
                for case in manifest["conflicts"].values()}
    missed = sorted(expected - observations["conflicts_found"])
    for semantic_id in missed:
        case = next(k for k, v in manifest["conflicts"].items()
                    if v["semantic_id"] == semantic_id)
        kind = manifest["conflicts"][case].get("kind", "value_mismatch")
        print(f"    {case}  {semantic_id}  ({kind})")
    if not missed:
        print("    (none)")
    print()

    print("  NEGATIVE CONTROL")
    not_impacted = set(
        manifest["impact_cases"]["IMPACT-001"]["not_impacted"])
    wrongly = sorted(not_impacted & set(impact.artifacts))
    print(f"    artifacts the key says are NOT impacted: {len(not_impacted)}")
    print(f"    flagged anyway: {len(wrongly)}"
          + (f"  {wrongly}" if wrongly else "  (none)"))
    print()

    print("  RAW COUNTS")
    for name, value in counts.items():
        print(f"    {name:<28} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
