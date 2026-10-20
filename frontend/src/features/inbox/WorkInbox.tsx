// "My work" (RUX-04): what is waiting for THIS person, what they wait for, and what is blocked, placed by the server from the one task projection that the task frame
// and the request assistant also read (both request families). Opening an item goes straight to the request; nobody has to copy or paste a link to hand work over. It only
// shows where to go: what each person may do is still decided by the server on every action. Counts refer to what was loaded; a part that could not be read is said.
import * as React from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, Muted } from "@/components/bits";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";

export type Section = "mine" | "optional" | "blocked" | "waiting_human" | "waiting_system" | "recent";
export interface WorkItem {
  proposal_job_id: string; title: string; requested_by?: string | null; stage: string; label: string; page?: string; family?: string; step?: string;
  params: Record<string, string>; mine: boolean; you_do?: string; why?: string; optional?: boolean; waiting_on?: string | null; section?: Section;
  open_change_requests?: string[]; blocked?: boolean; blockers?: string[];
}
export interface Inbox { todo: WorkItem[]; waiting: WorkItem[]; recent: WorkItem[]; sections?: Record<Section, WorkItem[]>; unavailable?: number }

const HEAD: Record<Section, string> = {
  mine: "Waiting for you", optional: "Optional for you", blocked: "Blocked", waiting_human: "You are waiting for someone",
  waiting_system: "You are waiting for the system", recent: "Recently finished",
};
const MODE: Record<Section, "todo" | "waiting" | "recent"> = { mine: "todo", optional: "todo", blocked: "waiting", waiting_human: "waiting", waiting_system: "waiting", recent: "recent" };

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

function Row({ ws, it, section }: { ws: string; it: WorkItem; section: Section }) {
  const mode = MODE[section];
  const params = it.step ? { ...it.params, task: "map", step: it.step } : it.params;          // lands on the step of the whole-request map
  const href = hashFor(ws, it.page ?? "changes", undefined, section === "mine" || section === "optional" ? it.params : params);
  return (
    <li data-testid={`work-${mode}`} data-section={section} data-stage={it.stage} data-proposal={it.proposal_job_id} data-family={it.family}
      className="rounded-md border border-border px-3 py-2 grid gap-1 text-sm">
      <span className="font-medium break-words">{it.title}</span>
      <span className="text-muted">
        {mode === "todo" ? <>{it.why}. <strong>{it.optional ? "Optional for you" : `For you (${it.you_do})`}</strong></>
          : section === "blocked" ? <>{it.label}. Nobody can move it on its current path.</>
          : mode === "waiting" ? <>{it.label}. Waiting for <strong>{it.waiting_on}</strong></> : it.label}
        {it.requested_by ? ` · requested by ${it.requested_by}` : ""}
      </span>
      {it.blocked && (it.blockers ?? []).length > 0 && <span className="text-warning" data-testid="work-blockers">Blocked: {it.blockers!.join(" ")}</span>}
      {(it.open_change_requests ?? []).length > 0 && <span className="text-warning">A reviewer asked for changes ({it.open_change_requests!.join(", ")}).</span>}
      <div>
        <Button asChild variant={section === "mine" ? "primary" : "secondary"}>
          <a data-testid={`work-open-${mode}`} href={href}>Open<ArrowRight size={14} aria-hidden="true" /></a>
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
  const secs: Record<Section, WorkItem[]> = inbox.sections ?? {                       // an older server: the legacy lists
    mine: inbox.todo.filter((x) => !x.optional), optional: inbox.todo.filter((x) => x.optional), blocked: [], waiting_human: inbox.waiting, waiting_system: [], recent: inbox.recent,
  };
  const order: Section[] = ["mine", "optional", "blocked", "waiting_human", "waiting_system", "recent"];
  const empty = order.every((k) => k === "recent" || secs[k].length === 0);
  return (
    <Card title="My work" testid="work-inbox" data={{ account: s.account, todo: String(secs.mine.length + secs.optional.length) }}>
      {(inbox.unavailable ?? 0) > 0 && <Muted testid="work-partial">{inbox.unavailable} request(s) could not be read just now, so this list may be incomplete.</Muted>}
      {empty && !(inbox.unavailable ?? 0) && <Muted testid="work-empty">Nothing is waiting for you, and you are not waiting for anyone. Changes in progress by other people appear here when they need you.</Muted>}
      {order.filter((k) => secs[k].length > 0).map((k) => (
        <div key={k} className="grid gap-2" data-testid={`work-section-${k}`}>
          <h3 className="font-semibold">{HEAD[k]} ({secs[k].length})</h3>
          <ul className="grid gap-2">{secs[k].map((it) => <Row key={`${it.family ?? ""}${it.proposal_job_id}-${k}`} ws={ws} it={it} section={k} />)}</ul>
        </div>
      ))}
    </Card>
  );
}
