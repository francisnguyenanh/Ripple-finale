// A stored AI reading of ONE simulation result. Reading it never calls a model: the analysis is stored once (a job a different person
// allowed) and every render, poll or Business/Technical toggle only reads it. Measured facts, AI interpretation, gaps and options are shown apart.
import * as React from "react";
import { Loader2, Sparkles } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, StatePill, Tech } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { useNotify } from "@/lib/notify";
import { useParam } from "@/lib/param";
import { useFlow } from "@/features/changes/flow";
import { failureAdvice } from "@/lib/reasons";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const BASIS: Record<string, string> = {
  untested_edit: "an edit that was not simulated", draft_unaddressed: "a draft item nobody addressed", draft_not_covered: "a source the draft did not cover",
  draft_open_item: "an open item of the draft", simulation_limit: "a limit of the simulation", model_inference: "the AI's own inference (not in the facts)",
};

interface Reading {
  retryable?: boolean;
  context_differs?: boolean;
  status: "none" | "waiting_for_approval" | "running" | "done" | "failed" | "cancelled";
  job_id: string | null; analysis: Rec | null; model?: string; reason?: string | null;
  snapshot: { key: string; input_hash?: string };
  currentness?: { status: "CURRENT" | "HISTORICAL" | "UNKNOWN"; facts_source_revision_id?: string | null; head_source_revision_id?: string | null };
}

export function SimulationAnalysisCard({ ws, simulationJobId, impactJobId }: { ws: string; simulationJobId: string; impactJobId?: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const [r, setR] = React.useState<Reading | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const polls = React.useRef(0);
  const notify = useNotify();
  const { data } = useFlow();
  const [, setWis] = useParam("wis");
  const tag = (r?.job_id ?? "").slice(-8);
  const point = (kind: "q" | "o", i: number) => (data?.gaps?.gaps ?? []).find((g) => g.key === `reading:${tag}:${kind}${i}`) ?? null;
  const goto = (sel: string) => window.setTimeout(() => document.querySelector(sel)?.scrollIntoView({ block: "center" }), 50);
  const explore = (text: string) => { setWis(text.slice(0, 300)); goto("[data-testid=flow-whatif]"); };
  const mayExplore = s.can("change.prepare") && r?.currentness?.status !== "HISTORICAL";
  // where the person stands on this question/option: it is an open point of the draft (same comment / confirm / revise path)
  const where = (g: ReturnType<typeof point>) => g === null ? null : (
    <span data-testid="reading-point" data-state={g.state} className="text-xs rounded-full border border-border px-2 ml-1">
      {g.state === "confirmed" ? `Decided by ${g.confirmation?.actor_id}` : g.state === "commented" ? "Commented, not decided" : "Open point of the draft"}
    </span>);
  const since = React.useRef<number | null>(null);
  const prev = React.useRef<string | null>(null);
  const [now, setNow] = React.useState(() => Date.now());
  const path = `/v2/simulation-analyses?workspace_id=${q(ws)}&simulation_job_id=${q(simulationJobId)}${impactJobId ? `&impact_job_id=${q(impactJobId)}` : ""}`;
  const load = React.useCallback(async () => {
    try { setR(await api<Reading>("GET", path)); setErr(null); } catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [path]);
  React.useEffect(() => { polls.current = 0; void load(); }, [load]);
  const waiting = r?.status === "waiting_for_approval" || r?.status === "running";
  // Checks every 3 s for the first 3 minutes, then every 10 s up to 15 minutes in all (a rate-limited provider call can take minutes); then it says it stopped.
  const POLL_LIMIT = 60 + 80;
  const gaveUp = waiting && polls.current >= POLL_LIMIT;
  React.useEffect(() => {
    if (!waiting || polls.current >= POLL_LIMIT) return;
    const t = window.setTimeout(() => { polls.current += 1; void load(); }, polls.current < 60 ? 3000 : 10000);
    return () => window.clearTimeout(t);
  }, [waiting, r, load]);
  React.useEffect(() => {      // elapsed time while it runs, and one clear message when it ends
    const st = r?.status ?? null;
    if (st === "running" && since.current === null) since.current = Date.now();
    if (st !== "running" && since.current !== null && (st === "done" || st === "failed" || st === "cancelled")) {
      if (prev.current === "running") notify.status(st === "done" ? "The AI reading is ready." : "The AI reading failed. See the red box under the title.");
      since.current = null;
    }
    prev.current = st;
  }, [r?.status, notify]);
  React.useEffect(() => {
    if (r?.status !== "running") return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [r?.status]);

  if (err) return err.status === 404 || err.status === 409 ? <Muted testid="sim-analysis-unavailable">No AI reading is possible for this result: there is no executed simulation to read.</Muted> : <Muted>The AI reading could not be loaded (HTTP {err.status}).</Muted>;
  if (!r) return null;
  const a = r.analysis;
  return (
    <section data-testid="sim-analysis" data-state={r.status} aria-label="AI reading of the simulation" className="rounded-md border border-border bg-surface-2 p-3 grid gap-2">
      <h3 className="text-sm font-semibold flex flex-wrap items-center gap-2"><Sparkles size={14} aria-hidden="true" />AI reading of this simulation
        {r.status === "running" && <StatePill state="RUNNING" label="Running" testid="sim-analysis-state" />}
        {r.status === "done" && <StatePill state="COMPLETED" label="Done" testid="sim-analysis-state" />}
        {(r.status === "failed" || r.status === "cancelled") && <StatePill state="FAILED" label={r.status === "failed" ? "Failed" : "Cancelled"} testid="sim-analysis-state" />}
      </h3>
      {r.currentness?.status === "HISTORICAL" && (
        <div data-testid="sim-analysis-historical" className="grid gap-1">
          <StatePill state="STALE" label="Older revision" />
          <Muted>This reading describes a simulation that was run on the files as they were before the latest applied change. The files have changed since, so it is history, not a statement about the current files. Nothing can be applied from it: run a new simulation for the current files.
            <Tech> Baseline revision {r.currentness.facts_source_revision_id ?? "unknown"}; current head {r.currentness.head_source_revision_id ?? "unknown"}.</Tech></Muted>
        </div>
      )}
      {r.context_differs && <Muted testid="sim-analysis-context">This reading was written from the simulation result alone, without the impact analysis that is open on this page.</Muted>}
      {r.status === "done" && a && (
        <div className="grid gap-2 text-sm">
          <div data-testid="sim-analysis-measured"><h4 className="font-semibold">Measured by the simulation</h4>
            <p>{(a.changed as Rec).changed_count} sample cases change their outcome (direction: {String((a.changed as Rec).direction).replace(/_/g, " ")}).<Tech> cases cited: {((a.changed as Rec).case_refs as string[]).join(", ")}</Tech></p></div>
          <div data-testid="sim-analysis-interpretation"><h4 className="font-semibold">AI interpretation (not a measured fact)</h4>
            <ul className="list-disc pl-5">{(a.interpretation as Rec[]).map((i, k) => <li key={k} data-ai-text>{i.statement}{!i.grounded_in_cases && <span className="text-muted"> (not tied to a tested case)</span>}<Tech> [{(i.case_refs as string[]).join(", ")}]</Tech></li>)}</ul></div>
          <div data-testid="sim-analysis-gaps"><h4 className="font-semibold">Open or not tested</h4>
            {(a.gaps as Rec[]).length ? <ul className="list-disc pl-5">{(a.gaps as Rec[]).map((g, k) => <li key={k} data-ai-text>{g.statement} <span className="text-muted">({BASIS[g.basis as string] ?? g.basis})</span></li>)}</ul> : <Muted>The AI listed no gap. This does not mean there is none.</Muted>}</div>
          {(a.options as Rec[]).length > 0 && <div data-testid="sim-analysis-options"><h4 className="font-semibold">Options to consider (suggestions, not decisions)</h4>
            <ul className="list-disc pl-5">{(a.options as Rec[]).map((o, k) => <li key={k} data-ai-text>{o.option}<span className="text-muted"> - assumes: {o.assumption}</span>{where(point("o", k))}{point("o", k) && <> <Button data-testid="sim-analysis-goto" onClick={() => goto(`[data-gap-key="reading:${tag}:o${k}"]`)}>Decide in the open points</Button></>}{mayExplore && <> <Button data-testid="sim-analysis-explore" onClick={() => explore(`What happens if: ${String(o.option)}`)}>Explore this option (what-if)</Button></>}</li>)}</ul>
            <Muted>An option is a suggestion, not a decision. It is listed with the draft's open points: a person with confirm rights decides it there, and the next draft takes the decision into account. "Explore" opens the what-if with the text proposed: it can only simulate a change of a supported number, and says so when it cannot. Nothing is applied.</Muted></div>}
          {(a.questions as string[]).length > 0 && <div data-testid="sim-analysis-questions"><h4 className="font-semibold">Questions for you</h4>
            <ul className="list-disc pl-5">{(a.questions as string[]).map((t, k) => <li key={k} data-ai-text>{t}{where(point("q", k))}{point("q", k) && <> <Button data-testid="sim-analysis-goto" onClick={() => goto(`[data-gap-key="reading:${tag}:q${k}"]`)}>Answer in the open points</Button></>}</li>)}</ul>
            <Muted>These questions are open points of the draft: answer them there (a comment is not used by the next draft; a confirmed answer is).</Muted></div>}
          <p className="text-xs text-muted">AI-written from stored facts. It cannot mark anything verified, resolve an open item, approve or apply. Numbers, case references and direction were checked by the server; the reasoning was not.</p>
          <Tech><p className="text-xs text-muted break-all">snapshot {r.snapshot.key} - model {r.model} - facts from revision {r.currentness?.facts_source_revision_id ?? "unknown"}, head {r.currentness?.head_source_revision_id}</p></Tech>
        </div>
      )}
      {r.status === "none" && (
        <>
          <Muted>No AI reading exists for this simulation. Writing one uses the paid model (one call, within your AI permission).</Muted>
          {s.can("ai.use") && <div><Button data-testid="sim-analysis-request" title={aiWhy(s, "sim.analysis")} disabled={pending || !s.aiKind("sim.analysis")} onClick={async () => { if (await run(() => api("POST", "/v2/simulation-analyses", { workspace_id: ws, simulation_job_id: simulationJobId, impact_job_id: impactJobId }), { stale: true })) await load(); }}>Ask the AI to read this simulation (uses the paid model)</Button></div>}
        </>
      )}
      {r.status === "waiting_for_approval" && (
        <>
          <Muted>In this shared trial workspace a different person has to allow this AI run. Nothing was sent to the AI.</Muted>
          {s.can("source.import") && r.job_id && <div><Button data-testid="sim-analysis-approve" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${q(r.job_id as string)}/approve`, { workspace_id: ws }), { stale: true })) await load(); }}>Allow this AI run (uses the paid model)</Button></div>}
        </>
      )}
      {r.status === "running" && (
        <Callout tone="wait" testid="sim-analysis-running" role="status">
          <p className="flex items-center gap-2"><Loader2 size={16} className="animate-spin" aria-hidden="true" /><strong>Running: the AI is writing the reading.</strong></p>
          <p className="text-muted">{since.current ? `Waiting ${Math.max(0, Math.floor((now - since.current) / 1000))} s on this page. ` : ""}It usually takes under a minute; if the AI service is busy it is tried again automatically and can take a few minutes. The result appears here by itself and a message says when it is ready. Do not click again.</p>
          {gaveUp && <p data-testid="sim-analysis-gaveup">This page stopped checking after 15 minutes. <button className="underline" onClick={() => { polls.current = 0; void load(); }}>Check again</button></p>}
        </Callout>
      )}
      {(r.status === "failed" || r.status === "cancelled") && <Callout tone="danger" testid="sim-analysis-failed" role="alert"><p><strong>The AI reading was not stored.</strong> {r.reason ? failureAdvice(r.reason).what : r.status} The simulation result above is unchanged.</p>{r.reason && <p className="text-muted">{failureAdvice(r.reason).next}</p>}{r.retryable && s.can("ai.use") && <div><Button data-testid="sim-analysis-retry" title={s.aiOff ? "AI actions are switched off on this server" : undefined} disabled={pending || s.aiOff} onClick={async () => { if (await run(() => api("POST", "/v2/simulation-analyses", { workspace_id: ws, simulation_job_id: simulationJobId, impact_job_id: impactJobId, retry: true }), { stale: true })) await load(); }}>Try again (a new request; uses the paid model)</Button></div>}</Callout>}
    </section>
  );
}
