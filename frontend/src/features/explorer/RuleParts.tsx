// The things a person can do with a rule or an issue, and the history of a rule. Shared by the tree and the graph detail so both
// show the same facts and offer the same, server-checked actions. Confirming records a decision; it never edits a source file.
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Field, Muted, Tech, TextInput } from "@/components/bits";
import { basename, humanize } from "@/lib/fmt";
import { plain } from "@/lib/reasons";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";
import { bizLabel, describeState, EVENT_BIZ, whenWho } from "./explorerText";
import type { GNode } from "./layout";

const q = encodeURIComponent;

export function useTimeline(ws: string, nodeId: string | undefined, enabled: boolean, dep: unknown): Rec | null | "err" {
  const [tl, setTl] = React.useState<Rec | null | "err">(null);
  React.useEffect(() => {
    setTl(null);
    if (!nodeId || !enabled) return;
    let live = true;
    api<Rec>("GET", `/v2/knowledge/timeline?workspace_id=${q(ws)}&node_id=${q(nodeId)}`).then((t) => { if (live) setTl(t); }).catch(() => { if (live) setTl("err"); });
    return () => { live = false; };
  }, [nodeId, enabled, ws, dep]);
  return tl;
}

export function Timeline({ tl }: { tl: Rec | null | "err" }) {
  const s = useSession();
  return (
    <div data-testid="node-timeline" className="grid gap-2">
      {tl === null && <Muted>Loading history...</Muted>}
      {tl === "err" && <Muted>History could not be read; try again.</Muted>}
      {tl && tl !== "err" && (
        <>
          {(tl.lineage_candidate_ids ?? []).length > 1 && <Muted>The same rule, followed across {(tl.lineage_candidate_ids as string[]).length} readings of the file (same file, name and position).</Muted>}
          {((tl.lineage_notes ?? []) as string[]).map((x, i) => <Muted key={i}>Note: {s.mode === "technical" ? x : plain(x, false)}</Muted>)}
          {!((tl.events ?? []) as Rec[]).length && <Muted>No history has been recorded for this rule yet.</Muted>}
          <ol className="grid gap-2 border-l border-border pl-4">
            {((tl.events ?? []) as Rec[]).map((e, i) => (
              <li key={i} data-category={e.category} data-kind={e.kind} className="text-sm">
                <span className="rounded-full border border-border px-2 text-xs mr-1">{({ analysis: "AI reading", knowledge: "person decision", source: "file changed" } as Rec)[e.category] ?? e.category}</span>
                <strong>{e.kind === "value_changed_in_source" ? (e.change_kind === "scope_only" ? "Read again with a different scope (value unchanged)" : e.source_edited_by_apply ? "Value changed by an applied change" : "Value read differently than in the earlier reading") : e.kind === "source_changed" && e.edited_by_apply ? "The source file was edited by an applied change" : e.kind === "extracted" && e.read_again_after_apply ? "Read again because of an applied change" : (EVENT_BIZ[e.kind] ?? humanize(e.kind))}</strong>
                <Tech><span className="text-muted"> {e.kind}{e.edited_by_apply ? ` - apply ${e.edited_by_apply}` : ""}{e.source_edited_by_apply ? ` - apply ${e.source_edited_by_apply}` : ""}{e.read_again_after_apply ? ` - caused by apply ${e.read_again_after_apply}` : ""}</span></Tech>
                <span className="block text-xs text-muted">{whenWho(e, s.mode === "technical")}</span>
                {e.why && <span className="block">Why: {plain(String(e.why).replace(/ \(None\)/, ""), s.mode === "technical")}</span>}
                {e.before && <span className="block">Before: {s.mode === "technical" ? JSON.stringify(e.before) : describeState(e.before)}</span>}
                {e.after && <span className="block">After: {s.mode === "technical" ? JSON.stringify(e.after) : describeState(e.after)}</span>}
                {e.note && <span className="block text-muted">{e.note}</span>}
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

export function ConfirmPanel({ id, reload }: { id: string; reload: () => Promise<void> }) {
  const s = useSession();
  const ws = s.route.ws!;
  const notify = useNotify();
  const { run, pending } = useAction();
  const [why, setWhy] = React.useState("");
  return (
    <div data-testid="confirm-panel" className="grid gap-2">
      <Field label="Why (recorded with your name)"><TextInput data-testid="confirm-rationale" value={why} onChange={(e) => setWhy(e.target.value)} /></Field>
      <div><Button data-testid="confirm-rule" disabled={pending} onClick={async () => {
        const r = await run(() => api("POST", "/v2/knowledge/confirm", { workspace_id: ws, claim_id: id, ...(why.trim() ? { rationale: why.trim() } : {}) }), { stale: true });
        if (r) { setWhy(""); notify.status("Decision recorded; the source file is unchanged."); await reload(); }
      }}>Confirm this rule as knowledge</Button></div>
    </div>
  );
}

export function ResolvePanel({ id, cands, reload }: { id: string; cands: GNode[]; reload: () => Promise<void> }) {
  const s = useSession();
  const ws = s.route.ws!;
  const notify = useNotify();
  const { run, pending } = useAction();
  const [picked, setPicked] = React.useState("");
  return (
    <div data-testid="resolve-panel" className="grid gap-2">
      <p className="text-sm">Which source is authoritative? (Records a decision; does not edit either file.)</p>
      {cands.map((c) => (
        <label key={c.id} className="flex items-start gap-2 text-sm"><input type="radio" name={`auth-${id}`} checked={picked === c.id} onChange={() => setPicked(c.id)} className="mt-1" />{bizLabel(c)} ({basename((c.detail as Rec)?.logical_path)})</label>
      ))}
      <div><Button data-testid="resolve-conflict" disabled={!picked || pending} onClick={async () => {
        const r = await run(() => api("POST", "/v2/conflict/resolve", { workspace_id: ws, conflict_id: id, authoritative_claim_id: picked }), { stale: true });
        if (r) { notify.status("Authority recorded as your decision; no source file was edited."); await reload(); }
      }}>Record this source as authoritative</Button></div>
    </div>
  );
}
