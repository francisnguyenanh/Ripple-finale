// Conflict scan with AI (AIX-K1 / LHA-K1, SUR-01/02) over the current knowledge version. Reading shows what earlier scans found; a scan starts only when a person with
// `knowledge.confirm` and `ai.use` asks (paid model calls per rule group, stated next to the button). A hypothesis is a model's proposal: the server's evaluator checks its
// case, the compatibility rubric may downgrade it, a person dismisses it with a reason or answers through the existing questions. Nothing here is an approved conflict.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Muted, Tech, TextInput } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { aiJobBadge } from "@/lib/aiJob";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const KIND: Record<string, string> = { potential_conflict: "Possible conflict", exception_overlap: "Exception overlap", ambiguity: "Ambiguity", missing_context: "Missing context" };
const DISPOSITION: Record<string, string> = { as_proposed: "kept as the model proposed", "downgraded:compatibility_unknown": "downgraded: the outcomes may be compatible",
  "downgraded:case_undecided": "downgraded: the evaluator could not decide the case", "downgraded:no_case": "downgraded: no concrete case was given" };
const JOB_STATE: Record<string, string> = { completed: "done", failed: "failed", running: "running", waiting_for_approval: "waiting_for_approval", not_requested: "not_requested", not_needed: "done" };

function Dismiss({ ws, h, attempt, onDone }: { ws: string; h: Rec; attempt: number; onDone: () => void }) {
  const [reason, setReason] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  return (
    <div className="grid gap-1">
      <Field label="Why this is not a conflict" hint="Recorded with your name for this knowledge version; the hypothesis stays listed as dismissed.">
        <TextInput data-testid="mining-dismiss-reason" value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
      </Field>
      <div>
        <Button size="sm" data-testid="mining-dismiss" disabled={busy || !reason.trim()} onClick={async () => {
          setBusy(true); setErr(null);
          try { await api<Rec>("POST", `/v2/business/mining/hypotheses/${q(String(h.hypothesis_id))}/dismiss`, { workspace_id: ws, reason: reason.trim(), attempt }); onDone(); }
          catch (e) { setErr(errorText(e)); } finally { setBusy(false); }
        }}>Dismiss with this reason</Button>
      </div>
      {err && <Callout tone="danger">{err}</Callout>}
    </div>
  );
}

export function ConflictScan({ ws, canScan }: { ws: string; canScan: boolean }) {
  const [view, setView] = React.useState<Rec | null>(null);
  const [attempt, setAttempt] = React.useState(1);
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [tick, setTick] = React.useState(0);
  const [continuing, setContinuing] = React.useState(false);                       // set only by the person's click: this session continues THAT scan's deferred groups

  React.useEffect(() => {                                                          // read only; polled while a job runs; continues deferred groups of a scan the person started here
    let live = true;
    let timer: number | undefined;
    api<Rec>("GET", `/v2/business/mining?workspace_id=${q(ws)}&attempt=${attempt}`)
      .then(async (v) => {
        if (!live) return;
        setView(v); setErr(null);
        const jobs = [...((v.jobs ?? []) as Rec[]), v.semantic_candidates as Rec, v.unbound_triage as Rec].filter(Boolean);
        const busyNow = jobs.some((j) => j.state === "running" || j.state === "waiting_for_approval");
        const waiting = ((v.jobs ?? []) as Rec[]).some((j) => j.state === "not_requested");
        if (busyNow) { timer = window.setTimeout(() => setTick((t) => t + 1), 2000); return; }
        if (continuing && waiting) {                                                   // the slot is free: ask for the next deferred groups of the same attempt (finite, planned set)
          const r = await api<Rec>("POST", "/v2/business/mining", { workspace_id: ws, attempt });
          if (!live) return;
          if (!((r.jobs_created ?? []) as unknown[]).length && !((r.jobs_deferred ?? []) as unknown[]).length) setContinuing(false);
          timer = window.setTimeout(() => setTick((t) => t + 1), 1000);
        } else if (continuing) setContinuing(false);
      })
      .catch((e) => { if (live) setErr(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "The conflict scan is not available to your account." : errorText(e)); });
    return () => { live = false; if (timer) window.clearTimeout(timer); };
  }, [ws, attempt, tick, continuing]);

  const scan = async (att: number) => {
    setBusy(true); setErr(null);
    try { await api<Rec>("POST", "/v2/business/mining", { workspace_id: ws, attempt: att }); setAttempt(att); setContinuing(true); setTick((t) => t + 1); }
    catch (e) { setErr(e instanceof ApiError && e.status === 403 ? "A scan needs the AI permission. Nothing was started." : errorText(e)); }
    finally { setBusy(false); }
  };

  const jobs = (view?.jobs ?? []) as Rec[];
  const hyps = (view?.hypotheses ?? []) as Rec[];
  const cov = (view?.coverage ?? {}) as Rec;
  const cand = (view?.semantic_candidates ?? {}) as Rec;
  const triage = (view?.unbound_triage ?? {}) as Rec;
  const states = jobs.map((j) => String(j.state));
  const started = states.some((s) => s !== "not_requested") || (!!cand.state && cand.state !== "not_requested") || (!!triage.state && triage.state !== "not_requested");
  const notCompared = states.filter((s) => s === "not_requested").length;
  const running = states.some((s) => s === "running" || s === "waiting_for_approval");
  const failed = jobs.filter((j) => j.state === "failed");
  return (
    <Card title="Conflict scan with AI" testid="mining" data={{ attempt: String(attempt), open: String(view?.open ?? 0) }}>
      <Muted>A model compares groups of rules and proposes where they may disagree; the server checks each proposed case with the policy evaluator. Every result is a proposal for a person.</Muted>
      {err && <Callout tone="danger" testid="mining-error">{err}</Callout>}
      {view?.stale ? <Callout tone="warn" testid="mining-stale">These results belong to an older knowledge version.</Callout> : null}
      {view && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <StatusBadge {...aiJobBadge(!started ? "not_requested" : running ? "running" : failed.length ? "failed" : "done")} />
          <span data-testid="mining-progress">{states.filter((s) => s === "completed").length} of {jobs.length} rule groups compared{failed.length ? `, ${failed.length} not compared` : ""}.</span>
          {cov.complete === false && <StatusBadge status="partial" label="Coverage incomplete" />}
        </div>
      )}
      {canScan ? (
        <div className="flex flex-wrap items-center gap-2">
          {!started && <Button variant="primary" data-testid="mining-scan" disabled={busy || !view} onClick={() => void scan(attempt)}>{busy ? "Starting..." : "Scan conflicts with AI"}</Button>}
          {started && !running && !continuing && notCompared > 0 && <Button variant="primary" data-testid="mining-continue" disabled={busy} onClick={() => void scan(attempt)}>Continue the scan ({notCompared} groups not compared yet)</Button>}
          {started && !running && !continuing && notCompared === 0 && attempt < 3 && <Button data-testid="mining-rescan" disabled={busy} onClick={() => void scan(attempt + 1)}>Scan again (new attempt)</Button>}
          {continuing && <span role="status" className="text-sm" data-testid="mining-continuing">Continuing the scan you started: {notCompared} group(s) waiting for a free slot.</span>}
          <span className="text-xs text-muted">One paid model call per rule group plus the candidate and unbound-value steps, within your workspace allowance.</span>
        </div>
      ) : <Muted>Starting a scan needs knowledge authority and the AI permission.</Muted>}
      {failed.map((j) => <Callout key={String(j.group_id)} tone="warn" testid="mining-group-failed">{String(j.reason ?? "This group was not compared.")}</Callout>)}
      {cov.statement ? <Muted testid="mining-coverage">{String(cov.statement)}</Muted> : null}
      {started && hyps.length === 0 && !running && <Muted testid="mining-none">No hypothesis was kept. This is not proof that the rules agree: see the coverage above.</Muted>}
      <ul className="grid gap-2">
        {hyps.map((h) => (
          <li key={String(h.hypothesis_id)} data-testid="mining-hypothesis" data-kind={String(h.kind)} data-status={String(h.status)} data-verified={String(h.case_verified)}
              className="rounded-md border border-border p-3 grid gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <strong>{KIND[String(h.kind)] ?? String(h.kind)}</strong>
              <StatusBadge status="info" label="Proposed by a model" />
              {h.case_verified === true && <StatusBadge status="info" label="Case checked by the evaluator" />}
              {h.case_verified === false && <StatusBadge status="partial" label="Case not confirmed by the evaluator" />}
              {h.status === "DISMISSED" && <StatusBadge status="stale" label="Dismissed" />}
            </div>
            <p className="text-sm">{((h.titles ?? []) as string[]).join(" / ")}</p>
            <p>{String(h.reason ?? "")}</p>
            {h.case && Object.keys(h.case as Rec).length > 0 ? <Muted>Case: {Object.entries(h.case as Rec).map(([k, v]) => `${k} = ${String(v)}`).join(", ")}</Muted> : null}
            <Muted>Server: {DISPOSITION[String(h.disposition)] ?? String(h.disposition)}{h.compatibility ? `; outcomes ${String(h.compatibility)}` : ""}.{h.question ? ` Question for a person: ${String(h.question)}` : ""}</Muted>
            {h.status === "DISMISSED" && h.dismissal ? <Muted>Dismissed: {String((h.dismissal as Rec).reason)}</Muted> : null}
            {canScan && h.status === "PROPOSED" && <Dismiss ws={ws} h={h} attempt={attempt} onDone={() => setTick((t) => t + 1)} />}
            <Tech><span className="text-xs">hypothesis <code>{String(h.hypothesis_id)}</code> proposed kind <code>{String(h.kind_proposed)}</code> rules <code>{((h.rule_ids ?? []) as string[]).join(", ")}</code></span></Tech>
          </li>
        ))}
      </ul>
      {(cand.state && cand.state !== "not_requested") || (triage.state && triage.state !== "not_requested") ? (
        <details data-testid="mining-extra">
          <summary className="text-sm cursor-pointer">Candidate pairs and unbound values</summary>
          <div className="grid gap-1 text-sm">
            <p>Candidate pairs: {aiJobBadge(JOB_STATE[String(cand.state)] ?? cand.state).label}; {((cand.pairs ?? []) as Rec[]).length} kept, {((cand.rejected ?? []) as Rec[]).length} refused. {String(cand.statement ?? "")}</p>
            <p>Unbound values: {aiJobBadge(JOB_STATE[String(triage.state)] ?? triage.state).label}; {((triage.items ?? []) as Rec[]).length} classified{triage.values_withheld ? " (values withheld from your account)" : ""}. Proposals only; a number is never presumed a rule.</p>
          </div>
        </details>
      ) : null}
    </Card>
  );
}
