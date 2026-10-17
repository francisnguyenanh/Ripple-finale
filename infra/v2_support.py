"""The registered support matrix for the Harbor demo, served to the UI (read-only). Each row says, per capability, ONE of:

  supported      the capability exists and a test proves it (named in `tests`)
  manual         a person does it; the product only analyses/lists it
  final          needs a CODE change; planned for Final, not a Pitch capability
  not-controlled the config does not control this behaviour (a code trace proves it; evidence shown)
  not-supported  not available
  unknown        not traced / not rehearsed: nothing is claimed

Code evidence is resolved against the pinned public Harbor copy at request time (file:line:text), so a line number can never
drift from the code that actually runs. A row is only "supported" when a test is named; `tests/pitch_brief/test_support_matrix.py`
checks that every named test exists and that every code reference resolves.
"""

from __future__ import annotations

from infra.worker import harbor_sim

T_ROUTE = "tests/pitch_brief/test_harbor_sim.py::test_manager_threshold_lowering_matches_the_registered_expectation_and_is_fully_explained"
T_CAPEX = "tests/pitch_brief/test_harbor_sim.py::test_capex_threshold_edit_changes_only_capex_rows"
T_MATCH = "tests/pitch_brief/test_harbor_sim_match.py::test_raising_the_fixed_tolerance_frees_exactly_the_registered_invoices"
T_TERMS = "tests/pitch_brief/test_harbor_sim_match.py::test_payment_terms_key_is_inert_because_the_code_hardcodes_30_days"
T_EMERG = "tests/pitch_brief/test_harbor_sim_match.py::test_emergency_is_reported_as_not_controlled_by_config_with_code_evidence"
T_NOCHG = "tests/pitch_brief/test_harbor_sim_match.py::test_zero_change_is_not_called_inert_unless_a_trace_proves_no_module_reads_the_key"
T_APPLY = "tests/pitch_brief/test_ui_journey.py::test_full_judge_journey_in_the_browser_with_the_bounded_pump"
T_PERM = "tests/pitch_brief/test_permission_matrix.py::test_matrix_refusals_and_source_unchanged_until_apply"
T_GAP = "tests/pitch_brief/test_gap_loop.py::test_confirm_needs_the_right_role_and_makes_the_old_draft_stale_and_the_next_one_use_it"
T_UIMATCH = "tests/pitch_brief/test_ui_sim_match.py::test_invoice_tolerance_and_payment_terms_in_the_browser"


def _s(state, note=""):
    return {"state": state, "note": note}


# (row id, title, files, view, simulate, apply, analyse, notes, code refs [(file, needle)], tests, extra trace)
ROWS = [
    ("mgr", "Manager approval threshold", ["config/approval.yaml routing_rules.standard_manager_threshold_jpy"],
     _s("supported", "graph, evidence, timeline"), _s("supported", "Harbor `route` over 240 public requisitions"),
     _s("supported", "scalar YAML edit, one file, distinct approver"),
     _s("supported", "related policy/workflow/workbook text is listed as manual/unknown"),
     [("app/routing.py", "config.standard_manager_threshold_jpy")], [T_ROUTE, T_APPLY, T_PERM], None),
    ("capex", "CAPEX finance threshold", ["config/approval.yaml routing_rules.capex_finance_threshold_jpy"],
     _s("supported"), _s("supported", "also changes EMERGENCY-process CAPEX rows, because emergency.py reads this key"),
     _s("supported", "scalar YAML edit"), _s("supported"),
     [("app/routing.py", "config.capex_finance_threshold_jpy"), ("app/emergency.py", "config.capex_finance_threshold_jpy")], [T_CAPEX], None),
    ("tol", "Invoice price tolerance (fixed JPY, percent)", ["config/approval.yaml matching_rules.price_tolerance_fixed_jpy / price_tolerance_percent"],
     _s("supported"), _s("supported", "Harbor `match` over 120 public invoices"), _s("supported", "scalar YAML edit"),
     _s("supported", "invoice_matching_manual.md is manual"),
     [("app/matching.py", "config.price_tolerance_fixed_jpy"), ("app/matching.py", "config.price_tolerance_percent")], [T_MATCH, T_UIMATCH, T_NOCHG], None),
    ("terms", "Payment terms (days)", ["config/approval.yaml matching_rules.payment_term_days"],
     _s("supported"), _s("not-controlled", "config does not control this behaviour: Harbor parses the key, no module reads it"),
     _s("final", "a scalar edit is accepted but changes no behaviour; real payment terms = code change"),
     _s("supported", "docs are manual"),
     [("app/calculations.py", "timedelta(days=30)"), ("app/config_loader.py", "payment_term_days")], [T_TERMS, T_UIMATCH], "payment_terms"),
    ("emerg", "Emergency purchase approval threshold", ["app/emergency.py", "docs/emergency_purchases.md"],
     _s("supported", "rows shown in the route simulation; not claimed"), _s("not-controlled", "config does not control this behaviour: hard-coded literal"),
     _s("final", "needs a Python change (planned for Final)"), _s("supported", "analysis only"),
     [("app/emergency.py", "net_amount_jpy >= 1_000_000")], [T_EMERG], "emergency"),
    ("wf", "workflow.yaml step thresholds", ["config/workflow.yaml"],
     _s("supported"), _s("not-supported", "Harbor's CLI does not read workflow.yaml"), _s("manual", "reported UNADDRESSED; a person edits it"),
     _s("supported"), [], [], "workflow"),
    ("md", "Policy / SOP / guide Markdown", ["docs/*.md"],
     _s("supported", "chunks, evidence"), _s("not-supported"), _s("manual", "text edits are manual by design"), _s("supported"), [], [], None),
    ("xlsx", "Workbook cell (approval_matrix.xlsx)", ["data/approval_matrix.xlsx"],
     _s("supported"), _s("not-supported"), _s("unknown", "a typed workbook edit exists in code but is not rehearsed here"), _s("supported"), [], [], None),
    ("py", "Python code of Harbor", ["app/*.py"],
     _s("supported", "very thin observations"), _s("not-supported"), _s("final", "GenAI-proposed code change -> validated patch -> tests -> approval -> deploy: planned for Final"),
     _s("supported", "read for references only"), [], [], None),
    ("data", "CSV / JSONL transaction data", ["data/*.csv", "data/*.jsonl"],
     _s("not-supported", "not imported into the workspace"), _s("supported", "used only as the fixed public input of the simulation"),
     _s("not-supported"), _s("not-supported"), [], [T_ROUTE, T_MATCH], None),
    ("html", "HTML invoice/receipt forms", ["forms/**/*.html"],
     _s("not-supported", "format not admitted"), _s("not-supported"), _s("not-supported"), _s("not-supported"), [], [], None),
    ("free", "Any other free-text change", ["(no matching row)"],
     _s("unknown"), _s("not-supported"), _s("not-supported", "no automatic edit"), _s("manual", "the draft lists what it cannot cover; GAP questions ask you"), [], [T_GAP], None),
]

CAPS = ("view", "simulate", "apply", "analyse")


def _trace(kind: str, files: dict) -> list[dict]:
    """Extra code traces that back a 'not-controlled'/'not-supported' statement."""
    out = []
    if kind == "emergency":
        reads = harbor_sim._consumers(files, "standard_manager_threshold_jpy", "app/emergency.py")
        out.append({"claim": "app/emergency.py never reads standard_manager_threshold_jpy", "matches": len(reads)})
    if kind == "payment_terms":
        out.append({"claim": "no module outside the loader/model reads payment_term_days", "matches": len(harbor_sim._consumers(files, "payment_term_days"))})
    if kind == "workflow":
        n = sum(1 for rel, c in files.items() if rel.startswith("app/") and rel.endswith(".py") and b"workflow" in c.lower())
        out.append({"claim": "no Harbor module mentions 'workflow'", "matches": n})
    return out


def build_matrix() -> dict:
    try:
        files = harbor_sim.load_fixture()
    except harbor_sim.HarborSimError as exc:
        files, err = {}, str(exc)
    else:
        err = None
    rows = []
    for rid, title, where, view, sim, apply_, analyse, refs, tests, trace in ROWS:
        evidence = []
        for file, needle in refs:
            hit = harbor_sim._find(files[file].decode("utf-8"), needle, file) if file in files else None
            if hit:
                evidence.append(hit)
        rows.append({"id": rid, "title": title, "where": where,
                     "view": view, "simulate": sim, "apply": apply_, "analyse": analyse,
                     "code_evidence": evidence, "traces": _trace(trace, files) if trace and files else [], "tests": tests,
                     "_refs": len(refs)})
    return {"rows": rows, "fixture_error": err, "legend": {
        "supported": "exists and a named test proves it", "manual": "a person does it", "final": "needs a code change; planned for Final",
        "not-controlled": "the config does not control this behaviour (code evidence shown)", "not-supported": "not available",
        "unknown": "not traced or not rehearsed: nothing is claimed"},
        "isolation": "Simulations run only the hash-pinned public Harbor copy in a resource-limited runner that is NOT a security boundary; "
                     "no uploaded or model-written code is executed.",
        "ai": "GenAI outputs on the local rehearsal build are SIMULATED; the Harbor simulation is a real deterministic run (no model call)."}
