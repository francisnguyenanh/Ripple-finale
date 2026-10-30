import * as React from "react";
import { ArrowRight, BookCheck, FileText, HelpCircle, ListChecks, ShieldCheck } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, Muted, Tech, TechField } from "@/components/bits";
import { ResultSummary, type Notice } from "@/components/decision/ResultSummary";
import { NextActionPanel } from "@/components/decision/NextActionPanel";
import { humanize } from "@/lib/fmt";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";
import { WorkInbox, sectionsOf, useInbox } from "@/features/inbox/WorkInbox";
import { PageHeader } from "./Page";
import { buildSummary, ctaRepeatsFirstRow, nextStep, type Summary } from "./overviewModel";

type Rec = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
const q = encodeURIComponent;

const RECENT: Record<string, string> = {
  SOURCE_PUBLISHED: "made a new version of the files current", INTENT_PARSED: "saved a change request", INTENT_CONFIRMED: "confirmed a change request",
  DECISION_RECORDED: "recorded a decision", RESTORE_APPROVED: "approved a restore", HITL_ANSWER_RECORDED: "wrote an answer", SANDBOX_LEASE_ACQUIRED: "started a demo session",
};

// A failed optional read is `undefined` here and stays "unknown" in the summary: it is never shown as 0 or as "nothing waiting".
const ok = <T,>(p: Promise<T>): Promise<T | undefined> => p.catch(() => undefined);

async function summarise(ws: string): Promise<Summary> {
  const head = await api<Rec>("GET", `/v2/workspaces/${q(ws)}/head`);
  const [rev, overview, queue, audit, business] = await Promise.all([
    head.source_revision_id ? ok(api<Rec>("GET", `/v2/sources/revisions/${q(head.source_revision_id)}?workspace_id=${q(ws)}`)) : Promise.resolve(null),
    ok(api<Rec>("GET", `/v2/knowledge/overview?workspace_id=${q(ws)}`)),
    ok(api<Rec>("GET", `/v2/hitl/queue?workspace_id=${q(ws)}`)),
    ok(api<Rec>("GET", `/v2/audit/${q(ws)}`)),
    ok(api<Rec>("GET", `/v2/business/knowledge?workspace_id=${q(ws)}`)),
  ]);
  return buildSummary({ head, rev: rev ?? null, overview, queue, audit, business }, (t) => !!RECENT[t]);
}

// SCN-16: Current state is a quiet strip of workspace figures under the three My work counts (which stay the focus); every figure, unit and link is kept.
function Stat({ icon: Icon, value, label, href }: { icon: typeof FileText; value: string | number | null; label: string; href: string }) {
  return (
    <a href={href} className="grid content-start gap-0.5 min-w-0 bg-surface px-4 py-3 hover:bg-surface-2 focus-visible:-outline-offset-2 last:col-span-2 lg:last:col-span-1">
      <span className="flex items-center gap-2">
        <Icon size={16} aria-hidden="true" className="shrink-0 text-muted" />
        <span className={value === null ? "text-base font-semibold text-muted" : "text-xl font-semibold tnum"} data-state={value === null ? "unavailable" : "known"}>{value === null ? "Unavailable" : value}</span>
      </span>
      <span className="text-sm text-muted">{label}</span>
    </a>
  );
}
const STRIP = "grid grid-cols-2 lg:grid-cols-5 gap-px overflow-hidden rounded-lg border border-border bg-border";

// While loading: the same layout as the page (My work, four figures, recent activity), with placeholder blocks and no value, so nothing jumps and no number is invented.
function Block({ className }: { className: string }) {
  return <span aria-hidden="true" className={`block rounded bg-surface-2 motion-safe:animate-pulse ${className}`} />;
}

function OverviewSkeleton({ inbox }: { inbox: boolean }) {
  return (
    <div className="grid gap-4" data-testid="overview-skeleton">
      <p role="status" className="sr-only">Loading the workspace summary...</p>
      {inbox && <Card title="My work"><Block className="h-16 w-full" /></Card>}
      <Card title="Current state">
        <div className={STRIP}>
          {[0, 1, 2, 3, 4].map((i) => <div key={i} className="bg-surface px-4 py-3 grid gap-2 last:col-span-2 lg:last:col-span-1"><Block className="h-6 w-16" /><Block className="h-4 w-32" /></div>)}
        </div>
      </Card>
      <Card title="Recent activity"><Block className="h-4 w-2/3" /><Block className="h-4 w-1/2" /></Card>
    </div>
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
  const inboxStatus = inboxState.inbox ? "ready" : inboxState.failed ? "failed" : "loading";
  const next = nextStep(sum, { count: mine.length, first: mine[0] ? (mine[0].why ?? mine[0].label) : undefined }, inboxStatus);
  const firstHref = mine.length ? hashFor(ws, mine[0].page ?? "changes", undefined, mine[0].params) : "";     // a business change opens on its own page
  const unknownParts = sum ? ([sum.files === null && "files", sum.rules === null && "rules", sum.questions === null && "questions", sum.events === null && "recent activity"].filter(Boolean) as string[]) : [];
  const notices: Notice[] = [];
  if (unknownParts.length) notices.push({ key: "unread", tone: "warn", blocking: true, text: <span data-testid="overview-unavailable">Some figures could not be read ({unknownParts.join(", ")}). They are shown as Unavailable, not as zero. <Button variant="secondary" size="sm" onClick={() => s.reload()} data-testid="overview-refresh">Try again</Button></span> });
  if (sum?.coveragePartial) notices.push({ key: "partial", tone: "warn", text: <span data-testid="overview-partial"><strong>Partly covered.</strong> Not every file was admitted and read. Do not treat missing results as "nothing found". <a className="text-primary underline" href={hashFor(ws, "sources")}>See Sources</a>.</span> });
  const inb = inboxState.inbox;
  const list = inb ? { first: sectionsOf(inb).mine[0], unavailable: inb.unavailable ?? 0, cut: inb.coverage?.complete === false && (inb.coverage.truncated ?? []).length > 0 } : null;
  const repeat = ctaRepeatsFirstRow(next, mine[0], list);                    // identity of the request, never its wording
  const action = next.to ? <Button variant="primary" asChild><a href={firstHref || hashFor(ws, next.to, undefined, next.params)} data-testid="overview-cta">{next.label}<ArrowRight size={16} aria-hidden="true" /></a></Button> : undefined;
  return (
    <>
      <PageHeader id="overview" />
      <NextActionPanel compact={repeat} action={action}
        reason={repeat ? `${mine.length === 1 ? "1 request is" : `${mine.length} requests are`} waiting for you; the first one is at the top of My work below.` : next.text} />
      {err && <Muted testid="state-error">Some of this summary could not be read (HTTP {err.status}). The other pages still work.</Muted>}
      {!sum && !err && <>{inboxState.inbox && <WorkInbox ws={ws} state={inboxState} counts />}<OverviewSkeleton inbox={!inboxState.inbox && !inboxState.failed} /></>}
      {err && !sum && <WorkInbox ws={ws} state={inboxState} counts />}
      {sum && (
        <div className="grid gap-4">
          <WorkInbox ws={ws} state={inboxState} counts />
          <ResultSummary title="Current state" scopeLabel="Figures read from this workspace" warnings={notices} availability={unknownParts.length ? "unavailable" : "ready"} testid="overview-state">
            <div className={STRIP} data-testid="overview-stats">
              <Stat icon={FileText} value={sum.files} label="files in this workspace" href={hashFor(ws, "sources")} />
              <Stat icon={BookCheck} value={sum.businessRules ?? null} label="rules in the accepted business knowledge" href={hashFor(ws, "questions", undefined, { ui_section: "rules" })} />
              <Stat icon={ListChecks} value={sum.rules === null || sum.confirmed === null ? null : `${sum.confirmed} / ${sum.rules}`} label="rules the AI read from files, confirmed by a person" href={hashFor(ws, "explorer")} />
              <Stat icon={HelpCircle} value={sum.questions} label="questions waiting for a person" href={hashFor(ws, "questions")} />
              <Stat icon={ShieldCheck} value={sum.openFindings} label="open findings or conflicts" href={hashFor(ws, "questions")} />
            </div>
          </ResultSummary>
          <Card title="Recent activity">
            {sum.events === null ? <Muted testid="activity-unavailable">Recent activity could not be read. This is not the same as nothing having happened.</Muted>
              : sum.events.length ? (
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
