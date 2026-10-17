// What stands behind a draft, said before it is asked for: which files have a current extraction, whether the relation coverage and the conflict check
// (finding) exist for THIS revision, and whether a step is waiting, running, failed or out of date. The steps are created once, for the current revision,
// by the server (deterministic, no AI). "No conflict check" is never shown as "no conflict".
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, StatePill } from "@/components/bits";
import { basename } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { useFlow } from "./flow";
import type { Rec } from "./types";

export interface Kind { state: "none" | "running" | "done" | "failed"; job_id?: string | null; reason?: string | null; older_revisions_ignored?: number; findings?: number }
export interface Coverage {
  source_revision_id?: string | null; ready: boolean;
  extraction: { files_in_scope: number; files_with_extraction: number; files_without_extraction: string[] };
  relation: Kind; finding: Kind;
}

const q = encodeURIComponent;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export const readCoverage = (ws: string) => api<Coverage>("GET", `/v2/change/coverage?workspace_id=${q(ws)}`);
const CHANGED = "ripple:coverage";
export const ensureCoverage = async (ws: string) => { const c = await api<Coverage>("POST", "/v2/change/coverage/ensure", { workspace_id: ws }); window.dispatchEvent(new Event(CHANGED)); return c; };

/** Make the steps exist and wait (about 20 s at most) until they ended. "ready": both done; "failed": a step failed; "waiting": still running (nothing was sent to the AI). */
export async function prepareCoverage(ws: string): Promise<{ result: "ready" | "failed" | "waiting"; cov: Coverage }> {
  let cov = await ensureCoverage(ws);
  for (let i = 0; i < 20; i++) {
    if (cov.ready) return { result: "ready", cov };
    if (cov.relation.state === "failed" || cov.finding.state === "failed" || cov.extraction.files_with_extraction === 0) {      // a failed step, or nothing was read: nothing to check
      window.dispatchEvent(new Event(CHANGED));
      return { result: "failed", cov };
    }
    await sleep(1000);
    cov = await readCoverage(ws);
  }
  window.dispatchEvent(new Event(CHANGED));
  return { result: cov.ready ? "ready" : "waiting", cov };
}

const WORD: Record<Kind["state"], string> = { none: "Not run yet", running: "Running", done: "Done", failed: "Failed" };

export function useCoverage() {
  const { ws, data } = useFlow();
  const [cov, setCov] = React.useState<Coverage | null>(null);
  const [err, setErr] = React.useState(false);
  const polls = React.useRef(0);
  const head = data?.head.source_revision_id;
  const read = React.useCallback(async () => { try { setCov(await readCoverage(ws)); setErr(false); } catch { setErr(true); } }, [ws]);
  React.useEffect(() => { polls.current = 0; void read(); }, [read, head, data?.draft?.job_id]);
  React.useEffect(() => {          // a step of the page (Generate, Run the checks) changed the checks: read them again, nothing else
    const on = () => { polls.current = 0; void read(); };
    window.addEventListener(CHANGED, on);
    return () => window.removeEventListener(CHANGED, on);
  }, [read]);
  const running = cov?.relation.state === "running" || cov?.finding.state === "running";
  React.useEffect(() => {          // bounded: it stops by itself, and the panel offers "Check again"
    if (!running || polls.current >= 30) return;
    const t = window.setTimeout(() => { polls.current += 1; void read(); }, 2000);
    return () => window.clearTimeout(t);
  }, [running, cov, read]);
  return { cov, err, read, gaveUp: running && polls.current >= 30, reset: () => { polls.current = 0; } };
}

export function CoveragePanel() {
  const { ws, data, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const { cov, err, read, gaveUp, reset } = useCoverage();
  if (!data?.intent || data.intent.status !== "CONFIRMED") return null;
  if (!cov) return err ? <Muted testid="coverage-error">The checks behind the draft could not be read. <button className="underline" onClick={() => void read()}>Check again</button></Muted> : null;
  const ex = cov.extraction;
  const mayRun = s.can("change.prepare");        // the deterministic checks over stored extraction output; retrying a failed job stays with source.import
  const note = (k: Kind, what: string) => (k.state === "failed" ? `${what}: failed (${k.reason ?? "no reason recorded"}). Try again below.` : null);
  return (
    <div data-testid="coverage-panel" data-ready={String(cov.ready)} className="rounded-md border border-border p-3 grid gap-1 text-sm">
      <h3 className="font-semibold">Before the draft: what stands behind it</h3>
      <p data-testid="coverage-files">Files read: <strong>{ex.files_with_extraction} of {ex.files_in_scope}</strong> have a current extraction
        {ex.files_without_extraction.length > 0 && <> - not read: {ex.files_without_extraction.map((x) => basename(x)).join(", ")}</>}.</p>
      <p data-testid="coverage-relation" data-state={cov.relation.state}>Relations between rules (used by the impact analysis): <StatePill state={cov.relation.state === "done" ? "COMPLETED" : cov.relation.state === "failed" ? "FAILED" : cov.relation.state === "running" ? "RUNNING" : "NONE"} label={WORD[cov.relation.state]} /></p>
      <p data-testid="coverage-finding" data-state={cov.finding.state}>Conflict and gap check between the sources: <StatePill state={cov.finding.state === "done" ? "COMPLETED" : cov.finding.state === "failed" ? "FAILED" : cov.finding.state === "running" ? "RUNNING" : "NONE"} label={cov.finding.state === "done" ? `Done: ${cov.finding.findings ?? 0} finding(s)` : WORD[cov.finding.state]} /></p>
      {(cov.relation.older_revisions_ignored || cov.finding.older_revisions_ignored) ? <Muted>Results of earlier revisions exist and are NOT used for this one.</Muted> : null}
      {cov.finding.state !== "done" && <Callout tone="warn" testid="coverage-nofinding"><span>{cov.finding.state === "running" ? "The conflict check is running; the draft waits for it." : "Without the conflict check the draft cannot say that the sources agree: no check is NOT the same as no conflict."}</span></Callout>}
      {[note(cov.relation, "Relation coverage"), note(cov.finding, "Conflict check")].filter(Boolean).map((t, i) => <Callout key={i} tone="danger" testid="coverage-failed"><span>{t}</span></Callout>)}
      {gaveUp && <Muted>Still running after a minute. <button className="underline" onClick={() => { reset(); void read(); }}>Check again</button> (this only reads the status).</Muted>}
      {mayRun && (cov.relation.state === "none" || cov.finding.state === "none" || cov.relation.state === "failed" || cov.finding.state === "failed") && (
        <div className="flex flex-wrap gap-2">
          {(cov.relation.state === "none" || cov.finding.state === "none") && (
            <Button data-testid="action-run-coverage" disabled={pending} onClick={async () => { if (await run(() => ensureCoverage(ws))) { notify.status("The checks were started (no AI call)."); reset(); await read(); await refresh(); } }}>Run the checks now (no AI call)</Button>
          )}
          {[cov.relation, cov.finding].filter((k) => k.state === "failed" && k.job_id && s.can("source.import")).map((k) => (
            <Button key={k.job_id} data-testid="action-retry-coverage" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${q(k.job_id!)}/retry`, { workspace_id: ws }), { stale: true })) { reset(); await read(); } }}>Try the failed check again</Button>
          ))}
        </div>
      )}
    </div>
  );
}

export type { Rec };
