"""Real before/after simulation of Harbor behaviour, adapter "harbor_cli" of the existing `simulation` job.

Cases (each a Harbor CLI command over PUBLIC sample data):
  route  - `route`  over 240 requisitions : routing_rules.standard_manager_threshold_jpy, routing_rules.capex_finance_threshold_jpy
  match  - `match`  over 120 invoices     : matching_rules.price_tolerance_fixed_jpy, price_tolerance_percent, payment_term_days

What runs: the pinned, hash-verified copy of the PUBLIC Harbor package (fixtures/harbor_public, MANIFEST.json) executing Harbor's
own CLI once with the sandbox revision's current approval.yaml (baseline) and once with that config plus the proposed scalar
edit(s). What does NOT run: any code from a workspace/upload or written by a model. The only workspace-derived inputs are DATA:
the approval.yaml bytes (parsed with yaml.safe_load, allowlisted keys, bounded numbers, then re-validated by Harbor's own loader
inside the runner).

Integrity gate: before any before/after claim, the SEED config must reproduce Harbor's shipped output for the case exactly
(`run_output/routes.jsonl`, `run_output/invoice_decisions.jsonl`); otherwise ADAPTER_INTEGRITY_FAILED and nothing is claimed.

Inert edits: an edit that changes no row is NEVER called inert by itself. A key is reported INERT only when a code trace proves
that no module reads it (a scan of the pinned app/*.py for the attribute outside the config loader/model finds nothing; for
`payment_term_days` the due date is a hard-coded +30 days in app/calculations.py). Any other zero-change edit is reported as
"no change in this sample" together with the code line that DOES consume the key.

Isolation statement: `infra/worker/sandbox_runner.py` is a resource-limited separate process (wall clock, output cap, scratch
directory, read-only inputs with before/after hashes, best-effort memory limit). It is NOT a security boundary and is used only
because the executed code is the pinned public Harbor copy. Untrusted or model-written code must never be passed to it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib

import yaml

from infra.worker import sandbox_runner
from infra.worker.sandbox_config import SandboxConfig

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "harbor_public"
CONFIG_PATH_SUFFIX = "config/approval.yaml"
_ROUTE_KEYS = {"routing_rules.standard_manager_threshold_jpy": (1, 1_000_000_000),
               "routing_rules.capex_finance_threshold_jpy": (1, 1_000_000_000)}
_MATCH_KEYS = {"matching_rules.price_tolerance_fixed_jpy": (0, 1_000_000_000),
               "matching_rules.price_tolerance_percent": (0, 100),
               "matching_rules.payment_term_days": (1, 365)}
#: case -> edit path -> (min, max) inclusive. Scalar integers only; anything else is refused before any run.
ALLOWED_EDITS = {"route": _ROUTE_KEYS, "match": _MATCH_KEYS}
MAX_CHANGED_ROWS = 200
_RUN_CFG = SandboxConfig(cpu_seconds=20, wall_seconds=30, memory_mb=512, output_bytes=262144, allow_network=False)

_CASE_FILES = {
    "route": ("data/requisitions.jsonl", "data/suppliers.csv", "data/employees.csv"),
    "match": ("data/purchase_orders.jsonl", "data/receipts.jsonl", "data/invoices.jsonl"),
}
_SHIPPED = {"route": "run_output/routes.jsonl", "match": "run_output/invoice_decisions.jsonl"}
_ID_KEY = {"route": "request_id", "match": "invoice_id"}


_ATTR = {"routing_rules.standard_manager_threshold_jpy": "standard_manager_threshold_jpy",
         "routing_rules.capex_finance_threshold_jpy": "capex_finance_threshold_jpy",
         "matching_rules.price_tolerance_fixed_jpy": "price_tolerance_fixed_jpy",
         "matching_rules.price_tolerance_percent": "price_tolerance_percent",
         "matching_rules.payment_term_days": "payment_term_days"}
_LOADER_FILES = ("app/models.py", "app/config_loader.py")


def _consumers(files: dict[str, bytes], attr: str, only: str | None = None) -> list[dict]:
    """Trace: where the pinned Harbor modules READ this config attribute (the model/loader only define and fill it)."""
    import re
    hits = []
    for rel in sorted(files):
        if not (rel.startswith("app/") and rel.endswith(".py")) or rel in _LOADER_FILES or (only and rel != only):
            continue
        for i, line in enumerate(files[rel].decode("utf-8").splitlines(), 1):
            if re.search(r"\b" + re.escape(attr) + r"\b", line):
                hits.append({"file": rel, "line": i, "text": line.strip()})
    return hits


class HarborSimError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# The program run inside the runner (fixed, repo-authored). `handle(case, config)` is the runner's own contract; the single
# dummy case carries the run name, the real work is Harbor's CLI over the pinned files copied into inputs/.
_CANDIDATE = r'''
import json, os, sys


def handle(case, config):
    work = os.path.join(SCRATCH_DIR, "work")
    for flat, rel in json.load(open(os.path.join(INPUTS_DIR, "layout.json"), "r", encoding="utf-8")).items():
        dest = os.path.join(work, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(os.path.join(INPUTS_DIR, flat), "rb") as src, open(dest, "wb") as out:
            out.write(src.read())
    sys.path.insert(0, work)
    from app import cli
    out_path = os.path.join(SCRATCH_DIR, "out.jsonl")
    cfg = os.path.join(work, "config", "run.yaml")
    if config["case"] == "route":
        args = ["route", "--requests", os.path.join(work, "data", "requisitions.jsonl"), "--config", cfg, "--output", out_path]
    else:
        args = ["match", "--data-dir", os.path.join(work, "data"), "--config", cfg, "--output", out_path]
    rc = cli.main(args)
    with open(out_path, "r", encoding="utf-8") as fh:
        return json.dumps({"rc": rc, "rows": fh.read()})
'''


def load_fixture() -> dict[str, bytes]:
    """The pinned files, verified against MANIFEST.json. Any drift/missing file refuses to run."""
    try:
        manifest = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise HarborSimError("fixture_missing", str(exc)) from exc
    files = {}
    for rel, digest in manifest["files"].items():
        try:
            content = (FIXTURE / rel).read_bytes()
        except OSError as exc:
            raise HarborSimError("fixture_missing", rel) from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise HarborSimError("fixture_hash_mismatch", rel)
        files[rel] = content
    return files


def validate_payload(payload) -> str | None:
    if isinstance(payload, dict) and payload.get("from_draft_job_id") is not None:
        # server-built form: the edits come from the stored draft, never from the client
        if not isinstance(payload["from_draft_job_id"], str) or not payload["from_draft_job_id"]:
            return "from_draft_job_id_must_be_a_non_empty_string"
        if "edits" in payload or "case" in payload:
            return "edits_and_case_are_built_by_the_server_from_the_draft"
        return None
    if not isinstance(payload, dict) or payload.get("case") not in ALLOWED_EDITS:
        return "case_must_be_route_or_match"
    allowed = ALLOWED_EDITS[payload["case"]]
    edits = payload.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 3:
        return "edits_required_1_to_3"
    seen = set()
    for e in edits:
        if not isinstance(e, dict) or e.get("path") not in allowed or e["path"] in seen:
            return "edit_path_not_allowed"
        seen.add(e["path"])
        lo, hi = allowed[e["path"]]
        v = e.get("value")
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return "edit_value_must_be_a_bounded_integer"
        ec = e.get("expected_current")
        if ec is not None and (isinstance(ec, bool) or not isinstance(ec, int)):
            return "expected_current_must_be_an_integer"
    return None


def _get(d: dict, dotted: str, default=...):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            if default is not ...:
                return default
            raise HarborSimError("config_key_missing", dotted)
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    *parents, last = dotted.split(".")
    cur = d
    for part in parents:
        cur = cur.setdefault(part, {})
    cur[last] = value


def _validate_config(doc, where: str) -> dict:
    if not isinstance(doc, dict):
        raise HarborSimError("config_invalid", f"{where}: root is not a mapping")
    for key, (lo, hi) in _ROUTE_KEYS.items():                    # Harbor's loader requires both routing thresholds
        v = _get(doc, key)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise HarborSimError("config_invalid", f"{where}: {key} must be a bounded integer")
    for key, (lo, hi) in _MATCH_KEYS.items():                    # optional in Harbor (defaults); bounded when present
        v = _get(doc, key, None)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            raise HarborSimError("config_invalid", f"{where}: {key} must be a bounded integer")
    return doc


def _run(files: dict[str, bytes], case: str, config_yaml: bytes, run_name: str):
    """One Harbor CLI run in the runner -> (rows, meta). Raises HarborSimError if the runner did not EXECUTE."""
    layout, inputs = {}, {}
    for rel, content in files.items():
        if rel.startswith("app/") or rel in _CASE_FILES[case]:
            flat = "f%03d.bin" % len(layout)
            layout[flat] = rel
            inputs[flat] = content
    layout["fcfg.bin"] = "config/run.yaml"
    inputs["fcfg.bin"] = config_yaml
    inputs["layout.json"] = json.dumps(layout).encode("utf-8")
    inputs["corpus.json"] = json.dumps([{"historical_case_id": run_name}]).encode("utf-8")
    inputs["config.json"] = json.dumps({"case": case}).encode("utf-8")
    # The child gets a minimal environment; give it only the directory that holds PyYAML (a library path, read-only use).
    lib = {"PYTHONPATH": str(pathlib.Path(yaml.__file__).resolve().parents[1])}
    res = sandbox_runner.run_isolated(candidate_source=_CANDIDATE, input_files=inputs, config=_RUN_CFG, extra_env=lib)
    if res.status != sandbox_runner.STATUS_EXECUTED or res.input_hashes_before != res.input_hashes_after:
        raise HarborSimError("runner_" + res.status.lower(), str(res.reason))
    outcomes = res.stdout_result.get("outcomes") or []
    if len(outcomes) != 1:
        raise HarborSimError("runner_no_output")
    out = json.loads(outcomes[0]["outcome"])
    if out.get("rc") != 0:
        raise HarborSimError("harbor_cli_failed", f"rc={out.get('rc')}")
    rows = [json.loads(line) for line in out["rows"].splitlines() if line.strip()]
    return rows, {"duration_seconds": round(res.duration_seconds, 3), "memory_limit_enforced": res.memory_limit_enforced,
                  "input_hash_unchanged": res.input_hashes_before == res.input_hashes_after}


def _find(text: str, needle: str, file: str):
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return {"file": file, "line": i, "text": line.strip()}
    return None


def _code_refs(files: dict[str, bytes]) -> dict:
    routing = files["app/routing.py"].decode("utf-8")
    matching = files["app/matching.py"].decode("utf-8")
    calc = files["app/calculations.py"].decode("utf-8")
    emerg = files["app/emergency.py"].decode("utf-8")
    refs = {
        "routing_rules.standard_manager_threshold_jpy": _find(routing, "config.standard_manager_threshold_jpy", "app/routing.py"),
        "routing_rules.capex_finance_threshold_jpy": _find(routing, "config.capex_finance_threshold_jpy", "app/routing.py"),
        "matching_rules.price_tolerance_fixed_jpy": _find(matching, "config.price_tolerance_fixed_jpy", "app/matching.py"),
        "matching_rules.price_tolerance_percent": _find(matching, "config.price_tolerance_percent", "app/matching.py"),
        "emergency_hardcoded_threshold": _find(emerg, "net_amount_jpy >= 1_000_000", "app/emergency.py"),
        "emergency_capex_read": _find(emerg, "config.capex_finance_threshold_jpy", "app/emergency.py"),
        "payment_term_hardcoded": _find(calc, "timedelta(days=30)", "app/calculations.py"),
    }
    return {k: v for k, v in refs.items() if v}


def _signature(case: str, r: dict):
    return ((r["route"], r["reason_code"]) if case == "route"
            else (r["status"], r["reason_code"], r.get("due_date")))


def _attribute(case: str, b: dict, a: dict, edited: dict) -> list[str]:
    """Which EDITED keys can explain this changed row (by the reason codes / fields that changed)."""
    if case == "route":
        key = {"STANDARD_MANAGER_REQUIRED": "routing_rules.standard_manager_threshold_jpy",
               "CAPEX_HIGH_VALUE": "routing_rules.capex_finance_threshold_jpy"}
        keys = {key.get(b["reason_code"]), key.get(a["reason_code"])} - {None}
        return sorted(k for k in keys if k in edited)
    keys = set()
    if "PRICE_OUT_OF_TOLERANCE" in (b["reason_code"], a["reason_code"]):
        keys |= {"matching_rules.price_tolerance_fixed_jpy", "matching_rules.price_tolerance_percent"}
    if b.get("due_date") != a.get("due_date"):
        keys.add("matching_rules.payment_term_days")
    return sorted(k for k in keys if k in edited)


def simulate(case: str, *, baseline_yaml: bytes | None, baseline_ref: dict | None, edits: list[dict]) -> dict:
    if case not in ALLOWED_EDITS:
        raise HarborSimError("case_not_supported", str(case))
    files = load_fixture()
    seed_doc = _validate_config(yaml.safe_load(files["config/approval.yaml"]), "fixture seed")
    if baseline_yaml is None:
        base_doc, baseline_ref = copy.deepcopy(seed_doc), {"kind": "fixture_seed", "note": "workspace has no approval.yaml"}
    else:
        base_doc = _validate_config(yaml.safe_load(baseline_yaml), "workspace approval.yaml")
    prop_doc = copy.deepcopy(base_doc)
    applied = []
    for e in edits:
        current = _get(base_doc, e["path"], None)
        if e.get("expected_current") is not None and current != e["expected_current"]:
            raise HarborSimError("stale_baseline", f"{e['path']} is {current}, the request states {e['expected_current']}")
        _set(prop_doc, e["path"], e["value"])
        applied.append({"path": e["path"], "from": current, "to": e["value"], "no_op": current == e["value"]})
    _validate_config(prop_doc, "proposed config")
    dump = lambda d: yaml.safe_dump(d, sort_keys=False).encode("utf-8")
    # integrity gate: the seed config must reproduce Harbor's shipped output exactly
    seed_rows, seed_run = _run(files, case, dump(seed_doc), "seed")
    shipped = [json.loads(x) for x in files[_SHIPPED[case]].decode("utf-8").splitlines() if x.strip()]
    integrity = {"seed_equals_shipped": seed_rows == shipped, "rows": len(shipped), "shipped_file": _SHIPPED[case]}
    if not integrity["seed_equals_shipped"]:
        raise HarborSimError("adapter_integrity_failed", f"the seed config did not reproduce Harbor's shipped {_SHIPPED[case]}")
    base_rows, base_run = (seed_rows, seed_run) if base_doc == seed_doc else _run(files, case, dump(base_doc), "baseline")
    prop_rows, prop_run = (base_rows, base_run) if prop_doc == base_doc else _run(files, case, dump(prop_doc), "proposed")

    refs = _code_refs(files)
    manifest_sha = hashlib.sha256((FIXTURE / "MANIFEST.json").read_bytes()).hexdigest()
    combo = lambda names: hashlib.sha256("".join(f"{n}:{hashlib.sha256(files[n]).hexdigest()};" for n in sorted(names)).encode()).hexdigest()
    snapshot = {"fixture_manifest_sha256": manifest_sha,
                "code_sha256": combo([n for n in files if n.startswith("app/")]),
                "input_sha256": combo(_CASE_FILES[case]), "input_files": list(_CASE_FILES[case]),
                "config_baseline_sha256": hashlib.sha256(dump(base_doc)).hexdigest(),
                "config_proposed_sha256": hashlib.sha256(dump(prop_doc)).hexdigest(),
                "differs_only_in": [{"path": a["path"], "from": a["from"], "to": a["to"]} for a in applied if not a["no_op"]],
                "statement": "baseline and proposed runs used the same pinned Harbor code, the same input files and the same runner "
                             "limits; only the validated config values listed here differ"}
    idk = _ID_KEY[case]
    if case == "route":
        subjects = {r["request_id"]: r for r in (json.loads(x) for x in files["data/requisitions.jsonl"].decode("utf-8").splitlines() if x.strip())}
    else:
        subjects = {r["invoice_id"]: r for r in (json.loads(x) for x in files["data/invoices.jsonl"].decode("utf-8").splitlines() if x.strip())}
    before = {r[idk]: r for r in base_rows}
    after = {r[idk]: r for r in prop_rows}
    edited = {a["path"]: a for a in applied if not a["no_op"]}
    outcomes, changed, unexplained = [], [], 0
    for rid in sorted(before):
        b, a = before[rid], after.get(rid)
        ch = a is not None and _signature(case, b) != _signature(case, a)
        sb, sa = _signature(case, b), (_signature(case, a) if a else None)
        outcomes.append({"historical_case_id": rid, "currency": "JPY", "changed": ch,
                         "current_outcome": ":".join(str(x) for x in sb if x is not None),
                         "shadow_outcome": ":".join(str(x) for x in sa if x is not None) if sa else None})
        if not ch:
            continue
        cause_keys = _attribute(case, b, a, edited)
        if not cause_keys:
            unexplained += 1
        sub = subjects.get(rid, {})
        emergency_row = case == "route" and sub.get("process") == "EMERGENCY"
        row = {"id": rid, "explained": bool(cause_keys),
               "cause": [{"config_key": k, "from": edited[k]["from"], "to": edited[k]["to"],
                          "code_ref": (refs.get("emergency_capex_read") if emergency_row and k == "routing_rules.capex_finance_threshold_jpy" and refs.get("emergency_capex_read")
                                       else refs.get(k))}      # the line that ran for THIS row: an emergency requisition goes through app/emergency.py, not app/routing.py
                         for k in cause_keys]}
        if case == "route":
            row.update(request_id=rid, amount_jpy=sub.get("net_amount_jpy"), category=sub.get("category"),
                       process=sub.get("process"), supplier_id=sub.get("supplier_id"),
                       label=f"{sub.get('category')} / {sub.get('process')}",
                       before={"route": b["route"], "reason": b["reason_code"], "outcome": b["route"]},
                       after={"route": a["route"], "reason": a["reason_code"], "outcome": a["route"]})
        else:
            det = b.get("comparison_details") or {}
            row.update(invoice_id=rid, amount_jpy=sub.get("goods_net_jpy"), supplier_id=sub.get("supplier_id"), process="INVOICE",
                       price_difference_jpy=det.get("price_difference_jpy"),
                       label=f"PO {sub.get('po_id')}, price diff {det.get('price_difference_jpy')}",
                       before={"status": b["status"], "reason": b["reason_code"], "due_date": b.get("due_date"), "outcome": b["status"]},
                       after={"status": a["status"], "reason": a["reason_code"], "due_date": a.get("due_date"), "outcome": a["status"]})
        changed.append(row)

    inert, no_change, not_controlled = [], [], []
    for ed in applied:
        if ed["no_op"] or any(c["config_key"] == ed["path"] for row in changed for c in row["cause"]):
            continue
        consumers = _consumers(files, _ATTR[ed["path"]])
        if not consumers:                                    # TRACED: no module reads the key
            entry = {"path": ed["path"], "from": ed["from"], "to": ed["to"], "consumers": [],
                     "reason": "config does not control this behaviour: Harbor parses the key but no module reads it (trace: 0 reads "
                               "of %s in app/*.py outside the loader/model)" % _ATTR[ed["path"]]}
            if ed["path"] == "matching_rules.payment_term_days" and refs.get("payment_term_hardcoded"):
                entry["reason"] += "; the due date uses a hard-coded 30 days"
                entry["code_ref"] = refs["payment_term_hardcoded"]
            inert.append(entry)
        else:                                                # consumed, but this sample never crosses the new value
            no_change.append({"path": ed["path"], "from": ed["from"], "to": ed["to"], "consumers": consumers[:3],
                              "reason": "no row of this sample changes; the key IS read by the code (not inert)"})
    if case == "route" and "routing_rules.standard_manager_threshold_jpy" in edited:
        emerg = refs.get("emergency_hardcoded_threshold")
        reads = _consumers(files, "standard_manager_threshold_jpy", "app/emergency.py")
        if emerg and not reads:
            not_controlled.append({"behaviour": "emergency manager approval threshold", "key": "routing_rules.standard_manager_threshold_jpy",
                                   "reason": "config does not control this behaviour",
                                   "evidence": emerg, "proof": "app/emergency.py never reads standard_manager_threshold_jpy (trace: 0 "
                                                               "matches); it compares the amount with a literal 1,000,000"})

    count = lambda rows, f: {k: sum(1 for r in rows if f(r) == k) for k in sorted({f(r) for r in rows})}
    pick = (lambda r: r["route"]) if case == "route" else (lambda r: r["status"])
    limits = ["Only Harbor's own CLI `%s` over the %d public %s was run; the result describes this sample under the proposed "
              "config, it is not a prediction for real orders." % (case, len(before), "requisitions" if case == "route" else "invoices")]
    if case == "route":
        limits.append("Emergency rows are shown but emergency is NOT in the support matrix.")
        if refs.get("emergency_hardcoded_threshold"):
            e = refs["emergency_hardcoded_threshold"]
            limits.append("Emergency routing uses a HARD-CODED 1,000,000 at %s:%d (%s): editing the manager threshold in approval.yaml "
                          "cannot change it; that would be a code change (planned for Final, not a Pitch capability)." % (e["file"], e["line"], e["text"]))
            c = refs.get("emergency_capex_read")
            n_em = sum(1 for r in changed if r.get("process") == "EMERGENCY")
            if c and "routing_rules.capex_finance_threshold_jpy" in edited:
                # the CAPEX rule is applied BEFORE the literal (emergency.py: CAPEX check first, then the 1,000,000 line), so a CAPEX threshold edit DOES move emergency CAPEX rows
                limits.append("Emergency requisitions in the CAPEX category are first checked against routing_rules.capex_finance_threshold_jpy at %s:%d (%s), BEFORE the hard-coded "
                              "1,000,000 line: editing the CAPEX threshold does change those rows (%d of the %d changed rows here are EMERGENCY). Emergency requisitions of other categories "
                              "are decided only by the literal and are not changed by approval.yaml." % (c["file"], c["line"], c["text"], n_em, len(changed)))
        else:
            limits.append("Emergency thresholds were not traced.")
    limits.append("workflow.yaml, policy/guide text and the workbook are not read by this command: edits there are not simulated.")
    return {
        "status": "EXECUTED", "case": case, "integrity": integrity, "baseline_source": baseline_ref, "edits": applied,
        "transactions": len(before), "changed_count": len(changed), "unexplained_changes": unexplained,
        "by_route_before": count(base_rows, pick), "by_route_after": count(prop_rows, pick),
        "changed": changed[:MAX_CHANGED_ROWS], "changed_truncated": len(changed) > MAX_CHANGED_ROWS,
        "emergency_rows_changed": sum(1 for c in changed if c.get("process") == "EMERGENCY"),
        "inert_edits": inert, "no_change_edits": no_change, "not_controlled": not_controlled, "snapshot": snapshot,
        "source_refs": [{"file": CONFIG_PATH_SUFFIX, "key": a["path"]} for a in applied]
                       + [v for k, v in refs.items() if k in edited],
        "input_data": {"files": list(_CASE_FILES[case][:3]) if case == "match" else [_CASE_FILES[case][0]],
                       "rows": len(subjects), "note": "public Harbor sample, not production data"},
        "runner": {"seed": seed_run, "baseline": base_run, "proposed": prop_run,
                   "statement": "resource-limited separate process, NOT a security boundary; only the hash-pinned public Harbor "
                                "copy ran; no uploaded or model-written code was executed"},
        "limits": limits, "outcomes": outcomes,
    }


def simulate_route(*, baseline_yaml, baseline_ref, edits) -> dict:
    return simulate("route", baseline_yaml=baseline_yaml, baseline_ref=baseline_ref, edits=edits)


def simulate_match(*, baseline_yaml, baseline_ref, edits) -> dict:
    return simulate("match", baseline_yaml=baseline_yaml, baseline_ref=baseline_ref, edits=edits)


def build_from_draft(draft_record: dict) -> dict:
    """The simulation inputs for one stored draft, derived ONLY from its server-validated edits.
    -> {"families": {case: [edit,...]}, "untested": [{...}], "edit_hash": str}. A config edit is simulable when its key is in
    ALLOWED_EDITS and its value is a bounded integer; every other draft edit (manual, other file, other key) is listed as untested."""
    res = (draft_record.get("execution_result") or {}) if isinstance(draft_record, dict) else {}
    families: dict[str, list] = {}
    untested = []
    for e in res.get("edits") or []:
        pe = e.get("proposal_edit") if isinstance(e, dict) else None
        path = pe.get("path") if isinstance(pe, dict) else None
        case = next((c for c, keys in ALLOWED_EDITS.items() if path in keys), None)
        ok = (case is not None and str(e.get("logical_path") or "").endswith(CONFIG_PATH_SUFFIX)
              and isinstance(pe.get("proposed"), int) and not isinstance(pe.get("proposed"), bool))
        if not ok:
            untested.append({"logical_path": (e or {}).get("logical_path"), "path": path,
                             "reason": "no_supported_simulation_for_this_edit"})
            continue
        ec = pe.get("expected_current")
        families.setdefault(case, []).append({"path": path, "value": pe["proposed"],
                                              "expected_current": ec if isinstance(ec, int) and not isinstance(ec, bool) else None})
    edit_hash = hashlib.sha256(json.dumps({"families": families, "untested": untested}, sort_keys=True).encode()).hexdigest()
    return {"families": families, "untested": untested, "edit_hash": edit_hash}


def _execute_from_draft(ctx, payload: dict, pinned_input: dict) -> dict:
    store, tenant, ws = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"]
    draft_id = payload["from_draft_job_id"]
    base = {"executor_id": "simulation.run.v1", "adapter": "harbor_cli", "direction": "forward_requested_change",
            "built_from": {"draft_job_id": draft_id}, "pinned_input": pinned_input,
            "denominator_detail": {"excluded": [], "malformed": [], "duplicate": [], "unknown": []}, "outcomes": []}
    empty_cov = {"submitted": 0, "known": 0, "excluded": 0, "unknown": 0, "malformed": 0, "duplicate": 0, "by_currency": {}}
    try:
        rec = store.job_record(tenant, ws, draft_id)
    except Exception:
        rec = None
    if not rec or rec.get("kind") != "change.draft" or rec.get("execution_status") != "COMPLETED":
        return {**base, "status": "NOT_EXECUTABLE", "reason": "draft_not_found_or_not_completed", "coverage": empty_cov}
    built = build_from_draft(rec)
    # the simulation tests the draft on the revision the DRAFT was made against (its own stored pin), not on whatever the head is now
    draft_rev = ((rec.get("execution_result") or {}).get("pinned_input") or {}).get("pinned_source_revision_id")
    base["built_from"].update(edit_hash=built["edit_hash"], untested=built["untested"], baseline_source_revision_id=draft_rev,
                              head_source_revision_id_at_run=ctx.job.get("pinned_source_revision_id"))
    if not built["families"]:
        return {**base, "status": "NOT_EXECUTABLE", "reason": "no_supported_edit_in_draft", "coverage": empty_cov}
    runs = []
    for case in sorted(built["families"]):
        sub = {"adapter": "harbor_cli", "case": case, "edits": built["families"][case]}
        err = validate_payload(sub)
        if err:
            runs.append({"case": case, "status": "FAILED", "reason": err, "harbor": None, "outcomes": []})
            continue
        r = _execute_single(ctx, sub, pinned_input, baseline_rev=draft_rev)
        runs.append({"case": case, "status": r["status"], "reason": r["reason"], "harbor": r["harbor"], "outcomes": r["outcomes"]})
    ok = [r for r in runs if r["status"] == "EXECUTED"]
    status = "EXECUTED" if len(ok) == len(runs) else ("PARTIAL" if ok else runs[0]["status"])
    n = sum(len(r["outcomes"]) for r in ok)
    first = ok[0] if ok else runs[0]
    coverage = {"submitted": n, "known": n, "excluded": 0, "unknown": 0, "malformed": 0, "duplicate": 0,
                "by_currency": {"JPY": n} if n else {}}
    reasons = [f"{r['case']}: {r['reason']}" for r in runs if r["status"] != "EXECUTED"]
    return {**base, "status": status, "reason": "; ".join(reasons) or None, "coverage": coverage, "outcomes": first["outcomes"],
            "harbor": first["harbor"], "families": [{k: v for k, v in r.items() if k != "outcomes"} for r in runs],
            "untested_edits": built["untested"]}


def execute(ctx, payload: dict, pinned_input: dict) -> dict:
    """Executor hook (called by SimulationExecutor). Returns the full simulation result dict."""
    err = validate_payload(payload)
    if err:
        raise HarborSimError(err)
    if payload.get("from_draft_job_id") is not None:
        return _execute_from_draft(ctx, payload, pinned_input)
    return _execute_single(ctx, payload, pinned_input)


def _execute_single(ctx, payload: dict, pinned_input: dict, baseline_rev: str | None = None) -> dict:
    store, tenant, ws = ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"]
    rev = baseline_rev or ctx.job.get("pinned_source_revision_id")
    baseline_yaml, ref = None, None
    if rev:
        view = store.source_revision_view(tenant, ws, rev)
        art = next((a for a in view["artifacts"] if str(a.get("logical_path") or "").endswith(CONFIG_PATH_SUFFIX)), None)
        if art is not None:
            baseline_yaml = store.read_blob(tenant, ws, rev, art["artifact_id"])
            ref = {"kind": "workspace_revision", "logical_path": art["logical_path"], "source_revision_id": rev,
                   "artifact_id": art["artifact_id"], "blob_hash": art.get("blob_hash")}
    try:
        sim = simulate(payload["case"], baseline_yaml=baseline_yaml, baseline_ref=ref, edits=payload["edits"])
        reason = None
    except HarborSimError as exc:
        sim = {"status": {"stale_baseline": "STALE_BASELINE", "adapter_integrity_failed": "ADAPTER_INTEGRITY_FAILED"}.get(
            exc.code, "FAILED"), "case": payload["case"], "outcomes": []}
        reason = str(exc)
    n = len(sim.get("outcomes") or [])
    coverage = {"submitted": n, "known": n, "excluded": 0, "unknown": 0, "malformed": 0, "duplicate": 0,
                "by_currency": {"JPY": n} if n else {}}
    return {"executor_id": "simulation.run.v1", "adapter": "harbor_cli", "status": sim.pop("status"), "reason": reason,
            "coverage": coverage, "denominator_detail": {"excluded": [], "malformed": [], "duplicate": [], "unknown": []},
            "outcomes": sim.pop("outcomes", []), "harbor": sim, "pinned_input": pinned_input}
