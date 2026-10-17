"""Proposed Harbor public source sets for the live-local demo, with a manifest of hashes, formats, parser support and EXPECTED analysis jobs.

Reads only the public `harbor_demo/input` tree (never evaluator_private, authoring or reports). No database, no network, no model, no spend.
The original Harbor bundle is never modified: files are imported into a workspace revision, where later changes live.

  python -m tools.judge_local.live_sources [--tier T6|T9|T14] [--write <out.json>]

The tiers are PROPOSALS for the user's decision; none is applied by this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

from tools.judge_local import harbor_inventory as inv

#: Only files that the parsers turn into observations are proposed: an extraction quote must resolve against stored observations, so a file with none would cost a
#: provider job and could never yield an accepted candidate, and the re-extraction wave after an apply covers EVERY artifact of the published revision, so it would also add a
#: job to every later wave. Harbor DOES ship its source code (`app/*.py`); the gap was Ripple's, not Harbor's: the Python parser read only top-level literal assignments.
#: Since parser 1.1.0 a function, a method, a class header and the remaining module statements are `code_block` observations (original text of an exact line span; parsed with
#: `ast`, never executed). The smallest code set that proves the Pitch story is three files (see CODE3); `calculations.py` is optional.
CORE6 = ("config/approval.yaml", "config/workflow.yaml", "docs/procurement_policy.md", "docs/finance_desk_guide.md", "data/approval_matrix.xlsx",
         "docs/emergency_purchases.md")
EXTRA9 = ("docs/requisition_sop.md", "docs/invoice_matching_manual.md", "docs/data_dictionary.md")
#: Code (source_role "implementation"): where the manager threshold is applied (reads the configured value), where emergency routing is applied (carries its OWN literal 1_000_000,
#: it does not read the configured threshold) and where the configuration is loaded (the key names the code reads).
CODE3 = ("app/routing.py", "app/emergency.py", "app/config_loader.py")
CODE_OPTIONAL = ("app/calculations.py",)
EXTRA14 = ("README.md", "docs/cli_guide.md", "docs/system_architecture.md", "data/procurement_summary_202609.xlsx", "data/supplier_directory.xlsx")
TIERS = {"T6": CORE6, "T9": CORE6 + EXTRA9, "T14": CORE6 + EXTRA9 + EXTRA14,
         "T9C": CORE6 + EXTRA9 + CODE3, "T9C+": CORE6 + EXTRA9 + CODE3 + CODE_OPTIONAL}
#: Uploader metadata for the import: code is "implementation" (the extraction prompt then says "extract what the code actually enforces, as observed behavior");
#: everything else keeps the previous "unknown" (no new classification is claimed).
ROLE = {p: "implementation" for p in CODE3 + CODE_OPTIONAL}

#: why each file is in the set (capability it gives) - the reason is stated per file, never implied by the tier.
WHY = {
    "config/approval.yaml": "authoritative routing thresholds (manager 1,000,000; CAPEX finance 2,000,000): the base of every change request",
    "config/workflow.yaml": "the same thresholds inside the workflow steps: a second place that must change together",
    "docs/procurement_policy.md": "the policy text for the same thresholds, plus free freight 500,000 that is NOT an approval limit (a scope distractor)",
    "docs/finance_desk_guide.md": "states the finance threshold with a wider scope than the policy: a real source disagreement for knowledge confirmation",
    "data/approval_matrix.xlsx": "workbook cell RoutingRules!B3 = 500,000: a number that disagrees with the 1,000,000 manager limit elsewhere",
    "docs/emergency_purchases.md": "emergency path with NO number ('higher-value orders require manager sign-off'): honest unknown and scope-exception questions",
    "docs/requisition_sop.md": "routing narrative without numbers: 'not stated' answers and the difference between a process step and a limit",
    "docs/invoice_matching_manual.md": "invoice matching rules (tolerances): an unrelated rule family, so 'does this change control it?' can be answered no",
    "docs/data_dictionary.md": "field definitions: Q&A about what a data field means without any approval limit",
    "app/routing.py": "WHERE the standard manager threshold is applied: `net_amount_jpy >= config.standard_manager_threshold_jpy` (reads the configured value, so a changed approval.yaml changes it) and the CAPEX finance rule",
    "app/emergency.py": "WHERE emergency routing is applied: `net_amount_jpy >= 1_000_000` is a literal in code that does NOT read the configured threshold: a real difference the documents leave vague",
    "app/config_loader.py": "the link between the configuration and the code: which YAML keys are read (`routing_rules.standard_manager_threshold_jpy`) and how they are validated",
    "app/calculations.py": "freight (500,000) and payment-term logic in code: a second place where the number 500,000 means something else (optional)",
    "README.md": "overview of the program", "docs/cli_guide.md": "how the program is run", "docs/system_architecture.md": "architecture overview",
    "data/procurement_summary_202609.xlsx": "cross-check workbook", "data/supplier_directory.xlsx": "cross-check workbook (105 observations)",
}

#: Capabilities each tier covers and what it leaves uncovered (stated, not hidden).
COVERS = {
    "T6": {"covers": ["multi-source extraction", "source disagreement (500,000 vs 1,000,000; 2,000,000 with different scope)", "emergency scope from a document with no number",
                      "change request, clarification, draft, impact/GAP, simulation, approval, apply, re-extraction, fresh query"],
           "not_covered": ["no code evidence: impact and GAP cannot cite code (no code parser); the simulation uses a pinned public copy, not workspace files",
                           "no document-only Q&A beyond policy, finance guide and emergency procedure"]},
    "T9": {"covers": ["everything in T6", "a 'no, this change does not control it' answer (invoice matching) and a 'not stated' answer (requisition SOP)", "data-field Q&A"],
           "not_covered": ["no code evidence (same reason)", "CLI, architecture and README documents and the two other workbooks"]},
    "T9C": {"covers": ["everything in T9", "code evidence with exact line spans: manager threshold applied in routing.py, emergency behaviour in emergency.py (a literal 1,000,000 that ignores the config), the config keys read in config_loader.py",
                       "the 'standard changes with the config but emergency does not' finding, from two kinds of source"],
            "not_covered": ["no dependency graph or call graph between code and config: a link is only what a quote states", "calculations.py (freight and payment terms), matching.py, approval.py, cli.py and the data loaders",
                            "code is read, never run: the behaviour check is the Harbor simulation, not the AI"]},
    "T9C+": {"covers": ["everything in T9C", "freight and payment-term logic (calculations.py) to tell 500,000 as freight from 500,000 as an approval limit"],
             "not_covered": ["same as T9C otherwise"]},
    "T14": {"covers": ["everything in T9", "overview, CLI and architecture documents and the two cross-check workbooks"],
            "not_covered": ["no code evidence (same reason)", "tests and master data (not rule sources)"]},
}


def build_manifest(tier: str, root: pathlib.Path | None = None) -> dict:
    root = root or inv.DEFAULT
    if not root.is_dir() or root.name != "input" or root.parent.name != "harbor_demo":
        raise SystemExit("refusing: the Harbor source must be the public harbor_demo/input directory")
    sys.path.insert(0, str(inv.REPO))
    from core.change_intelligence.v2 import source_ingest_adapters as sia
    from core.change_intelligence.v2.source_admission import admit_files
    rows = {r["path"]: r for r in inv.file_rows(root, sia, admit_files)}
    files, total_calls = [], 0
    for rel in TIERS[tier]:
        r = rows[rel]
        if not (r["eligible"] and r["parser"] == "supported"):
            raise SystemExit(f"{rel} is not eligible for knowledge: {r['excluded_because'] or r['parser']}")
        if r["observations"] == 0:
            raise SystemExit(f"{rel} has no observations: an extraction could not resolve any quote against it, so the job would cost money and yield nothing")
        data = (root / rel).read_bytes()
        parsed = sia.parser_for(f"harbor_demo/{rel}")(f"harbor_demo/{rel}", data)
        if parsed.parse_status != "parsed":
            raise SystemExit(f"{rel} is not read completely ({[d['code'] for d in parsed.diagnostics]}): a partial file must be a deliberate decision, not a default")
        files.append({"path": f"harbor_demo/{rel}", "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data), "format": r["type"],
                      "parser": r["parser"], "parser_version": parsed.parser_version, "parse_status": parsed.parse_status, "diagnostics": [d["code"] for d in parsed.diagnostics],
                      "observations": r["observations"], "source_role": ROLE.get(rel, "unknown"), "business_role": r["business_role"], "why": WHY[rel],
                      "expected_analysis_jobs": 1, "worst_case_usd_per_job": inv.WORST_JOB_USD})
        total_calls += 1
    return {"tier": tier, "source": "public harbor_demo/input only; originals are never modified, changes live in workspace revisions",
            "files": files, "expected_initial_extraction_jobs": total_calls, "worst_case_initial_extraction_usd": round(total_calls * inv.WORST_JOB_USD, 4),
            "coverage": COVERS[tier], "stub_facts_copied": False}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=sorted(TIERS), default="T9C+")
    ap.add_argument("--write", default=None)
    a = ap.parse_args(argv)
    m = build_manifest(a.tier)
    text = json.dumps(m, indent=2)
    if a.write:
        pathlib.Path(a.write).write_text(text, encoding="utf-8")
        print(f"manifest {a.tier}: {len(m['files'])} files, {m['expected_initial_extraction_jobs']} expected extraction jobs, worst case {m['worst_case_initial_extraction_usd']} USD -> {a.write}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
