// X2a: read a declared repository into a new source revision (ext/1). A local repository is read by a git.sync job; a declared remote is first fetched
// into a managed mirror by a git.fetch job, which then starts the git.sync job. The list shows only the repositories this member may see, never a location.
// The person starts one read; the jobs run on the server (no automatic retry of the request); a refusal is worded from its stable code.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Muted, StatePill, Tech, TechField } from "@/components/bits";
import { jobState } from "@/features/changes/types";
import { refusalOf, refusalWords } from "@/lib/refusal";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const STATE_WORDS: Record<string, string> = { QUEUED: "Waiting to run", PENDING_APPROVAL: "Waiting to run", RUNNING: "Running", COMPLETED: "Done", FAILED: "Failed", CANCELLED: "Cancelled" };

function JobLine({ ws, id, label, onDone }: { ws: string; id: string; label: string; onDone: (job: Rec) => void }) {
  const [job, setJob] = React.useState<Rec | null>(null);
  const [gone, setGone] = React.useState(false);
  React.useEffect(() => {
    let live = true, n = 0;
    let t: number | undefined;
    const read = async () => {
      try {
        const j = await api<Rec>("GET", `/v2/jobs/${q(id)}?workspace_id=${q(ws)}`);
        if (!live) return;
        setJob(j);
        const st = jobState(j as never);
        if (["COMPLETED", "FAILED", "CANCELLED"].includes(st)) { onDone(j); return; }
        if (n++ < 90) t = window.setTimeout(() => void read(), 2000);
      } catch (e) { if (live && e instanceof ApiError) setGone(true); else if (live) throw e; }
    };
    void read();
    return () => { live = false; if (t) window.clearTimeout(t); };
  }, [ws, id, onDone]);
  if (gone) return <Muted testid="repo-job-unavailable">{label}: its status could not be read.</Muted>;
  const st = job ? jobState(job as never) : "QUEUED";
  return (
    <p className="text-sm flex flex-wrap items-center gap-2" data-testid="repo-job" data-kind={String(job?.kind ?? "")} data-state={st}>
      {label}: <StatePill state={st} label={STATE_WORDS[st] ?? st} />
      {st === "FAILED" && job?.execution_reason ? <span className="text-muted">{refusalWords(String(job.execution_reason).split(":")[0])}</span> : null}
      <Tech><TechField label="job" value={id} /></Tech>
    </p>
  );
}

export function RepoSync({ ws, onSynced }: { ws: string; onSynced: () => void }) {
  const [repos, setRepos] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ReturnType<typeof refusalOf> | null>(null);
  const [refused, setRefused] = React.useState<ReturnType<typeof refusalOf> | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [jobs, setJobs] = React.useState<{ id: string; label: string }[]>([]);
  React.useEffect(() => {
    api<Rec>("GET", `/v2/sources/repositories?workspace_id=${q(ws)}`).then(setRepos).catch((e) => { if (e instanceof ApiError) setErr(refusalOf(e)); else throw e; });
  }, [ws]);
  const finished = React.useCallback((j: Rec) => {
    const res = (j.execution_result ?? {}) as Rec;
    if (j.kind === "git.fetch" && typeof res.sync_job_id === "string") setJobs((xs) => xs.some((x) => x.id === res.sync_job_id) ? xs : [...xs, { id: String(res.sync_job_id), label: "Reading the files into a new source version" }]);
    if (j.kind === "git.sync" && jobState(j as never) === "COMPLETED") onSynced();
  }, [onSynced]);
  if (err) return null;                              // the list is an extension: when it cannot be read, Sources works as before (nothing implied)
  if (!repos || !repos.configured) return null;      // nothing declared: no empty card
  const list = (repos.repositories ?? []) as Rec[];
  const start = async (r: Rec) => {
    setBusy(String(r.repo_id)); setRefused(null);
    try {
      const out = await api<Rec>("POST", "/v2/sources/git/sync", { workspace_id: ws, repo_id: r.repo_id });
      setJobs((xs) => [...xs, { id: String(out.job_id), label: out.kind === "git.fetch" ? `Fetching ${String(r.name)} from its remote` : `Reading ${String(r.name)}` }]);
    } catch (e) { if (e instanceof ApiError) setRefused(refusalOf(e)); else throw e; }
    finally { setBusy(null); }
  };
  return (
    <Card title="Read from a declared repository" testid="repo-sync">
      <Muted>Reads the repository's current commit into a new source version, through the same path as a file refresh. Nothing is written to the repository.</Muted>
      {list.length === 0 ? <Muted testid="repo-none">No declared repository is visible to your role.</Muted> : (
        <ul className="grid gap-2">
          {list.map((r) => {
            const sync = (r.sync ?? {}) as Rec;
            const hidden = sync.reason_code === "permission_missing";
            return (
              <li key={String(r.repo_id)} data-testid="repo-row" data-repo={String(r.repo_id)} className="rounded-md border border-border p-2 grid gap-1 text-sm">
                <span><strong>{String(r.name)}</strong> <span className="text-muted">({r.source === "remote" ? `remote${r.provider ? `, ${String(r.provider)}` : ""}` : "local"}; read: {(r.grants as Rec)?.read ? "allowed" : "not allowed"}; pull request: {(r.grants as Rec)?.pull_request ? "allowed" : "not allowed"})</span></span>
                {!hidden && (
                  <span className="flex flex-wrap items-center gap-2">
                    <Button data-testid="repo-sync-start" disabled={!sync.allowed || busy !== null} aria-describedby={sync.allowed ? undefined : `why-${String(r.repo_id)}`} onClick={() => void start(r)}>Read the current commit</Button>
                    {!sync.allowed && <span id={`why-${String(r.repo_id)}`} className="text-muted" data-testid="repo-sync-why">{refusalWords(String(sync.reason_code))}</span>}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {jobs.map((j) => <JobLine key={j.id} ws={ws} id={j.id} label={j.label} onDone={finished} />)}
      {refused && <Callout tone="warn" testid="repo-refused"><span data-code={refused.code ?? ""}>{refused.words}</span>{refused.requestId ? <span className="block text-xs text-muted">Reference: {refused.requestId}</span> : null}</Callout>}
    </Card>
  );
}
