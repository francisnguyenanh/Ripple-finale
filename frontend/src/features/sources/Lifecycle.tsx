// What has really happened to each source file, stage by stage, read from `/v2/sources/lifecycle`. The stages are never merged:
// added, read by the parser, analysed by the AI, rules found (pending / confirmed) and the knowledge version are separate facts.
import * as React from "react";
import { Check, CircleAlert, CircleDashed, Clock, Loader } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSession } from "@/session";
import { Callout, Card, Muted, Tech, TechField } from "@/components/bits";
import { hashFor } from "@/lib/route";
import type { Rec } from "@/features/changes/types";

export const ANALYSIS_LABEL: Record<string, string> = {
  not_analysed: "Not analysed by the AI yet",
  pending_approval: "Waiting for a person to allow this AI run",
  queued: "Queued for analysis",
  running: "Being analysed now",
  analysed: "Analysed",
  failed: "The analysis failed",
  cancelled: "The analysis was cancelled",
  stale: "Out of date: it was analysed on an earlier version of the sources and has not been re-read yet",
};
const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "");

export function providerLine(p: Rec | undefined): string {
  if (!p) return "";
  if (p.mode === "SIMULATED") return "SIMULATED: a stub, not a model. Nothing was sent to an AI and nothing was spent.";
  if (p.mode === "LIVE_DISARMED") return "LIVE, NOT ARMED: no AI is called until the operator arms this server. Nothing is simulated and nothing was sent to an AI.";
  if (p.mode === "LIVE") return "LIVE: the AI provider is used. Each run shows its cost and the model it recorded.";
  return "Not declared: this server did not say whether its AI is live or simulated, so a recorded model name is not proof of a live call.";
}

function Row({ icon, testid, state, children }: { icon: React.ReactNode; testid: string; state: string; children: React.ReactNode }) {
  return <li data-testid={testid} data-state={state} className="flex items-start gap-2"><span className="mt-0.5 shrink-0">{icon}</span><span className="min-w-0">{children}</span></li>;
}

export function FileStages({ row, provider }: { row: Rec; provider?: Rec }) {
  const ex = (row.extraction ?? {}) as Rec;
  const cl = (row.claims ?? {}) as Rec;
  const pr = (row.parse ?? {}) as Rec;
  const st = (ex.state as string) ?? "not_analysed";
  const spent = ex.cost_usd !== null && ex.cost_usd !== undefined && ex.cost_usd !== "";
  const analysisIcon = st === "analysed" ? <Check size={16} aria-hidden="true" className="text-success" />
    : st === "failed" ? <CircleAlert size={16} aria-hidden="true" className="text-danger" />
    : st === "running" || st === "queued" ? <Loader size={16} aria-hidden="true" />
    : st === "pending_approval" || st === "stale" ? <Clock size={16} aria-hidden="true" className="text-warning" /> : <CircleDashed size={16} aria-hidden="true" className="text-muted" />;
  return (
    <ol aria-label="What has happened to this file" className="grid gap-1.5 text-sm" data-testid="file-stages">
      <Row icon={<Check size={16} aria-hidden="true" className="text-success" />} testid="stage-imported" state="done">Added to this workspace</Row>
      <Row icon={pr.state === "parsed" ? <Check size={16} aria-hidden="true" className="text-success" /> : <CircleDashed size={16} aria-hidden="true" className="text-muted" />} testid="stage-parsed" state={pr.state as string}>
        {pr.state === "parsed" ? `Read by the parser: ${pr.observations} piece${pr.observations === 1 ? "" : "s"} of content found` : "Nothing readable was found (the file type is not supported or the file is empty)"}
      </Row>
      <Row icon={analysisIcon} testid="stage-analysis" state={st}>
        <span><strong>{ANALYSIS_LABEL[st] ?? st}</strong>
          {st === "analysed" && ex.last_run_at ? ` on ${when(ex.last_run_at as string)}` : ""}
          {st === "failed" && ex.reason ? `: ${ex.reason}` : ""}
          {st === "stale" ? ". Its rules are not shown as current." : ""}</span>
        {(st === "analysed" || st === "stale" || st === "failed" || st === "running") && (
          <span className="block text-muted" data-testid="stage-analysis-provenance">
            {provider?.mode === "SIMULATED" ? "SIMULATED analysis" : provider?.mode === "LIVE" ? "Live analysis" : provider?.mode === "LIVE_DISARMED" ? "Live server (not armed)" : "Provider not declared"}
            {ex.model_recorded ? `, model recorded: ${ex.model_recorded}` : ""}
            {spent ? `, cost ${ex.cost_usd} USD` : ""}
            {st === "stale" ? ", made on an earlier version of the file, which has changed since" : ""}
            {st === "analysed" && ex.file_unchanged_since_analysis ? ", made at an earlier revision; the file has not changed since, so it is still valid" : ""}
          </span>
        )}
      </Row>
      <Row icon={cl.total ? <CircleDashed size={16} aria-hidden="true" className={cl.pending ? "text-warning" : "text-success"} /> : <CircleDashed size={16} aria-hidden="true" className="text-muted" />} testid="stage-rules" state={cl.total ? (cl.pending ? "pending" : "confirmed") : "none"}>
        {cl.total ? <span data-testid="stage-rules-text">{cl.total} rule{cl.total === 1 ? "" : "s"} found: <strong>{cl.confirmed} confirmed</strong> by a person, <strong>{cl.pending} waiting</strong> for a person. The file itself is not confirmed or rejected as a whole.</span>
          : st === "analysed" ? "The analysis found no rules in this file." : "No rules yet."}
      </Row>
      {st === "analysed" && /\.py$/.test(String(row.logical_path ?? "")) && (
        <li data-testid="code-coverage-limit" className="text-xs text-muted list-none">
          Coverage limit: the AI read this code as text and lists only the rules it chose to extract. A condition that is not listed may still be in the code, so its absence is not proof. Hints about code in an impact analysis come from a deterministic text scan, not from the AI.
        </li>
      )}
      <Tech>
        <li className="text-xs text-muted break-all"><TechField label="analysis job" value={ex.job_id ?? "none"} /><TechField label="analysed source revision" value={ex.analysed_source_revision_id ?? "none"} /><TechField label="analysis is on the current revision" value={String(!!ex.on_current_revision)} /></li>
      </Tech>
    </ol>
  );
}

export function LifeSummary({ life, paths }: { life: Rec; paths: string[] }) {
  const t = (life.totals ?? {}) as Rec;
  const k = (life.knowledge ?? {}) as Rec;
  const harborOnly = paths.length > 0 && paths.every((p) => p.startsWith("harbor_demo/"));
  const waiting = (t.pending_approval ?? 0) + (t.queued ?? 0) + (t.running ?? 0);
  return (
    <Card title="What has been done with these files" testid="lifecycle-summary">
      <p className="text-sm" data-testid="source-count"><strong>{t.imported}</strong> source file{t.imported === 1 ? " is" : "s are"} in the current version of this workspace. The numbers below come from what is stored right now.</p>
      <ul className="grid gap-1 text-sm" aria-label="Counts by stage">
        <li data-testid="count-parsed">Read by the parser: <strong>{t.parsed}</strong> of {t.imported}</li>
        <li data-testid="count-analysed">Analysed by the AI (current version of the file): <strong>{t.analysed}</strong> of {t.imported}</li>
        {t.stale > 0 && <li data-testid="count-stale">Out of date (analysed before the file changed): <strong>{t.stale}</strong></li>}
        {waiting > 0 && <li data-testid="count-waiting">Waiting or running: <strong>{waiting}</strong></li>}
        {t.failed > 0 && <li data-testid="count-failed">Analysis failed: <strong>{t.failed}</strong></li>}
        <li data-testid="count-not-analysed">Not analysed yet: <strong>{t.not_analysed}</strong></li>
        <li data-testid="count-rules">Rules found: <strong>{t.claims_total}</strong> - confirmed by a person: <strong>{t.claims_confirmed}</strong>, waiting for a person: <strong>{t.claims_pending}</strong></li>
      </ul>
      <p className="text-sm" data-testid="knowledge-state" data-state={k.state}>
        Knowledge version: {k.state === "built_on_current_sources" ? "built on the current files." : k.state === "built_on_older_sources" ? "built on older files, so it does not yet include the current version." : k.state === "no_sources" ? "there are no files yet." : "no knowledge version has been accepted yet."}{" "}
        <span className="text-muted">Accepting knowledge is its own decision. An analysis finishing does not accept anything.</span>
      </p>
      <Muted testid="provider-line">AI mode: {providerLine(life.provider as Rec)}</Muted>
      <Callout testid="subset-note">
        {harborOnly
          ? <span>This workspace holds a <strong>selected part of the public Harbor demo files</strong> ({t.imported} file{t.imported === 1 ? "" : "s"}), not the whole demo bundle. Anything that was not imported has not been read and is not part of any result.</span>
          : <span>These are the files that were imported. Files that were not imported have not been read and are not part of any result. Add more files with Import source files below.</span>}
      </Callout>
      <Tech><TechField label="source revision" value={life.source_revision_id} /><TechField label="knowledge revision" value={k.knowledge_revision_id ?? "none"} /><TechField label="decision epoch" value={k.decision_epoch ?? ""} /></Tech>
    </Card>
  );
}

/** The right button for the real state of the file: Analyze / Analyze again / Analyze latest version / Re-analyze (asks first) / View analysis status. */
export function AnalysisButtons({ ws, row, canRun, onStart, busy }: { ws: string; row: Rec; canRun: boolean; onStart: () => void; busy: boolean }) {
  const aiOff = useSession().aiOff;
  const [ask, setAsk] = React.useState(false);
  const ex = (row.extraction ?? {}) as Rec;
  const st = (ex.state as string) ?? "not_analysed";
  const status = ex.job_id ? <a data-testid="view-analysis-status" className="text-primary underline text-sm" href={hashFor(ws, "sources", ex.job_id as string)}>View analysis status</a> : null;
  if (st === "pending_approval" || st === "queued" || st === "running") return status;
  if (st === "analysed")
    return (
      <>
        {canRun && !ask && <Button data-testid="action-reanalyze" title={aiOff ? "AI actions are switched off on this server" : undefined} disabled={busy || aiOff} onClick={() => setAsk(true)}>Re-analyze</Button>}
        {status}
        {canRun && ask && (
          <div role="group" aria-label="Confirm re-analysis" data-testid="reanalyze-confirm" className="basis-full rounded-md border border-border bg-surface-2 p-3 grid gap-2 text-sm">
            <p>This starts a <strong>new</strong> AI analysis of this file. The current result stays until the new one is ready. When the AI is live it uses budget within the allocation.</p>
            <div className="flex gap-2">
              <Button variant="primary" data-testid="action-reanalyze-confirm" title={aiOff ? "AI actions are switched off on this server" : undefined} disabled={busy || aiOff} onClick={() => { setAsk(false); onStart(); }}>Start a new analysis</Button>
              <Button onClick={() => setAsk(false)}>Cancel</Button>
            </div>
          </div>
        )}
      </>
    );
  if (!canRun) return status;
  return (
    <>
      <Button data-testid="action-analyze" title={aiOff ? "AI actions are switched off on this server" : undefined} disabled={busy || aiOff} onClick={onStart}>{st === "stale" ? "Analyze latest version" : st === "failed" ? "Analyze again" : "Analyze"}</Button>
      {status}
    </>
  );
}
