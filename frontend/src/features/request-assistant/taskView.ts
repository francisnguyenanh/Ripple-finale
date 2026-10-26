// The server's task projection of one request (`GET /v2/requests/{family}/{id}/task-view`, contract ripple-request-task/1), read once per page and shared, so the task frame,
// the journey and the next-step text all say the same thing about who holds the step. Read only; reloaded when the owning page's data changes, never polled here.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";

export type Rec = Record<string, unknown>;
export interface TaskViewState { tv: Rec | null; err: string | null }

export function useTaskView(ws: string, family: "intent_flow" | "business_change", id: string | null, refreshKey?: unknown): TaskViewState {
  const [state, setState] = React.useState<TaskViewState>({ tv: null, err: null });
  React.useEffect(() => {
    if (!id) { setState({ tv: null, err: null }); return; }
    let live = true;
    api<Rec>("GET", `/v2/requests/${family}/${encodeURIComponent(id)}/task-view?workspace_id=${encodeURIComponent(ws)}`)
      .then((r) => { if (live) setState({ tv: r, err: null }); })
      .catch((e) => { if (live) setState({ tv: null, err: e instanceof ApiError && e.status === 404 ? null : errorText(e) }); });
    return () => { live = false; };
  }, [ws, family, id, refreshKey]);
  return state;
}

export const TaskViewContext = React.createContext<TaskViewState | null>(null);

/** Who holds the current step for THIS reader, from the projection only: `mine` is a current required obligation the reader is asked to do (never a permission alone). */
export interface Responsibility { mine: boolean; holder: string | null; step: string | null; eligible: boolean; reason: string | null }

export function responsibility(tv: Rec | null | undefined): Responsibility | null {
  if (!tv) return null;
  const obs = (tv.obligations ?? []) as Rec[];
  const live = obs.filter((o) => o.required && (o.state === "current" || o.state === "blocked"));
  const mine = live.some((o) => o.obligation_me && o.state === "current");
  const first = live.find((o) => !o.obligation_me) ?? live[0] ?? null;
  const waits = (tv.waiting_on ?? []) as Rec[];
  const holder = mine ? null : (first ? String(first.holder_label ?? "") : waits.length ? String(waits[0].holder_label ?? "") : "") || null;
  return { mine, holder, step: first ? String(first.label ?? "") || null : null, eligible: !mine && !!first?.eligible_me, reason: first?.reason ? String(first.reason) : null };
}

/** SCN-02 header facts of one request for THIS reader, read from the projection only (never from a role name):
 *  - `held`: who holds the open step(s): "You" only where an obligation is the reader's to do now (`obligation_me`), otherwise the holders the record waits on;
 *    a blocked request that nobody is waited on says so; a request with no open step says so; anything else is null ("Not recorded").
 *  - `part`: the reader's own part: required steps asked now (count when several; each is listed by the frame's "What to do"), an optional contribution,
 *    "may act but not asked" (eligible, not obligated), or nothing. `label` is not repeated here. */
export interface TaskSummary { held: string | null; heldByYou: boolean; part: "asked" | "optional" | "eligible" | "none"; partText: string; askedCount: number }

export function taskSummary(tv: Rec): TaskSummary {
  const obs = (tv.obligations ?? []) as Rec[];
  const now = obs.filter((o) => o.state === "current" || o.state === "blocked");
  const asked = now.filter((o) => o.obligation_me && o.required && o.state === "current");
  const optional = now.filter((o) => o.obligation_me && !o.required && o.state === "current");
  const eligible = now.filter((o) => !o.obligation_me && o.eligible_me && o.state === "current");
  const waits = [...new Set(((tv.waiting_on ?? []) as Rec[]).map((w) => String(w.holder_label ?? "")).filter(Boolean))];
  const blocked = ((tv.blockers ?? []) as Rec[]).length > 0;
  const heldByYou = asked.length > 0;
  const held = heldByYou ? (waits.length ? `You, and ${waits.join(", ")}` : "You")
    : waits.length ? waits.join(", ")
    : blocked && !now.some((o) => o.state === "current") ? "Nobody can move it on its current path"
    : now.length === 0 ? "No step is open" : null;
  // the step itself is named once, in the frame's "What to do"; the header only says where the reader stands (plan 17: one full statement per reason)
  if (asked.length) return { held, heldByYou, part: "asked", askedCount: asked.length, partText: asked.length === 1 ? "Your step now" : `${asked.length} steps for you now` };
  if (optional.length) return { held, heldByYou, part: "optional", askedCount: 0, partText: "An optional step for you" };
  if (eligible.length) return { held, heldByYou, part: "eligible", askedCount: 0, partText: "You may act, but you are not asked to" };
  return { held, heldByYou, part: "none", askedCount: 0, partText: "Nothing for you now" };
}
