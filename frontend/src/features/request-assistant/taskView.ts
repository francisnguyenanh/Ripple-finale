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
