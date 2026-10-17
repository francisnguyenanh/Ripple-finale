// Runtime switches for the operator (an administrator): stop new AI starts, or stop the worker claiming any job, without a redeploy. Everyone signed in can read the state and why.
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Muted, TextArea } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";

const WORDS: Record<string, string> = { ai_switch: "New AI starts", worker_claims: "Job processing (the worker claims jobs)" };

export function OperatorSwitches() {
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const ws = s.route.ws;
  const [st, setSt] = React.useState<Rec | null>(null);
  const [reason, setReason] = React.useState("");
  const load = React.useCallback(async () => { if (ws) { try { setSt(await api<Rec>("GET", `/v2/operator/flags?workspace_id=${encodeURIComponent(ws)}`)); } catch { setSt(null); } } }, [ws]);
  React.useEffect(() => { void load(); }, [load]);
  if (!st) return null;
  const flip = async (key: string, value: string) => {
    if (await run(() => api("POST", "/v2/operator/flags", { workspace_id: ws, key, value, reason }))) { notify.status("The switch was changed and recorded."); setReason(""); await load(); }
  };
  return (
    <Card title="AI and processing switches" testid="operator-switches">
      {!st.active && <Muted testid="switches-inactive">The runtime switches are not active on this deployment.</Muted>}
      {st.active && (
        <div className="grid gap-3 text-sm">
          {["ai_switch", "worker_claims"].map((k) => (
            <div key={k} data-testid={`switch-${k}`} data-value={String(st[k])} className="flex flex-wrap items-center gap-2">
              <strong>{WORDS[k]}:</strong> <span>{st[k] === "on" ? "ON" : st[k] === "off" ? "OFF (paused by the operator)" : String(st[k])}</span>
              {st.can_change && <Button disabled={pending || reason.trim().length < 10} data-testid={`flip-${k}`} onClick={() => void flip(k, st[k] === "on" ? "off" : "on")}>Switch {st[k] === "on" ? "OFF" : "ON"}</Button>}
            </div>
          ))}
          {st.can_change
            ? <Field label="Reason (at least 10 characters, recorded with your name)"><TextArea data-testid="switch-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
            : <Muted>Only an administrator can change a switch.</Muted>}
          {(st.ai_switch === "off" || st.worker_claims === "off") && <Callout tone="warn" testid="switches-paused">Something is paused by the operator. Saved results can be read. Queued work waits and is not lost; running work finishes.</Callout>}
          <Muted>Switching off stops NEW work within a few seconds. It does not cancel an attempt already running.</Muted>
          <ul className="list-disc pl-5" data-testid="switch-history">{((st.history ?? []) as Rec[]).map((h, i) => <li key={i}>{h.set_at}: {WORDS[h.key] ?? h.key} {h.value} by {h.set_by} - {h.reason}</li>)}</ul>
        </div>
      )}
    </Card>
  );
}
