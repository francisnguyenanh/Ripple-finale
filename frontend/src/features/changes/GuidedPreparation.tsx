// EUI-08 guided proposal preparation (contract docs/contracts/GUIDED_PREPARATION.md). Hidden unless the server enables it. Shows the manifest the server computed
// for this person (pins, stages, 0 paid calls, unknowns, what stays manual) and starts it only with the person's explicit choices: the target rule when the draft
// names several, and the edits (none preselected). Each step is an ordinary job created as this person; the panel steps the run while it is active and never
// approves, commits or applies anything.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, Tech } from "@/components/bits";
import { StatusBadge, type Status } from "@/components/StatusBadge";
import { basename } from "@/lib/fmt";
import type { Rec } from "./types";

const q = encodeURIComponent;
const RUN: Record<string, { status: Status; label: string }> = {
  RUNNING: { status: "running", label: "Preparing" }, PAUSED: { status: "stale", label: "Paused" }, FAILED: { status: "failed", label: "Stopped" },
  PROPOSAL_READY: { status: "success", label: "Proposal prepared (not approved)" }, CANCELLED: { status: "stale", label: "Cancelled" },
};

export function GuidedPreparation({ ws, intentId, onReady }: { ws: string; intentId: string; onReady: (proposalJobId: string) => void }) {
  const [enabled, setEnabled] = React.useState(false);
  const [run, setRun] = React.useState<Rec | null>(null);
  const [m, setM] = React.useState<Rec | null>(null);
  const [target, setTarget] = React.useState<string>("");
  const [chosen, setChosen] = React.useState<Set<string>>(new Set());              // never preselected
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/intents/${q(intentId)}/preparation/runs?workspace_id=${q(ws)}`)
      .then((r) => { if (!live) return; setEnabled(!!r.enabled); const active = ((r.runs ?? []) as Rec[]).find((x) => ["RUNNING", "PAUSED", "PROPOSAL_READY"].includes(String(x.status))); setRun(active ?? null); })
      .catch(() => { if (live) setEnabled(false); });
    return () => { live = false; };
  }, [ws, intentId]);

  const accepted = [...chosen].sort();
  React.useEffect(() => {                                                          // read-only manifest for the current choices
    if (!enabled || (run && run.status !== "CANCELLED")) return;
    let live = true;
    const params = new URLSearchParams({ workspace_id: ws, ...(target ? { target } : {}), ...(accepted.length ? { accepted: accepted.join(",") } : {}) });
    api<Rec>("GET", `/v2/intents/${q(intentId)}/preparation/manifest?${params.toString()}`)
      .then((x) => { if (live) { setM(x); setErr(null); } })
      .catch((e) => { if (live) { setM(null); setErr(e instanceof ApiError ? String((e.body as Rec)?.error ?? errorText(e)) : errorText(e)); } });
    return () => { live = false; };
  }, [enabled, ws, intentId, target, accepted.join(","), run?.status]);              // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {                                                          // step the run only while it is active
    if (!run || run.status !== "RUNNING") return;
    const t = window.setTimeout(() => {
      api<Rec>("POST", `/v2/preparation/runs/${q(String(run.run_id))}/advance`, { workspace_id: ws }).then((r) => {
        setRun(r);
        if (r.status === "PROPOSAL_READY" && r.proposal_job_id) onReady(String(r.proposal_job_id));
      }).catch((e) => setErr(errorText(e)));
    }, 2000);
    return () => window.clearTimeout(t);
  }, [ws, run, onReady]);

  if (!enabled) return null;
  const startRun = async () => {
    if (!m?.ready_to_start) return;
    setBusy(true); setErr(null);
    try {
      setRun(await api<Rec>("POST", `/v2/intents/${q(intentId)}/preparation`, { workspace_id: ws, manifest_hash: m.manifest_hash, target_candidate_id: (m.selected as Rec)?.target_candidate_id ?? null, accepted_refs: accepted }));
    } catch (e) { setErr(e instanceof ApiError ? String((e.body as Rec)?.error ?? errorText(e)) : errorText(e)); } finally { setBusy(false); }
  };

  if (run && run.status !== "CANCELLED") {
    const badge = RUN[String(run.status)] ?? { status: "info" as Status, label: String(run.status) };
    return (
      <section data-testid="prep-run" data-status={String(run.status)} aria-label="Guided preparation" className="rounded-md border border-border p-3 grid gap-2 text-sm">
        <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">Guided preparation</h3><StatusBadge status={badge.status} label={badge.label} /></div>
        <ol className="grid gap-1">{((run.stages ?? []) as Rec[]).map((s0) => <li key={String(s0.stage)} data-testid="prep-stage" data-stage={String(s0.stage)} data-state={String(s0.state)}>{String(s0.stage)}: {String(s0.state).replace(/_/g, " ")}</li>)}</ol>
        {run.reason ? <Callout tone={run.status === "FAILED" ? "danger" : "warn"} testid="prep-reason">{String(run.reason)}</Callout> : null}
        <Muted>Nothing is approved, committed or applied by this run. Next, by people: {((run.manual_after ?? []) as string[]).join(", ")}.</Muted>
        {(run.status === "RUNNING" || run.status === "PAUSED") && <div><Button size="sm" variant="ghost" data-testid="prep-cancel" onClick={async () => setRun(await api<Rec>("POST", `/v2/preparation/runs/${q(String(run.run_id))}/cancel`, { workspace_id: ws }))}>Stop the preparation</Button></div>}
        {err && <Callout tone="danger" testid="prep-error">{err}</Callout>}
      </section>
    );
  }
  const sel = (m?.selected ?? {}) as Rec;
  return (
    <section data-testid="prep-manifest" aria-label="Guided preparation" className="rounded-md border border-border p-3 grid gap-2 text-sm">
      <h3 className="font-semibold">Prepare the proposal in one go (optional)</h3>
      <Muted>Runs the impact analysis, the sample simulation and the proposal one after the other, as you, after you confirm the choices below. 0 paid model calls; no file changes.</Muted>
      {err && <Callout tone="warn" testid="prep-error">{err}</Callout>}
      {m && (
        <>
          <p>Stages: {((m.stages ?? []) as Rec[]).map((s0) => String(s0.stage)).join(" → ")} · paid calls: {String(m.paid_calls)}</p>
          {((m.target_options ?? []) as Rec[]).length > 1 && (
            <fieldset className="grid gap-1" data-testid="prep-targets">
              <legend className="font-medium">Which rule is the analysis about? (your choice)</legend>
              {((m.target_options ?? []) as Rec[]).map((t) => (
                <label key={String(t.id)} className="flex items-center gap-2"><input type="radio" name={`prep-target-${intentId}`} checked={target === t.id} onChange={() => setTarget(String(t.id))} />{String(t.label)}</label>))}
            </fieldset>
          )}
          {sel.target_proposed_by_server ? <Muted testid="prep-target-proposed">The draft names one rule, so it is proposed: {String(((m.target_options ?? []) as Rec[])[0]?.label ?? "")}. Starting confirms it.</Muted> : null}
          <fieldset className="grid gap-1" data-testid="prep-edits">
            <legend className="font-medium">Which proposed edits should the proposal carry? (nothing is ticked for you)</legend>
            {((m.edits_offered ?? []) as Rec[]).map((e) => (
              <label key={String(e.ref)} className="flex items-center gap-2">
                <input type="checkbox" data-testid="prep-edit" checked={chosen.has(String(e.ref))}
                  onChange={(ev) => setChosen((p) => { const n = new Set(p); if (ev.target.checked) n.add(String(e.ref)); else n.delete(String(e.ref)); return n; })} />
                {basename(String(e.logical_path ?? ""))}: {String(e.expected_current ?? "?")} → {String(e.proposed ?? "?")}
              </label>))}
          </fieldset>
          {((m.unknowns ?? []) as string[]).map((u) => <Callout key={u} tone="warn" testid="prep-unknown">{u.split(": ").slice(1).join(": ") || u}</Callout>)}
          <Muted>Stays with people afterwards: {((m.manual_after ?? []) as string[]).join(", ")}. {String(m.files_changed_by_run)}.</Muted>
          <Tech><span className="text-xs">manifest <code>{String(m.manifest_hash)}</code> pins <code>{JSON.stringify(m.pins)}</code></span></Tech>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" data-testid="prep-start" disabled={busy || !m.ready_to_start} onClick={() => void startRun()}>Prepare proposal</Button>
            {!m.ready_to_start && <span className="text-xs text-muted" data-testid="prep-choices">Still to choose: {((m.choices_needed ?? []) as string[]).map((c) => c.replace(/_/g, " ")).join(", ")}</span>}
          </div>
        </>
      )}
    </section>
  );
}
