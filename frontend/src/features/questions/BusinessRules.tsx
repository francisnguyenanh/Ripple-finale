// The business rules of the current knowledge, in business words; the Technical view adds ids, provenance references and relations. Read only.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Callout, Muted, StatePill, Tech } from "@/components/bits";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const IMPL: Record<string, string> = {
  not_required: "Describes what the sources say", pending: "Not yet implemented: the files and code still show the old rule", partial: "Partly implemented",
  verified: "Matches what the sources show", divergent: "Differs from what the sources show", unverifiable: "Cannot be compared",
};
const STATUS: Record<string, string> = { CONFIRMED: "Confirmed by a person", VALIDATED: "Read from the sources", DRAFT: "Draft", PENDING_HUMAN: "Waiting for a person", SUPERSEDED: "Withdrawn", REJECTED: "Rejected" };

function who(ref: unknown): string {
  return typeof ref === "string" && ref ? ref.replace(/^role-/, "").replace(/-/g, " ") : "Not stated in the sources";
}

export function BusinessRules({ ws }: { ws: string }) {
  const [graph, setGraph] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState(false);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/graph?workspace_id=${q(ws)}`).then((r) => { if (live) { setGraph(r); setErr(false); } })
      .catch((e) => { if (live && e instanceof ApiError) setErr(true); });
    return () => { live = false; };
  }, [ws]);
  if (err) return <Callout tone="danger" testid="rules-error">The business rules could not be loaded.</Callout>;
  if (!graph) return null;
  const rules = ((graph.nodes ?? []) as Rec[]).filter((n) => n.type === "rule");
  if (!graph.snapshot_id || rules.length === 0) return <Muted testid="rules-none">No business rules have been read from the sources yet.</Muted>;
  const groups: [string, Rec[]][] = [["Decided by people", rules.filter((r) => r.authority === "TARGET")], ["What the sources say", rules.filter((r) => r.authority === "OBSERVED")]];
  return (
    <div className="grid gap-4" data-testid="business-rules" data-count={rules.length}>
      {groups.filter(([, list]) => list.length).map(([title, list]) => (
        <section key={title} aria-label={title} className="grid gap-2">
          <h3 className="text-base font-semibold">{title} ({list.length})</h3>
          {list.map((r) => (
            <article key={r.id as string} data-testid="rule" data-rule-id={r.id} data-authority={r.authority} className="rounded-md border border-border p-3 grid gap-1 text-sm">
              <div className="flex flex-wrap items-center gap-2"><strong>{r.title as string}</strong><StatePill state={r.status as string} label={STATUS[r.status as string] ?? (r.status as string)} /></div>
              <span>Who acts: <span className="capitalize">{who(r.actor_ref)}</span></span>
              <span>When it applies: {String(r.condition_text ?? "")}</span>
              <span>Exceptions: {((r.exceptions ?? []) as string[]).length ? (r.exceptions as string[]).join("; ") : "None recorded"}</span>
              <Muted>{IMPL[r.implementation_status as string] ?? String(r.implementation_status)}</Muted>
              <Tech><Muted>rule <code>{r.id as string}</code> - decisions {JSON.stringify(r.decision_ids)} - sources {JSON.stringify(r.evidence_refs)} - unknown {JSON.stringify(r.unknown_fields)}</Muted></Tech>
            </article>
          ))}
        </section>
      ))}
      <Tech>
        <div data-testid="rules-relations" className="text-xs grid gap-1">
          {((graph.edges ?? []) as Rec[]).map((e) => <span key={e.edge_id as string}><code>{e.edge_id as string}</code> {e.type as string}: <code>{e.from as string}</code> {"->"} <code>{e.to as string}</code> ({e.status as string})</span>)}
        </div>
      </Tech>
    </div>
  );
}
