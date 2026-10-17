"""Per-role canary gate for live-local (read-only; no provider call). Each ROLE that is about to be used has its own prompt, schema and model, so the extraction canary
is never evidence for another role: a role is released only by an attempt of ITS OWN job kind.

For one role the first settled attempt must show (from `v2_job_attempts` and `v2_job_cost_ledger`, the product's own records):
  model     the model recorded for the attempt equals the model configured for that role (no silent fallback to the default model)
  schema    the attempt recorded a prompt hash and a schema hash and succeeded (the executor validated the output against its schema)
  usage     input and output tokens are present and within the configured ceilings (a missing usage is a FAIL: it would be priced as the worst case)
  cost      the booked cost does not exceed the attempt's own worst-case estimate (`attempts_over_estimate` for that job is empty)
  ledger    the job's ledger row is SETTLED and equals the sum of its attempt costs
The endpoint is NOT observable per attempt: it is operator-declared in the launch configuration and recorded in the `live_local.start` audit row (extraction additionally records the
served model version in its result). The gate says so instead of claiming it.
Nothing here changes a prompt: a failing canary is stored as a finding, the run is kept, and a fix is a new snapshot with a reason.
"""
from __future__ import annotations

from decimal import Decimal

#: product job kind -> configured role (the budget config names roles, the ledger names kinds)
ROLE_OF_KIND = {"extraction": "extraction", "change.draft": "change.draft", "intent.interpret": "intent.interpret", "gap.answer": "gap.answer",
                "impact.judge": "impact.judge", "explain": "intent.explain"}


def evaluate_attempt(row: dict, config, role: str, ledger: dict | None) -> list[str]:
    """Findings for one attempt (empty = pass). `row`: model_id, outcome, prompt_hash, schema_hash, input_tokens, output_tokens, cost_usd, cost_estimate_usd."""
    out = []
    want = config.role_model_ids.get(role) or config.default_model_id
    if row.get("model_id") != want:
        out.append(f"model {row.get('model_id')!r} is not the configured model {want!r} for role {role}")
    if row.get("outcome") not in ("COMPLETED", "SUCCEEDED", "success"):
        out.append(f"attempt outcome is {row.get('outcome')!r}, not a success")
    if not row.get("prompt_hash") or not row.get("schema_hash"):
        out.append("prompt hash or schema hash not recorded")
    tin, tout = row.get("input_tokens"), row.get("output_tokens")
    if tin is None or tout is None:
        out.append("usage missing (it would be priced at the worst case)")
    else:
        if tin > config.max_input_tokens_per_call:
            out.append(f"input tokens {tin} above the ceiling {config.max_input_tokens_per_call}")
        if tout > config.max_output_tokens:
            out.append(f"output tokens {tout} above the ceiling {config.max_output_tokens}")
    est = row.get("cost_estimate_usd")
    if est is not None and Decimal(str(row.get("cost_usd") or 0)) > Decimal(str(est)):
        out.append("booked cost above the attempt's own worst-case estimate")
    if ledger is None or ledger.get("entry_state") != "SETTLED":
        out.append("ledger row is not SETTLED")
    elif Decimal(str(ledger["cost_usd"])) != Decimal(str(row.get("cost_usd") or 0)):
        out.append("ledger cost differs from the attempt cost (a retry would need the sum: compare all attempts of the job)")
    return out


def report(attempts: list[dict], ledgers: dict, config, roles_in_use: list[str]) -> dict:
    """role -> {"status": PASS | FAIL | NOT_OBSERVED, "findings": [...], "job_id": ...} using the FIRST attempt of that role's kind. A role not observed yet stays closed."""
    by_role: dict[str, list[dict]] = {}
    for a in attempts:
        role = ROLE_OF_KIND.get(a.get("kind"))
        if role:
            by_role.setdefault(role, []).append(a)
    out = {}
    for role in roles_in_use:
        rows = sorted(by_role.get(role, []), key=lambda r: str(r.get("recorded_at") or r.get("job_id")))
        if not rows:
            out[role] = {"status": "NOT_OBSERVED", "findings": [], "job_id": None}
            continue
        first = rows[0]
        # the job's ledger must equal the SUM of all its attempts (a retry is a second attempt of the same job)
        job_rows = [r for r in rows if r["job_id"] == first["job_id"]]
        total = sum((Decimal(str(r.get("cost_usd") or 0)) for r in job_rows), Decimal(0))
        led = ledgers.get(first["job_id"])
        merged = {**first, "cost_usd": total}
        findings = evaluate_attempt(merged, config, role, led)
        for r in job_rows[1:]:
            findings += [f for f in evaluate_attempt({**r, "cost_usd": 0}, config, role, {"entry_state": "SETTLED", "cost_usd": 0}) if "ledger" not in f and "booked" not in f]
        out[role] = {"status": "FAIL" if findings else "PASS", "findings": findings, "job_id": first["job_id"]}
    return out


def read(dsn: str, tenant_id: str, workspace_id: str) -> tuple[list[dict], dict]:
    """Read-only SELECTs of the product's own tables (attempts joined to the job kind, and the ledger)."""
    import psycopg2
    with psycopg2.connect(dsn) as cx, cx.cursor() as cur:
        cur.execute("""SELECT a.job_id, j.kind, a.outcome, a.model_id, a.prompt_hash, a.schema_hash, a.input_tokens, a.output_tokens, a.cost_estimate_usd, a.cost_usd, a.recorded_at
                         FROM v2_job_attempts a JOIN v2_jobs j USING (tenant_id, workspace_id, job_id)
                        WHERE a.tenant_id=%s AND a.workspace_id=%s ORDER BY a.recorded_at""", (tenant_id, workspace_id))
        cols = ("job_id", "kind", "outcome", "model_id", "prompt_hash", "schema_hash", "input_tokens", "output_tokens", "cost_estimate_usd", "cost_usd", "recorded_at")
        attempts = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("SELECT job_id, entry_state, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s", (tenant_id, workspace_id))
        ledgers = {r[0]: {"entry_state": r[1], "cost_usd": r[2]} for r in cur.fetchall()}
    return attempts, ledgers
