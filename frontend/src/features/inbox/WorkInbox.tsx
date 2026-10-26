// "My work" (RUX-04, SCN-03): what is waiting for THIS person, what they wait for, and what is blocked, placed by the server from the one task projection that the task
// frame and the request assistant also read (both request families). Opening an item goes straight to the request; nobody has to copy or paste a link to hand work over.
// It only shows where to go: what each person may do is still decided by the server on every action. Counts are request entries in the list that was loaded (the
// server reads a bounded set of candidates and says when it cut one: `coverage`); a part that could not be read is said, never shown as 0 or as "no work".
import * as React from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, EmptyState } from "@/components/bits";
import { DataTable, type Column } from "@/components/ui/data-table";
import { hashFor } from "@/lib/route";
import { cn } from "@/lib/cn";
import { useSession } from "@/session";
import { FAMILY_LABEL, rowTitle } from "./requestTitle";

export type Section = "mine" | "optional" | "blocked" | "waiting_human" | "waiting_system" | "recent";
export interface WorkItem {
  proposal_job_id: string; title: string; requested_by?: string | null; stage: string; label: string; page?: string; family?: string; step?: string;
  params: Record<string, string>; mine: boolean; you_do?: string; why?: string; optional?: boolean; waiting_on?: string | null; section?: Section;
  open_change_requests?: string[]; blocked?: boolean; blockers?: string[];
}
export interface Coverage { scope?: string; candidate_limits?: Record<string, number>; truncated?: string[]; recent_loaded?: number; recent_shown?: number; complete?: boolean }
export interface Inbox { todo: WorkItem[]; waiting: WorkItem[]; recent: WorkItem[]; sections?: Record<Section, WorkItem[]>; unavailable?: number; coverage?: Coverage }

const HEAD: Record<Section, string> = {
  mine: "Waiting for you", optional: "Optional for you", blocked: "Blocked", waiting_human: "You are waiting for someone",
  waiting_system: "You are waiting for the system", recent: "Recently finished",
};
const MODE: Record<Section, "todo" | "waiting" | "recent"> = { mine: "todo", optional: "todo", blocked: "waiting", waiting_human: "waiting", waiting_system: "waiting", recent: "recent" };
const ORDER: Section[] = ["mine", "optional", "blocked", "waiting_human", "waiting_system", "recent"];
const FAMILY = FAMILY_LABEL;
// The three Overview counts (plan 17 SCN-03): request entries per server group, never summed into one total, never mixed with optional or system waits.
const TILES: { id: Section; label: string }[] = [{ id: "mine", label: "Waiting for you" }, { id: "waiting_human", label: "Waiting for someone else" }, { id: "blocked", label: "Blocked" }];

export function useInbox(ws: string): { inbox: Inbox | null; failed: boolean } {
  const [inbox, setInbox] = React.useState<Inbox | null>(null);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => {
    setInbox(null); setFailed(false);                                  // a new workspace never shows the previous workspace's rows while it loads
    if (!ws) return;
    let live = true;
    const load = () => api<Inbox>("GET", `/v2/work/inbox?workspace_id=${encodeURIComponent(ws)}`)
      .then((x) => { if (live) { setInbox(x); setFailed(false); } }).catch(() => { if (live) setFailed(true); });
    void load();
    // No timer: an idle page makes no request. The list is read again when the person comes back to the tab.
    const again = () => { if (document.visibilityState === "visible") void load(); };
    document.addEventListener("visibilitychange", again);
    window.addEventListener("focus", again);
    return () => { live = false; document.removeEventListener("visibilitychange", again); window.removeEventListener("focus", again); };
  }, [ws]);
  return { inbox, failed };
}

/** The server groups, or the legacy lists of an older server. */
export function sectionsOf(inbox: Inbox): Record<Section, WorkItem[]> {
  return inbox.sections ?? {
    mine: inbox.todo.filter((x) => !x.optional), optional: inbox.todo.filter((x) => x.optional), blocked: [], waiting_human: inbox.waiting, waiting_system: [], recent: inbox.recent,
  };
}

function hrefOf(ws: string, it: WorkItem, section: Section): string {
  const params = it.step ? { ...it.params, task: "map", step: it.step } : it.params;          // a waiting item lands on its step of the whole-request map
  return hashFor(ws, it.page ?? "changes", undefined, section === "mine" || section === "optional" ? it.params : params);
}

function columns(ws: string, section: Section, technical: boolean): Column<WorkItem>[] {
  const mode = MODE[section];
  return [
    { id: "request", header: "Request", essential: true, sortValue: (it) => it.title, cell: (it) => {
      const t = rowTitle(it);
      return (
        <span className="grid gap-0.5" data-testid="work-title" data-fallback={String(t.fallback)}>
          <span className="font-medium text-base break-words">{t.text}</span>
          <span className="text-xs text-muted break-words">
            {t.fallback
              ? <>{/* the id is not a title: short id visible, full id for assistive technology and in the Technical view */}
                  ID <code data-testid="work-title-id" aria-hidden={technical ? undefined : "true"} title={t.id}>{technical ? t.id : t.short}</code>{!technical && <span className="sr-only">{t.id}</span>}</>
              : FAMILY[it.family ?? ""] ?? "Request"}
            {it.requested_by ? ` · requested by ${it.requested_by}` : ""}
          </span>
        </span>);
    } },
    { id: "step", header: mode === "todo" ? "Your part" : "Step now", essential: true, cell: (it) => (
      <span className="grid gap-1">
        {mode === "todo" ? <span><strong>{it.why}</strong>{it.optional && !/\(optional\)\s*$/i.test(String(it.why ?? "")) ? <span className="text-muted"> (optional)</span> : null}</span> : <span>{it.label}</span>}
        {it.blocked && (it.blockers ?? []).length > 0 && <span className="text-warning" data-testid="work-blockers">Blocked: {it.blockers!.join(" ")}</span>}
        {(it.open_change_requests ?? []).length > 0 && <span className="text-warning">A reviewer asked for changes ({it.open_change_requests!.join(", ")}).</span>}
      </span>) },
    { id: "holder", header: "Held by", essential: true, cell: (it) => (
      mode === "todo" ? <span>{it.optional ? "Optional for you" : `You (${it.you_do})`}</span>
        : section === "blocked" ? <span>Nobody can move it on its current path</span>
        : mode === "waiting" ? <span>Waiting for <strong>{it.waiting_on}</strong></span>
        : <span className="text-muted">Finished</span>) },
    { id: "open", header: "Open", essential: true, align: "end", cell: (it) => {
      const t = rowTitle(it);
      return (
        <Button asChild size="sm" variant={section === "mine" ? "primary" : "secondary"}>
          <a data-testid={`work-open-${mode}`} href={hrefOf(ws, it, section)} aria-label={`Open: ${t.fallback ? `${t.text} ${t.id}` : t.text}`}>Open<ArrowRight size={14} aria-hidden="true" /></a>
        </Button>);
    } },
  ];
}

export function WorkInbox({ ws, state, counts = false }: { ws: string; state?: { inbox: Inbox | null; failed: boolean }; counts?: boolean }) {
  const s = useSession();
  const own = useInbox(state ? "" : ws);
  const { inbox, failed } = state ?? own;
  const [filter, setFilter] = React.useState<Section | null>(null);
  React.useEffect(() => setFilter(null), [ws]);
  if (failed && !inbox)
    return (
      <Card title="My work" testid="work-inbox" eyebrow="Requests placed for you">
        {counts && <Tiles secs={null} filter={null} onFilter={() => undefined} />}
        <Callout tone="warn" testid="work-failed">Your work list could not be read, so no count is shown. The other pages still work.</Callout>
      </Card>
    );
  if (!inbox) return null;
  const secs = sectionsOf(inbox);
  const cov = inbox.coverage;
  const unavailable = inbox.unavailable ?? 0;
  const empty = ORDER.every((k) => k === "recent" || secs[k].length === 0);
  const shownOrder = filter ? [filter] : ORDER;
  return (
    <Card title="My work" testid="work-inbox" eyebrow="Requests placed for you" flush data={{ account: s.account, todo: String(secs.mine.length + secs.optional.length) }}
      description="Each request is placed by the server from the same task record as the request page; what you may do is checked again on every action.">
      {/* SCN-15 sub-header: counts, scope and notices in one tinted strip under the panel header; the tables below run edge to edge. */}
      <div className="grid gap-3 border-b border-border bg-surface-2/40 px-4 py-3 empty:hidden" data-testid="work-subheader">
        {counts && <Tiles secs={secs} filter={filter} onFilter={setFilter} />}
        {unavailable > 0 && <Callout tone="warn" testid="work-partial"><span>{unavailable} request(s) could not be read just now, so this list and its counts may be incomplete.</span></Callout>}
        {cov && cov.complete === false && (cov.truncated ?? []).length > 0 && (
          <Callout tone="warn" testid="work-cut"><span>This list reads the newest requests up to a limit, and more exist than it read. Counts are of the loaded list, not of the whole workspace.</span></Callout>
        )}
        {filter && (
          <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="work-filter">
            <span>Showing only: <strong>{HEAD[filter]}</strong> ({secs[filter].length})</span>
            <Button size="sm" variant="ghost" data-testid="work-filter-clear" onClick={() => setFilter(null)}>Show all groups</Button>
          </div>
        )}
        {empty && !unavailable && !filter && (
          <EmptyState kind="none" compact testid="work-empty" title="Nothing is waiting for you in the loaded list"
            action={<Button asChild size="sm" variant="secondary"><a href={hashFor(ws, "changes")} data-testid="work-empty-open">See the requests you may read</a></Button>}>
            <span>You are not waiting for anyone either. A request that needs you appears here. This says nothing about the rest of the workspace.</span>
          </EmptyState>
        )}
      </div>
      <div className="grid">
        {shownOrder.filter((k) => secs[k].length > 0 || (filter === k)).map((k) => (
          <section key={k} className="grid min-w-0 border-b border-border last:border-b-0" data-testid={`work-section-${k}`} aria-labelledby={`work-h-${k}`}>
            <h3 id={`work-h-${k}`} className="bg-surface-2/60 px-4 py-2 font-semibold">{HEAD[k]} ({k === "recent" && cov?.recent_loaded !== undefined && cov.recent_loaded > secs[k].length ? `${secs[k].length} of ${cov.recent_loaded}` : secs[k].length})</h3>
            <DataTable<WorkItem> flush testid={`work-table-${k}`} caption={HEAD[k]} captionHidden rows={secs[k]} rowKey={(it) => `${it.family ?? ""}${it.proposal_job_id}-${k}`}
              columns={columns(ws, k, s.mode === "technical")} empty={<EmptyState kind="no-match" compact testid="work-filter-none" title="No request in this group in the loaded list"
                action={<Button size="sm" variant="ghost" onClick={() => setFilter(null)}>Show all groups</Button>} />}
              rowProps={(it) => ({ "data-testid": `work-${MODE[k]}`, "data-section": k, "data-stage": it.stage, "data-proposal": it.proposal_job_id, "data-family": it.family })} />
          </section>
        ))}
      </div>
    </Card>
  );
}

/** Three counts of the loaded list; each one filters the list below to its group (no request, no mutation). `secs` null = the list could not be read: Unavailable. */
function Tiles({ secs, filter, onFilter }: { secs: Record<Section, WorkItem[]> | null; filter: Section | null; onFilter: (s: Section | null) => void }) {
  return (
    <div className="grid gap-1" data-testid="overview-counts">
    <div className="grid gap-2 grid-cols-3 sm:gap-3" role="group" aria-label="Request counts in the loaded list">
      {TILES.map((t) => {
        const n = secs ? secs[t.id].length : null;
        const on = filter === t.id;
        return (
          // SCN-19: one compact row per count from 640 px (label left, tabular number right), label above the number on a phone; at least 44 px to touch;
          // the pressed filter adds an inset primary ring (aria-pressed says the same).
          <button key={t.id} type="button" data-testid={`count-${t.id}`} data-count={n === null ? "unavailable" : String(n)} aria-pressed={on} disabled={n === null}
            onClick={() => onFilter(on ? null : t.id)}
            className={cn("min-h-11 rounded-md border border-border bg-surface px-3 py-1.5 text-left hover:bg-surface-2 disabled:cursor-default disabled:hover:bg-surface",
              "grid content-center gap-0.5 sm:flex sm:items-baseline sm:justify-between sm:gap-2",
              on && "border-primary ring-1 ring-inset ring-primary", t.id === "mine" && n ? "border-l-4 border-l-primary" : t.id === "blocked" && n ? "border-l-4 border-l-warning" : undefined)}>
            <span className="min-w-0 text-xs font-semibold uppercase tracking-wider text-muted">{t.label}</span>
            <span className={n === null ? "text-base font-semibold text-muted" : "text-xl font-semibold tnum text-text"}>{n === null ? "Unavailable" : n}</span>
          </button>
        );
      })}
    </div>
    <p className="text-xs text-muted" data-testid="overview-counts-scope">{secs ? "Request entries in the loaded list (not workspace totals). Select a count to show only that group." : "The list could not be read, so no count is shown."}</p>
    </div>
  );
}
