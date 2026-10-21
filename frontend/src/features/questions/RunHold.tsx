// F-ACC-2 (2026-10-21): why an AI run is not running yet, who may allow it and where, read from the server's `hold` (infra/adapters/v2_job_hold.py: the rules of
// POST /v2/jobs/{id}/approve and the sandbox lease guard). The "Allow this AI run" control is shown only when the server says this person may allow it; the route
// re-checks every rule when the person acts, and a refusal is shown as the server worded it. Nothing is allowed automatically.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted } from "@/components/bits";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const ROLE: Record<string, string> = { admin: "administrator", requester: "requester", change_approver: "change approver", knowledge_owner: "knowledge owner",
  reviewer: "reviewer", viewer: "viewer", judge: "judge", restore_operator: "restore operator" };
const WHY: Record<string, string> = {
  requested_by_you: "You asked for this run, so a different person has to allow it.",
  no_permission: "Your role cannot allow this kind of AI run.",
  not_readable: "You cannot see this run, so you cannot allow it.",
  lease_required: "Take the writing session on the Sandbox page first; then you can allow it.",
  unsupported_kind: "This kind of run cannot be allowed from here.",
};

function refusal(e: unknown): string {
  if (e instanceof ApiError) {
    const msg = typeof e.body?.error === "string" ? e.body.error : "";
    if (e.status === 409 && e.body?.code === "sandbox_lease_required") return "Take the writing session on the Sandbox page first. Nothing was allowed.";
    if (e.status === 403) return `Not allowed: ${msg || "your current role or the workspace policy does not permit it"}. Nothing was allowed.`;
    if (e.status === 409 || e.status === 404) return `The run could not be allowed: ${msg || "it changed or is no longer visible"}. Nothing was allowed.`;
  }
  return "The run could not be allowed. Nothing was allowed.";
}

/** Allow one run through the existing route; `onDone` reloads the owner's view. Errors stay next to the control. */
function AllowButton({ ws, jobId, onDone, testid }: { ws: string; jobId: string; onDone: () => void | Promise<void>; testid: string }) {
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  return (
    <div className="grid gap-1">
      <div><Button size="sm" variant="primary" data-testid={testid} disabled={busy} onClick={async () => {
        setBusy(true); setErr(null);
        try { await api("POST", `/v2/jobs/${q(jobId)}/approve`, { workspace_id: ws }); await onDone(); }
        catch (e) { setErr(refusal(e)); }
        finally { setBusy(false); }
      }}>Allow this AI run</Button></div>
      {err && <Callout tone="danger" testid={`${testid}-error`}>{err}</Callout>}
    </div>
  );
}

/** The waiting state of one business question / change request run. `status` is the DTO status; `hold` the server's description. */
export function RunHold({ ws, status, hold, onChanged }: { ws: string; status: string; hold: Rec | undefined; onChanged: () => void | Promise<void> }) {
  if (status === "waiting_for_slot") return <div data-testid="run-hold" data-state="waiting_for_slot"><Callout tone="wait">Allowed. Waiting for a free worker slot; it starts by itself.</Callout></div>;
  if (status === "running") return <div data-testid="run-hold" data-state="running"><Callout tone="wait">Working on it...</Callout></div>;
  if (status !== "waiting_for_approval" || !hold) return null;
  const roles = ((hold.allowed_roles ?? []) as string[]).map((r) => ROLE[r] ?? r).join(", ");
  const why = hold.why_not ? WHY[String(hold.why_not)] ?? String(hold.why_not) : null;
  return (
    <div data-testid="run-hold" data-state="waiting_for_approval" data-may={String(Boolean(hold.you_may_allow))}><Callout tone="wait">
      <span className="grid gap-1">
        <span>{hold.reason === "sandbox_second_person"
          ? "Waiting to start: in this shared trial workspace a second person has to allow every AI run."
          : "Waiting to start: this AI run needs a person's approval."}</span>
        <span data-testid="run-hold-who">Who may allow it: a member{roles ? ` with the ${roles} role` : ""}{hold.distinct_required ? ", other than the person who asked" : ""}.</span>
        <span data-testid="run-hold-where">Where: {String(hold.where)}.</span>
        {hold.you_may_allow
          ? <AllowButton ws={ws} jobId={String(hold.job_id)} onDone={onChanged} testid="run-hold-allow" />
          : why && <Muted testid="run-hold-why">{why}</Muted>}
      </span>
    </Callout></div>
  );
}

/** Runs waiting for approval that THIS person may allow now (GET /v2/ai-runs/awaiting-approval). Hidden when there are none. */
export function AwaitingRuns({ ws }: { ws: string }) {
  const [runs, setRuns] = React.useState<Rec[] | null>(null);
  const load = React.useCallback(async () => {
    try { setRuns(((await api<Rec>("GET", `/v2/ai-runs/awaiting-approval?workspace_id=${q(ws)}`)).runs ?? []) as Rec[]); } catch { setRuns(null); }
  }, [ws]);
  React.useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 10000);                         // read-only; a person allows each run explicitly
    return () => clearInterval(t);
  }, [load]);
  if (!runs || runs.length === 0) return null;
  return (
    <section aria-labelledby="ai-runs-awaiting-h" data-testid="ai-runs-awaiting" data-count={runs.length} className="rounded-lg border border-border bg-surface p-4 grid gap-3">
      <h2 id="ai-runs-awaiting-h" className="text-base font-semibold">AI runs waiting for a second person</h2>
      <Muted>Each run was asked for by someone else. Allowing it lets one simulated or real model call run under the workspace's limits; it decides nothing.</Muted>
      <ul className="grid gap-2">
        {runs.map((r) => (
          <li key={String(r.job_id)} data-testid="ai-run-awaiting" data-job={String(r.job_id)} className="grid gap-1">
            <span className="text-sm">{r.kind === "knowledge.qa" ? "Business question" : r.kind === "change.understand" ? "Change request" : String(r.kind)} from {String(r.requested_by)}{r.label ? `: "${String(r.label)}"` : ""}</span>
            <AllowButton ws={ws} jobId={String(r.job_id)} onDone={load} testid="ai-run-allow" />
          </li>
        ))}
      </ul>
    </section>
  );
}
