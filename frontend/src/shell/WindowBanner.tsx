// The trial window as every signed-in role sees it: when it ends, how long is left, and what the two operator switches are. The numbers come from the SERVER (the end time is recorded in the
// database; the web refuses new AI actions after it and the worker stops claiming at it), never from this browser's clock alone: the remaining time is the server's figure minus the seconds
// since it was read. Nothing is shown when no window is recorded.
import * as React from "react";
import { api } from "@/lib/api";
import { useSession } from "@/session";

interface Flags { active?: boolean; ai_switch?: string; worker_claims?: string; window?: { state: "none" | "open" | "ended"; ends_at?: string; seconds_left?: number } }

export function WindowBanner() {
  const s = useSession();
  const ws = s.route.ws;
  const [st, setSt] = React.useState<Flags | null>(null);
  const [readAt, setReadAt] = React.useState(0);
  const [, tick] = React.useState(0);
  const load = React.useCallback(async () => {
    if (!ws) return;
    try { setSt(await api<Flags>("GET", `/v2/operator/flags?workspace_id=${encodeURIComponent(ws)}`)); setReadAt(Date.now()); } catch { setSt(null); }
  }, [ws]);
  React.useEffect(() => { void load(); const t = window.setInterval(() => void load(), 30000); return () => window.clearInterval(t); }, [load]);
  React.useEffect(() => { const t = window.setInterval(() => tick((n) => n + 1), 1000); return () => window.clearInterval(t); }, []);
  const w = st?.window;
  if (!st || !w || w.state === "none") return null;
  const left = w.state === "open" ? Math.max(0, (w.seconds_left ?? 0) - Math.floor((Date.now() - readAt) / 1000)) : 0;
  const ended = w.state === "ended" || left === 0;
  const mm = Math.floor(left / 60), ss = String(left % 60).padStart(2, "0");
  const long = left >= 6 * 3600;           // an evaluation period of days: say days and hours, and the date
  const dd = Math.floor(left / 86400), hh = Math.floor((left % 86400) / 3600), mn = Math.floor((left % 3600) / 60);
  const ends = w.ends_at ? (long ? new Date(w.ends_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : new Date(w.ends_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) : "";
  const leftText = long ? `${dd > 0 ? `${dd}d ` : ""}${hh}h ${mn}m` : `${mm}:${ss}`;
  const sw = (v?: string) => (v === "on" ? "ON" : v === "off" ? "OFF" : "unknown");
  return (
    <div role="status" data-testid="window-banner" data-state={ended ? "ended" : "open"} className="bg-surface-2 border-b border-border text-sm px-4 py-1 flex flex-wrap items-center gap-x-4 gap-y-1">
      {ended
        ? <><strong>The evaluation window has ended.</strong><span>New work is refused with an explanation. You can still read everything saved and switch roles; nothing is lost.</span></>
        : <><strong>{long ? "Evaluation period:" : "Trial window:"}</strong><span data-testid="window-left">{leftText} left (ends {ends})</span></>}
      <span data-testid="window-switches" className="text-muted">New AI starts: {sw(st.ai_switch)} · Job processing: {sw(st.worker_claims)}</span>
    </div>
  );
}
