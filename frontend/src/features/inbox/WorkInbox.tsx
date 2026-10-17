// "My work": what is waiting for THIS person, and what they are waiting for, derived by the server from the stored jobs,
// decisions, checks and reviewer notes. Opening an item goes straight to the right place of the Changes page; nobody has to
// copy or paste a link to hand work over. It only shows where to go: what each person may do is still decided by the server.
import * as React from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, Muted } from "@/components/bits";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";

export interface WorkItem {
  proposal_job_id: string; title: string; requested_by?: string | null; stage: string; label: string;
  params: Record<string, string>; mine: boolean; you_do?: string; why?: string; optional?: boolean; waiting_on?: string | null;
  open_change_requests?: string[];
}
export interface Inbox { todo: WorkItem[]; waiting: WorkItem[]; recent: WorkItem[] }

export function useInbox(ws: string): { inbox: Inbox | null; failed: boolean } {
  const [inbox, setInbox] = React.useState<Inbox | null>(null);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => {
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

function Row({ ws, it, mode }: { ws: string; it: WorkItem; mode: "todo" | "waiting" | "recent" }) {
  return (
    <li data-testid={`work-${mode}`} data-stage={it.stage} data-proposal={it.proposal_job_id} className="rounded-md border border-border px-3 py-2 grid gap-1 text-sm">
      <span className="font-medium break-words">{it.title}</span>
      <span className="text-muted">
        {mode === "todo" ? <>{it.why}. <strong>{it.optional ? "Optional for you" : `For you (${it.you_do})`}</strong></>
          : mode === "waiting" ? <>{it.label}. Waiting for <strong>{it.waiting_on}</strong></> : it.label}
        {it.requested_by ? ` · requested by ${it.requested_by}` : ""}
      </span>
      {(it.open_change_requests ?? []).length > 0 && <span className="text-warning">A reviewer asked for changes ({it.open_change_requests!.join(", ")}).</span>}
      <div>
        <Button asChild variant={mode === "todo" && !it.optional ? "primary" : "secondary"}>
          <a data-testid={`work-open-${mode}`} href={hashFor(ws, "changes", undefined, it.params)}>Open<ArrowRight size={14} aria-hidden="true" /></a>
        </Button>
      </div>
    </li>
  );
}

export function WorkInbox({ ws, state }: { ws: string; state?: { inbox: Inbox | null; failed: boolean } }) {
  const s = useSession();
  const own = useInbox(state ? "" : ws);
  const { inbox, failed } = state ?? own;
  if (failed && !inbox) return <Card title="My work" testid="work-inbox"><Muted testid="work-failed">Your work list could not be read. The other pages still work.</Muted></Card>;
  if (!inbox) return null;
  const empty = inbox.todo.length === 0 && inbox.waiting.length === 0;
  return (
    <Card title="My work" testid="work-inbox" data={{ account: s.account, todo: String(inbox.todo.length) }}>
      {empty && <Muted testid="work-empty">Nothing is waiting for you, and you are not waiting for anyone. Changes in progress by other people appear here when they need you.</Muted>}
      {inbox.todo.length > 0 && (
        <div className="grid gap-2">
          <h3 className="font-semibold">Waiting for you ({inbox.todo.length})</h3>
          <ul className="grid gap-2">{inbox.todo.map((it) => <Row key={it.proposal_job_id + (it.optional ? "-r" : "")} ws={ws} it={it} mode="todo" />)}</ul>
        </div>
      )}
      {inbox.waiting.length > 0 && (
        <div className="grid gap-2">
          <h3 className="font-semibold">You are waiting for someone ({inbox.waiting.length})</h3>
          <ul className="grid gap-2">{inbox.waiting.map((it) => <Row key={it.proposal_job_id} ws={ws} it={it} mode="waiting" />)}</ul>
        </div>
      )}
      {inbox.recent.length > 0 && (
        <div className="grid gap-2">
          <h3 className="font-semibold">Recently applied</h3>
          <ul className="grid gap-2">{inbox.recent.map((it) => <Row key={it.proposal_job_id} ws={ws} it={it} mode="recent" />)}</ul>
        </div>
      )}
    </Card>
  );
}
