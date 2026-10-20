// AI panels of a confirmed change's review: the what-if simulation (MA-S1 / LLM-S1), the risk / trade-off memo (SUR-06) and the synthesis memo of a review-board run (SUR-08).
// Reading lists existing runs and memos; a run or a memo starts only when a person asks (paid model calls, stated next to the button). In the simulation the ENGINE result
// (deterministic, seeded), the model's SCENARIOS and CRITIC findings and the model's INTERPRETATION are shown as separate things; without calibration every number is
// exploratory, never a forecast. None of these changes the knowledge, a severity or a decision.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, HowItWorks, Muted, Tech } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import type { Status } from "@/components/StatusBadge";
import { OriginSection } from "@/components/decision/Origin";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";
import { AdvisoryMemo } from "./AdvisoryMemo";
import { formatEngine } from "./simFormat";
import { citedBy, parseMetricRef, interpretationCheck } from "./metricRefs";

const q = encodeURIComponent;
const RUN: Record<string, { status: Status; label: string }> = {
  PLANNED: { status: "queued", label: "Planned" }, RUNNING: { status: "running", label: "Model specialists working" }, JOINING: { status: "running", label: "Second round running" },
  REVIEW_READY: { status: "success", label: "Run finished" }, INCOMPLETE: { status: "partial", label: "Run incomplete" }, FAILED: { status: "failed", label: "Run failed" }, CANCELLED: { status: "stale", label: "Run cancelled" },
};
// The engine's headline KPIs (process_simulation.compare), each with its unit: a share is shown as a percentage and its change in percentage points.
const KPI: Record<string, { label: string; unit: "days" | "share" }> = {
  cycle_time_mean_days: { label: "Mean cycle time", unit: "days" }, cycle_time_p95_days: { label: "95th percentile cycle time", unit: "days" },
  sla_breach_share: { label: "Requests breaching the SLA", unit: "share" }, unsupported_share: { label: "Requests no rule could decide (unsupported)", unit: "share" },
};
const LIVE = ["PLANNED", "RUNNING", "JOINING"];
const MOVED = "Blocked: the business knowledge changed after this change was confirmed, so a simulation would measure an outdated version. The change has to be stated and confirmed again first. Nothing was started.";

const fmt = formatEngine;

function Simulation({ ws, run }: { ws: string; run: Rec }) {
  const rep = (run.report ?? null) as Rec | null;
  const sim = (rep?.simulation ?? null) as Rec | null;
  const cmp = (sim?.comparison ?? null) as Rec | null;
  const cal = (sim?.calibration ?? {}) as Rec;
  const interp = (sim?.interpretation ?? null) as Rec | null;
  const prog = (run.progress ?? null) as Rec | null;
  const waiting = LIVE.includes(String(run.status)) && prog?.phase === "waiting_for_slot";
  const badge = waiting ? { status: "waiting" as Status, label: "Waiting for a free slot" } : RUN[String(run.status)] ?? { status: "info" as Status, label: `State: ${String(run.status)}` };
  const steps = (prog?.children ?? null) as Rec | null;
  const kpis = Object.keys(KPI);
  const [hl, setHl] = React.useState<{ rows: string[]; pinned: boolean } | null>(null);   // EUI-07: rows cited by the statement under the pointer / focus / click
  const statements = ((interp?.statements ?? []) as Rec[]);
  const cites = citedBy(statements);
  const rowLabel = (n: number) => String((((cmp?.rows ?? []) as Rec[])[n - 1] ?? {}).label ?? "");
  const rowsOf = (st: Rec) => ((st.metric_refs ?? []) as unknown[]).map(parseMetricRef).filter((x) => x !== null).map((x) => x!.row);
  const show = (rows: string[], pinned = false) => setHl((h) => (h?.pinned && !pinned ? h : { rows, pinned }));
  const hide = () => setHl((h) => (h?.pinned ? h : null));
  return (
    <div className="grid gap-3" data-testid="sim-run" data-status={String(run.status)} data-phase={String(prog?.phase ?? "none")}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge status={badge.status} label={badge.label} />
        <StatusBadge status={cal.status === "within_tolerance" ? "info" : "partial"} label={cal.status === "within_tolerance" ? "Calibrated within tolerance" : "Exploratory: not calibrated against observed data"} />
      </div>
      {LIVE.includes(String(run.status)) && (
        <p role="status" className="text-sm text-muted" data-testid="sim-progress">
          {waiting ? String(prog?.reason ?? "Waiting for a free slot. Nothing failed.") : "The scenario designer, the critic and the engine are working; results appear when the run finishes."}
          {steps ? ` Steps: ${String(steps.done)} done, ${String(steps.running)} running, ${Number(steps.queued) + Number(steps.planned)} waiting${Number(steps.failed) ? `, ${String(steps.failed)} failed` : ""}.` : ""}
        </p>
      )}
      {sim && (
        <>
          <OriginSection origin="ai" ariaLabel="Scenarios proposed by a model" title="Scenarios (proposed by a model, checked by the server)">
            <ul className="list-disc pl-5">{((sim.scenarios ?? []) as Rec[]).map((sc, i) => <li key={i} data-testid="sim-scenario">{String(sc.label)}</li>)}</ul>
            {((sim.rejected_scenarios ?? []) as Rec[]).length > 0 && <Muted>{((sim.rejected_scenarios ?? []) as Rec[]).length} proposed scenario(s) were refused by the server.</Muted>}
          </OriginSection>
          {cmp && (
            <OriginSection origin="engine" ariaLabel="Engine result" title="Engine result (deterministic, seeded; not a forecast)">
              <div className="overflow-x-auto" role="region" aria-label="Engine comparison table, scrolls sideways" tabIndex={0}>
                <table data-testid="sim-table" className="text-sm border-collapse">
                  <caption className="text-left text-muted">Each scenario against the baseline run &quot;{String(cmp.baseline ?? "baseline")}&quot;; change = scenario minus baseline.</caption>
                  <thead><tr>{["Scenario", "Metric", "Baseline", "Scenario value", "Change"].map((h) => <th key={h} scope="col" className="text-left pr-4 pb-1">{h}</th>)}</tr></thead>
                  <tbody>
                    {((cmp.rows ?? []) as Rec[]).flatMap((row, i) => kpis.filter((k) => row[k] !== undefined).map((k, j) => {
                      const c = (row[k] ?? {}) as Rec;
                      const u = KPI[k].unit;
                      return (
                        <tr key={`${i}-${k}`} data-testid="sim-row" data-kpi={k} data-ref={`s${i + 1}.${k}`} data-highlight={String(!!hl?.rows.includes(`s${i + 1}.${k}`))}
                          className={`${j === 0 ? "border-t border-border" : ""} ${hl?.rows.includes(`s${i + 1}.${k}`) ? "bg-origin-ai-surface outline outline-2 outline-origin-ai" : ""}`}>
                          {j === 0 ? <th scope="row" rowSpan={kpis.filter((x) => row[x] !== undefined).length} className="text-left pr-4 align-top font-normal">{String(row.label)}</th> : null}
                          <td className="pr-4">{KPI[k].label}{cites[`s${i + 1}.${k}`] ? <span className="text-xs text-muted" data-testid="sim-row-cited"> (cited in AI statement {cites[`s${i + 1}.${k}`].join(", ")})</span> : null}</td>
                          <td className="pr-4 tnum whitespace-nowrap">{fmt(c.baseline, u)}</td>
                          <td className="pr-4 tnum whitespace-nowrap">{fmt(c.scenario, u)}</td>
                          <td className="pr-4 tnum" data-testid="sim-delta">{fmt(c.delta, u, true)}</td>
                        </tr>
                      );
                    }))}
                  </tbody>
                </table>
              </div>
              {((cmp.limitations ?? []) as string[]).map((l, i) => <Muted key={i}>{l}</Muted>)}
            </OriginSection>
          )}
          {Array.isArray(rep?.findings) && (rep?.findings as Rec[]).length > 0 && (
            <OriginSection origin="ai" ariaLabel="Critic findings" title="Assumption critic (a model's findings)">
              <ul className="list-disc pl-5">{(rep!.findings as Rec[]).map((f, i) => <li key={i} data-testid="sim-critic">{String(f.statement)} <span className="text-muted">({String(f.severity)})</span></li>)}</ul>
            </OriginSection>
          )}
          <OriginSection origin="ai" ariaLabel="AI interpretation" testid="sim-interpretation" status={String(interp?.status ?? "none")}
            title="AI interpretation (every number checked against the engine result)">
            <span data-testid="sim-interpretation-check" data-kept={interpretationCheck(interp).kept} data-refused={interpretationCheck(interp).refused}>
              <StatusBadge status={interpretationCheck(interp).status} label={interpretationCheck(interp).label} /></span>
            {!interp && <Muted>No interpretation was written; the engine result stands on its own.</Muted>}
            {interp && ((interp.statements ?? []) as Rec[]).length === 0 && <Muted>The interpreter kept no statement after the server checks.</Muted>}
            <ol className="list-decimal pl-5 grid gap-1">{statements.map((st, i) => {
              const rows = rowsOf(st);
              const on = !!hl?.pinned && hl.rows.join() === rows.join();
              return (
                <li key={i} data-testid="sim-statement" onMouseEnter={() => show(rows)} onMouseLeave={hide}>
                  {String(st.text)}{" "}
                  <span className="inline-flex flex-wrap gap-1 align-middle">
                    {((st.metric_refs ?? []) as unknown[]).map((r, j) => {
                      const p = parseMetricRef(r);
                      return p && rowLabel(p.scenario) ? (
                        <span key={j} data-testid="sim-statement-ref" data-ref={p.ref} className="text-xs rounded-sm border border-border px-1">{rowLabel(p.scenario)} · {KPI[p.kpi]?.label ?? p.kpi} ({p.part})</span>
                      ) : <span key={j} className="text-xs text-muted" data-testid="sim-statement-ref-missing">reference unavailable</span>;
                    })}
                    {rows.length > 0 && <Button size="sm" variant="ghost" data-testid="sim-statement-show" aria-pressed={on} onFocus={() => show(rows)} onBlur={hide}
                      onClick={() => setHl(on ? null : { rows, pinned: true })}>{on ? "Hide rows" : "Show rows"}</Button>}
                  </span>
                </li>
              );
            })}</ol>
            {((interp?.rejected ?? []) as Rec[]).length > 0 && <Muted>{((interp!.rejected) as Rec[]).length} statement(s) refused (for example a number that is not an engine metric).</Muted>}
            {interp?.label ? <Muted>{String(interp.label)}</Muted> : null}
          </OriginSection>
          {((rep?.limitations ?? []) as string[]).length > 0 && (                       // run-wide limits: one click away; the engine's own limits stay under the table
            <details className="text-sm" data-testid="sim-run-limits">
              <summary className="cursor-pointer w-fit text-primary">Other limits of this run ({((rep?.limitations ?? []) as string[]).length})</summary>
              {((rep?.limitations ?? []) as string[]).map((l, i) => <Muted key={i}>{l}</Muted>)}
            </details>
          )}
        </>
      )}
      {run.status === "INCOMPLETE" && rep?.incomplete_reason ? <Callout tone="warn">{String(rep.incomplete_reason)}</Callout> : null}
      <Tech><span className="text-xs">run <code>{String(run.run_id)}</code> report <code>{String(run.report_hash ?? "")}</code> workspace <code>{ws}</code></span></Tech>
    </div>
  );
}

export function ChangeAiPanels({ ws, cid, knowledgeMoved = false }: { ws: string; cid: string; knowledgeMoved?: boolean }) {
  const s = useSession();
  const [runs, setRuns] = React.useState<Rec[] | null>(null);
  const [simRun, setSimRun] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [blocked, setBlocked] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [tick, setTick] = React.useState(0);

  React.useEffect(() => {                                                          // read only: the change's runs, then the latest simulation run; polled while it works
    let live = true;
    let timer: number | undefined;
    (async () => {
      try {
        const r = await api<Rec>("GET", `/v2/business/changes/${q(cid)}/ma?workspace_id=${q(ws)}`);
        if (!live) return;
        const list = (r.runs ?? []) as Rec[];
        setRuns(list); setErr(null);
        const sim = [...list].reverse().find((x) => x.workflow === "simulation");
        if (sim) {
          const st = await api<Rec>("GET", `/v2/ma/runs/${q(String(sim.run_id))}?workspace_id=${q(ws)}`);
          if (!live) return;
          setSimRun(st);
          if (LIVE.includes(String(st.status))) timer = window.setTimeout(() => setTick((t) => t + 1), 2000);
        }
      } catch (e) {
        if (live) setErr(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "The AI panels of this change are not available to your account." : errorText(e));
      }
    })();
    return () => { live = false; if (timer) window.clearTimeout(timer); };
  }, [ws, cid, tick]);

  const startSim = async () => {
    setBusy(true); setErr(null); setBlocked(null);
    try { await api<Rec>("POST", "/v2/ma/runs", { workspace_id: ws, change_id: cid, workflow: "simulation" }); setTick((t) => t + 1); }
    catch (e) {
      const code = e instanceof ApiError ? String((e.body as Rec | null)?.code ?? "") : "";
      if (code === "knowledge_moved") setBlocked(MOVED);                          // a refusal by state, not a failure: nothing was started and the reason is the next step
      else setErr(e instanceof ApiError && e.status === 403 ? "A simulation needs the multi-agent and AI permissions. Nothing was started." : errorText(e));
    }
    finally { setBusy(false); }
  };

  const canSim = s.can("ma.run") && s.can("ai.use");
  const blockedReason = knowledgeMoved ? MOVED : blocked;
  const board = runs ? [...runs].reverse().find((x) => x.workflow === "review_board" && ["REVIEW_READY", "INCOMPLETE"].includes(String(x.status))) : null;
  return (
    <div className="grid gap-4" data-testid="change-ai">
      <Card title="Multi-agent assessment: what-if simulation" testid="sim">
        <Muted>Explores what this change could do; exploratory unless calibrated with observed data.</Muted>
        <HowItWorks testid="sim-how">
          <p>A model designs scenarios for this change and a critic questions their assumptions; the deterministic engine computes every number; a model then interprets the engine result.</p>
        </HowItWorks>
        {err && <Callout tone="danger" testid="sim-error">{err}</Callout>}
        {blockedReason && <Callout tone="warn" testid="sim-blocked">{blockedReason}</Callout>}
        {runs === null && !err && <p role="status" className="text-sm text-muted">Loading...</p>}
        {simRun ? <Simulation ws={ws} run={simRun} /> : runs && <Muted testid="sim-none">No simulation was run for this change.</Muted>}
        {runs && (!simRun || ["FAILED", "CANCELLED", "INCOMPLETE"].includes(String(simRun.status))) && (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant={simRun ? "secondary" : "primary"} data-testid="sim-start" disabled={busy || !canSim || !!blockedReason} onClick={() => void startSim()}>{busy ? "Starting..." : "Run a what-if simulation"}</Button>
            <span className="text-xs text-muted">{blockedReason ? "Blocked: see the reason above." : canSim ? "Up to three paid model calls (designer, critic, interpreter) within the run's budget." : "Needs the multi-agent and AI permissions."}</span>
          </div>
        )}
      </Card>
      <AdvisoryMemo testid="risk-memo" title="Risk and trade-off memo" purpose="A model explains which risks and trade-offs of this change matter most, over the impact report above. The algorithm's severity and the required review stay as they are."
        readPath={`/v2/business/changes/${q(cid)}/impact/risk-memo?workspace_id=${q(ws)}`} requestPath={`/v2/business/changes/${q(cid)}/impact/risk-memo`}
        requestBody={{ workspace_id: ws }} canRequest={s.can("ai.use")} cannotReason="Needs the AI permission." />
      {board ? (
        <AdvisoryMemo testid="synthesis-memo" title="Multi-agent assessment: review board synthesis" purpose="A model summarises what the specialist reviewers agree and disagree on. Every minority or unresolved finding stays listed."
          readPath={`/v2/ma/runs/${q(String(board.run_id))}/synthesis-memo?workspace_id=${q(ws)}`} requestPath={`/v2/ma/runs/${q(String(board.run_id))}/synthesis-memo`}
          requestBody={{ workspace_id: ws }} canRequest={s.can("ai.use")} cannotReason="Needs the AI permission." />
      ) : runs && <Muted testid="synthesis-memo-none">A synthesis memo needs a finished review-board run of this change; none exists.</Muted>}
    </div>
  );
}
