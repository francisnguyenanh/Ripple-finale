import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, StatePill } from "@/components/bits";
import { jobPhrase } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useSession } from "@/session";
import { failureAdvice } from "@/lib/reasons";
import { useFlow } from "./flow";
import type { Job, Rec } from "./types";
import { isTerminal, jobState, phase } from "./types";

const NONE_TEXT: Record<string, string> = {
  draft: "Not started. The request must be confirmed first.", impact: "Not started. It needs the AI draft first.",
  simulation: "Not started. Run the impact analysis first.", proposal: "Not started. Needs the simulation first.",
  review: "Not started. Needs a proposal first.", approval: "Not started. Needs a finished review first.",
  apply: "Not started. Needs an approved change first.", query: "Not started.", whatif: "Not started.",
};

export function stateText(name: string, s: string, confirmed = false): string {
  if (s === "NONE") return name === "draft" && confirmed ? "Not started." : NONE_TEXT[name] ?? "Not started.";
  return jobPhrase(s);
}

/** Job state line. `usePhase` shows "waiting for a second person" as its own phase for jobs that spend model budget. */
export function JobLine({ name, job, usePhase = false, retry = false }: { name: string; job: Job | null; usePhase?: boolean; retry?: boolean }) {
  const { ws, refresh, data } = useFlow();
  const s = useSession();
  const { run, pending } = useAction();
  const st = job ? (usePhase ? phase(job) : jobState(job)) : "NONE";
  const confirmed = data?.intent?.status === "CONFIRMED";
  return (
    <>
    <p className="text-sm flex flex-wrap items-center gap-2">
      <span className="text-muted">Job:</span>
      <StatePill state={st} label={name === "impact" && st === "NONE" && data?.draft && jobState(data.draft) === "COMPLETED" ? "Not started. The AI draft is done: compute the relation coverage if you want it (no AI call), then run the impact analysis." : stateText(name, st, confirmed)} testid={`state-${name}`} data={{ "job-id": job?.job_id ?? "" }} />
      {job && job.execution_reason && jobState(job) === "FAILED" && s.mode === "technical" && <span className="text-muted" data-testid="job-reason">- {job.execution_reason}</span>}
      {retry && job && jobState(job) === "FAILED" && s.can("source.import") && (
        <Button data-testid="action-retry-job" title={s.aiOff ? "AI actions are switched off on this server" : undefined} disabled={pending || s.aiOff}
          onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${encodeURIComponent(job.job_id)}/retry`, { workspace_id: ws }), { stale: true })) await refresh(); }}>Try again</Button>
      )}
    </p>
    <FailureNote job={job} />
    </>
  );
}

/** Why a job failed, in words, and what to do next. Business view: the explanation; Technical view: the server's exact reason is shown on the job line. */
export function FailureNote({ job }: { job: Job | null }) {
  if (!job || jobState(job) !== "FAILED" || !job.execution_reason) return null;
  const a = failureAdvice(job.execution_reason);
  return (
    <Callout tone="danger" testid="job-failure" role="alert">
      <p><strong>This step failed.</strong> {a.what}</p>
      <p className="text-muted" data-testid="job-failure-next">{a.next}</p>
    </Callout>
  );
}
/** Second-person approval of a model call (the requester cannot allow their own run). */
export function ApproveJobButton({ job, label, testid }: { job: Job | null; label: string; testid: string }) {
  const { ws, refresh } = useFlow();
  const s = useSession();
  const { run, pending } = useAction();
  // The server refuses the person who asked; the button is simply not offered to them.
  if (!job || job.status !== "PENDING_APPROVAL" || !s.can("source.import") || (!!job.requested_by && job.requested_by === s.account)) return null;
  return (
    <Button data-testid={testid} disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${encodeURIComponent(job.job_id)}/approve`, { workspace_id: ws }), { stale: true })) await refresh(); }}>{label}</Button>
  );
}

/** Create a stage job once. The id is generated per click; a second click while pending is ignored. */
export function useCreateStage() {
  const { ws } = useFlow();
  const { run, pending } = useAction();
  const create = React.useCallback(
    (kind: string, id: string, payload: Rec) => run(() => api<Rec>("POST", "/v2/jobs", { workspace_id: ws, kind, job_id: id, payload }), { stale: true }),
    [run, ws],
  );
  return { create, pending, run };
}

export const jobRunning = (j: Job | null): boolean => !!j && !isTerminal(j);
