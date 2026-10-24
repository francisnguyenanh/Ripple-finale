// A stored plain-English wording of a verified result, shown for the CURRENT snapshot only. Reading it never calls a model: the text
// is stored once (one job under the member's AI permission) and every render, poll, tab switch or Business/Technical toggle only reads it.
import * as React from "react";
import { Sparkles } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, StatePill, Tech } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;

interface Exp {
  status: "none" | "waiting_for_approval" | "running" | "done" | "failed" | "cancelled";
  job_id: string | null; text: string | null; label?: string; model?: string; reason?: string | null;
  snapshot: { key: string; source_revision_id?: string | null; decision_epoch?: number; input_hash?: string };
  out_of_date?: { text: string; snapshot: Rec };
  caveats?: { code: string; why?: string }[];
  currentness?: { status: "CURRENT" | "HISTORICAL" | "UNKNOWN"; facts_source_revision_id?: string | null; head_source_revision_id?: string | null };
}

export function ExplanationCard({ ws, subject, subjectId, title = "AI explanation" }: { ws: string; subject: "approval" | "change_result"; subjectId: string; title?: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const [exp, setExp] = React.useState<Exp | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const polls = React.useRef(0);
  const load = React.useCallback(async () => {
    try { setExp(await api<Exp>("GET", `/v2/explanations?workspace_id=${q(ws)}&subject=${subject}&subject_id=${q(subjectId)}`)); setErr(null); }
    catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws, subject, subjectId]);
  React.useEffect(() => { polls.current = 0; void load(); }, [load]);
  const waiting = exp?.status === "waiting_for_approval" || exp?.status === "running";
  React.useEffect(() => {
    if (!waiting || polls.current >= 40) return;
    const t = window.setTimeout(() => { polls.current += 1; void load(); }, 3000);
    return () => window.clearTimeout(t);
  }, [waiting, exp, load]);

  if (err) return err.status === 404 || err.status === 409 ? null : <Muted>The explanation could not be read (HTTP {err.status}).</Muted>;
  if (!exp) return null;
  return (
    <section data-testid="ai-explanation" data-state={exp.status} aria-label={title} className="rounded-md border border-border bg-surface-2 p-3 grid gap-2">
      <h3 className="text-sm font-semibold flex items-center gap-2"><Sparkles size={14} aria-hidden="true" />{title}</h3>
      {(exp.caveats ?? []).some((c) => c.code === "simulation_not_run") && (
        <p data-testid="ai-explanation-caveat" className="rounded-md border border-border bg-surface px-2 py-1 text-xs">
          No simulation ran for this change ({String((exp.caveats ?? [])[0]?.why ?? "")}). Read this wording as a summary of the review, not as an impact result.
        </p>
      )}
      {exp.currentness?.status === "HISTORICAL" && (
        <p data-testid="ai-explanation-historical" className="rounded-md border border-border bg-surface px-2 py-1 text-xs">
          <StatePill state="STALE" label="Older revision" /> These facts come from an earlier source revision, not the current one. Read this as history, not as the current state.
        </p>
      )}
      {exp.status === "done" && exp.text && (
        <>
          <p data-testid="ai-explanation-text" data-ai-text className="text-sm">{exp.text}</p>
          <p className="text-xs text-muted">AI-written, based on verified data. It phrases facts the system already checked and decides nothing.</p>
          <Tech><p className="text-xs text-muted break-all">snapshot {exp.snapshot.key} - source revision {exp.snapshot.source_revision_id} - decision epoch {exp.snapshot.decision_epoch} - model {exp.model}</p></Tech>
        </>
      )}
      {exp.status === "none" && exp.out_of_date && (
        <div data-testid="ai-explanation-stale" className="grid gap-1">
          <StatePill state="STALE" label="Out of date" />
          <p className="text-sm text-muted" data-ai-text>{exp.out_of_date.text}</p>
          <Muted>This wording describes an earlier state. The facts changed since, so it is not shown as current.</Muted>
        </div>
      )}
      {exp.status === "none" && (
        <>
          <Muted>No explanation has been written for the current state. Writing one uses the paid model (one call, within your AI permission).</Muted>
          {s.can("ai.use") && <div><Button data-testid="ai-explanation-request" title={aiWhy(s, "explain")} disabled={pending || !s.aiKind("explain")} onClick={async () => { if (await run(() => api("POST", "/v2/explanations", { workspace_id: ws, subject, subject_id: subjectId }), { stale: true })) await load(); }}>Write an AI explanation</Button></div>}
        </>
      )}
      {exp.status === "waiting_for_approval" && (
        <>
          <Muted>In this shared trial workspace a different person has to allow this AI run. Nothing was sent to the AI.</Muted>
          {s.can("source.import") && exp.job_id && <div><Button data-testid="ai-explanation-approve" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${q(exp.job_id as string)}/approve`, { workspace_id: ws }), { stale: true })) await load(); }}>Allow this AI run</Button></div>}
        </>
      )}
      {exp.status === "running" && <Muted>The explanation is being written. This page checks by itself.</Muted>}
      {(exp.status === "failed" || exp.status === "cancelled") && <Callout tone="warn">The explanation was not produced ({exp.reason ?? exp.status}). The verified facts above are unchanged.</Callout>}
    </section>
  );
}
