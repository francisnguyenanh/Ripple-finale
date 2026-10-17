import * as React from "react";
import { ArrowRight, FileText, HelpCircle, ListChecks, ShieldCheck } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, Muted, Tech, TechField } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { humanize } from "@/lib/fmt";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";
import { WorkInbox, useInbox } from "@/features/inbox/WorkInbox";
import { PageHeader } from "./Page";

type Rec = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
const q = encodeURIComponent;

interface Summary { files: number | null; coveragePartial: boolean; rules: number; confirmed: number; questions: number; openFindings: number; events: Rec[] }

const RECENT: Record<string, string> = {
  SOURCE_PUBLISHED: "made a new version of the files current", INTENT_PARSED: "saved a change request", INTENT_CONFIRMED: "confirmed a change request",
  DECISION_RECORDED: "recorded a decision", RESTORE_APPROVED: "approved a restore", HITL_ANSWER_RECORDED: "wrote an answer", SANDBOX_LEASE_ACQUIRED: "started a demo session",
};

async function summarise(ws: string): Promise<Summary> {
  const head = await api<Rec>("GET", `/v2/workspaces/${q(ws)}/head`);
  const [rev, overview, queue, audit] = await Promise.all([
    head.source_revision_id ? api<Rec>("GET", `/v2/sources/revisions/${q(head.source_revision_id)}?workspace_id=${q(ws)}`).catch(() => null) : Promise.resolve(null),
    api<Rec>("GET", `/v2/knowledge/overview?workspace_id=${q(ws)}`).catch(() => ({}) as Rec),
    api<Rec>("GET", `/v2/hitl/queue?workspace_id=${q(ws)}`).catch(() => ({ items: [] }) as Rec),
    api<Rec>("GET", `/v2/audit/${q(ws)}`).catch(() => ({ events: [] }) as Rec),
  ]);
  const claims = (overview.claims ?? []) as Rec[];
  const confirmed = ((overview.decisions ?? []) as Rec[]).filter((d) => d.active && d.action === "confirm").length;
  const qs = new Set(((queue.items ?? []) as Rec[]).map((i) => i.question_id));
  return {
    files: rev ? (rev.artifacts ?? []).length : head.source_revision_id ? null : 0,
    coveragePartial: !!rev?.coverage && (!rev.coverage.complete || !rev.coverage.admission_complete),
    rules: claims.length, confirmed, questions: qs.size,
    openFindings: ((overview.findings ?? []) as Rec[]).filter((f) => !f.decision).length,
    events: ((audit.events ?? []) as Rec[]).filter((e) => RECENT[e.event_type]).slice(-5).reverse(),
  };
}

function Stat({ icon: Icon, value, label, href }: { icon: typeof FileText; value: string | number; label: string; href: string }) {
  return (
    <a href={href} className="rounded-lg border border-border bg-surface p-4 grid gap-1 hover:bg-surface-2 min-w-0">
      <Icon size={18} aria-hidden="true" className="text-muted" />
      <span className="text-2xl font-semibold tnum">{value}</span>
      <span className="text-sm text-muted">{label}</span>
    </a>
  );
}

export function Overview() {
  const s = useSession();
  const ws = s.route.ws!;
  const [sum, setSum] = React.useState<Summary | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    let live = true;
    summarise(ws).then((x) => { if (live) { setSum(x); setErr(null); } }).catch((e) => { if (live && e instanceof ApiError) setErr(e); });
    return () => { live = false; };
  }, [ws]);
  const h = s.head;
  const inboxState = useInbox(ws);
  const mine = (inboxState.inbox?.todo ?? []).filter((x) => !x.optional);
  let next = { text: "Loading what to do next...", to: "", label: "" };
  if (sum) {
    if (!sum.files) next = { text: "No files have been added yet. Add the documents, code or workbooks this workspace should know about.", to: "sources", label: "Add files" };
    else if (sum.rules === 0) next = { text: "Files are loaded but the AI has not read rules from them yet. Open a file and choose Analyze.", to: "sources", label: "Open Sources" };
    else if (sum.questions > 0) next = { text: `${sum.questions} question${sum.questions === 1 ? " is" : "s are"} waiting for a person to confirm or answer.`, to: "questions", label: "Review questions" };
    else next = { text: "Describe what you want to change in your own words. Ripple asks if anything is unclear, then follows it through review.", to: "changes", label: "Start a change" };
  }
  // The same list as "My work" below decides what comes first, so Overview, My work and Changes never disagree about who acts next.
  if (mine.length) next = { text: `${mine.length} item${mine.length === 1 ? " is" : "s are"} waiting for you in My work: ${mine[0].why ?? mine[0].label}.`, to: "changes", label: "Open the first item" };
  const firstHref = mine.length ? hashFor(ws, "changes", undefined, mine[0].params) : "";
  return (
    <>
      <PageHeader id="overview" action={next.to ? <Button variant="primary" asChild><a href={firstHref || hashFor(ws, next.to)} data-testid="overview-cta">{next.label}<ArrowRight size={16} aria-hidden="true" /></a></Button> : undefined} next={next.text} />
      {err && <Muted testid="state-error">Some of this summary could not be read (HTTP {err.status}). The other pages still work.</Muted>}
      {!sum && !err && <StatusBadge status="loading" />}
      {sum && (
        <div className="grid gap-4">
          <WorkInbox ws={ws} state={inboxState} />
          <section aria-label="Current state" className="grid gap-3 grid-cols-2 lg:grid-cols-4">
            <Stat icon={FileText} value={sum.files ?? "?"} label="files in this workspace" href={hashFor(ws, "sources")} />
            <Stat icon={ListChecks} value={`${sum.confirmed} / ${sum.rules}`} label="rules confirmed by a person" href={hashFor(ws, "explorer")} />
            <Stat icon={HelpCircle} value={sum.questions} label="questions waiting for a person" href={hashFor(ws, "questions")} />
            <Stat icon={ShieldCheck} value={sum.openFindings} label="open findings or conflicts" href={hashFor(ws, "questions")} />
          </section>
          {sum.coveragePartial && <Card testid="overview-partial"><p className="text-sm"><strong>Partly covered.</strong> Not every file was admitted and read. Do not treat missing results as "nothing found". <a className="text-primary underline" href={hashFor(ws, "sources")}>See Sources</a>.</p></Card>}
          <Card title="Recent activity">
            {sum.events.length ? (
              <ul className="grid gap-2 text-sm">
                {sum.events.map((e, i) => <li key={i}><strong>{e.actor_id || "the system"}</strong> {RECENT[e.event_type] ?? humanize(e.event_type).toLowerCase()} <span className="text-muted">{e.created_at ? new Date(e.created_at).toLocaleString() : ""}</span></li>)}
              </ul>
            ) : <Muted>Nothing has happened yet.</Muted>}
            <div><a className="text-primary underline text-sm" href={hashFor(ws, "history")}>Open History</a></div>
          </Card>
          <Tech>
            <Card title="Revisions" testid="technical-block">
              <TechField label="source revision" value={h?.source_revision_id ?? "none"} />
              <TechField label="knowledge revision" value={h?.knowledge_revision_id ?? "none"} />
              <TechField label="decision epoch" value={h?.decision_epoch ?? ""} />
            </Card>
          </Tech>
        </div>
      )}
    </>
  );
}
