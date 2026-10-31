// The business rules of the current knowledge, in business words; the Technical view adds ids, provenance references and relations. Read only, except
// KNO-05 (2026-10-31): a person with knowledge authority may withdraw the decision behind a rule decided by people (preview of what it supersedes, a
// reason, one confirm); the server records a new knowledge version and asks the question again; nothing is deleted.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, StatePill, Tech, TextArea } from "@/components/bits";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
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

export function BusinessRules({ ws, onChanged }: { ws: string; onChanged?: () => void }) {
  const s = useSession();
  const [graph, setGraph] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState(false);
  const [tick, setTick] = React.useState(0);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/graph?workspace_id=${q(ws)}`).then((r) => { if (live) { setGraph(r); setErr(false); } })
      .catch((e) => { if (live && e instanceof ApiError) setErr(true); });
    return () => { live = false; };
  }, [ws, tick]);
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
              {r.authority === "TARGET" && r.status !== "SUPERSEDED" && ((r.decision_ids ?? []) as string[]).length > 0 && s.can("knowledge.confirm") && (
                <RevokeDecision ws={ws} rule={r} rules={rules} onDone={() => { setTick((n) => n + 1); onChanged?.(); }} />
              )}
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


// KNO-05: withdraw the decision a person made (the answer behind a rule decided by people). Preview first: every rule that stands on the same decision is
// listed as what will be marked withdrawn, and the question is asked again. A reason is required; the server refuses a stale base and an unknown or
// already withdrawn decision; nothing is deleted (the earlier versions and the decision stay in the history).
function RevokeDecision({ ws, rule, rules, onDone }: { ws: string; rule: Rec; rules: Rec[]; onDone: () => void }) {
  const notify = useNotify();
  const [open, setOpen] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const decisionId = ((rule.decision_ids ?? []) as string[])[0];
  const affected = rules.filter((x) => x.status !== "SUPERSEDED" && ((x.decision_ids ?? []) as string[]).includes(decisionId));
  const confirm = async () => {
    if (!reason.trim()) { setError("Say why the decision is withdrawn; the reason is recorded with the new version."); return; }
    setPending(true); setError(null);
    try {
      const r = await api<Rec>("POST", "/v2/business/decisions/revoke", { workspace_id: ws, decision_id: decisionId, reason: reason.trim() });
      notify.status(`Decision withdrawn: version ${String(r.business_revision)} of the business knowledge; the question is asked again.`);
      setOpen(false); setReason("");
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError("The knowledge moved (sources, analysis or another decision) after this version; extract and analyse again, then withdraw.");
      else if (e instanceof ApiError && e.status === 403) setError("Your account may not withdraw decisions.");
      else setError(e instanceof ApiError ? e.message : String(e));
    } finally { setPending(false); }
  };
  if (!open) {
    return <div><Button size="sm" variant="ghost" data-testid="rule-revoke" data-decision-id={decisionId} onClick={() => setOpen(true)}>Withdraw the decision behind this rule</Button></div>;
  }
  return (
    <div data-testid="rule-revoke-form" data-decision-id={decisionId} className="grid gap-2 rounded-md border border-border p-3">
      <p className="font-medium">Withdraw this decision?</p>
      <ul className="list-disc pl-5" data-testid="rule-revoke-preview">
        {affected.map((x) => <li key={x.id as string} data-testid="rule-revoke-affected" data-rule-id={x.id}>{x.title as string}: marked withdrawn</li>)}
        <li>The question it answered is asked again; the earlier versions and the decision stay in the history. Source files and code are not changed.</li>
      </ul>
      <Field label="Why"><TextArea data-testid="rule-revoke-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" data-testid="rule-revoke-confirm" disabled={pending || !reason.trim()} onClick={() => void confirm()}>Withdraw the decision</Button>
        <Button variant="secondary" data-testid="rule-revoke-cancel" disabled={pending} onClick={() => { setOpen(false); setError(null); }}>Keep it</Button>
      </div>
      {error && <Callout tone="danger" testid="rule-revoke-error">{error}</Callout>}
    </div>
  );
}
