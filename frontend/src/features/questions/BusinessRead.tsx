// F-ACC-1 (2026-10-21): turn the extracted source candidates into business knowledge from the UI, through the existing production route POST /v2/business/analyse
// (needs `knowledge.confirm`; deterministic, no model call; idempotent per source / knowledge / decision head). Shown when nothing has been read yet or when the sources
// changed since the last reading (`observed_stale` of GET /v2/business/knowledge). States the condition, the result and the next step (answer the business questions,
// then "Accept this knowledge"); refusals are shown as the server worded them (sandbox lease, no extracted candidates yet). Nothing is accepted by reading.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, HowItWorks, Muted } from "@/components/bits";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;

function refusal(e: unknown): string {
  if (e instanceof ApiError) {
    const code = e.body?.code, msg = typeof e.body?.error === "string" ? e.body.error : "";
    if (code === "sandbox_lease_required") return "Take the writing session on the Sandbox page first, then read the rules again. Nothing was read.";
    if (code === "no_snapshot") return "No rule has been extracted from the sources yet: import the source files and wait for the extraction to finish, then try again. Nothing was read.";
    if (e.status === 403) return `Not allowed: ${msg || "your role cannot read the business rules from the sources"}. Nothing was read.`;
    if (e.status === 409) return `The rules could not be read: ${msg || "the knowledge changed meanwhile"}. Reload and try again. Nothing was read.`;
  }
  return "The rules could not be read. Nothing was read.";
}

/** What reading did with the extracted statements (contract section 18): nothing is hidden, statements that are not rules are listed with what they lack. */
function SourcesSummary({ s }: { s: Rec | null | undefined }) {
  if (!s) return null;
  const items = (s.incomplete_items ?? []) as Rec[];
  const unresolved = (s.unresolved_references ?? []) as Rec[];
  return (
    <div className="grid gap-1 text-sm" data-testid="business-read-sources">
      <span data-testid="business-read-counts">
        {String(s.supporting_facts)} configuration value(s) kept as supporting facts; {String(s.source_links)} link(s) between files; {String(s.incomplete)} statement(s) not read as rules because they name no actor and no outcome.
      </span>
      {Number(s.code_branches) > 0 && (
        <span data-testid="business-read-branches">
          {String(s.code_branches)} decision branch(es) read from the code{Number(s.uncovered_branches) > 0 ? `; ${String(s.uncovered_branches)} of them had no extracted statement and are listed in the analysis` : ""}.
        </span>
      )}
      {unresolved.length > 0 && (
        <span data-testid="business-read-unlinked">{unresolved.length} comparison(s) could not be linked to one configuration value: each is a business question ({unresolved.map((u) => String(u.reference)).join(", ")}).</span>
      )}
      {items.length > 0 && (
        <HowItWorks testid="business-read-incomplete" title={`Statements not read as rules (${String(s.incomplete)})`}>
          <ul className="list-disc pl-5 grid gap-1">
            {items.map((i, n) => <li key={n}>{String(i.title || "untitled")}{i.file ? <span className="text-muted"> - {String(i.file)}</span> : null}</li>)}
          </ul>
          <Muted>They stay in the analysis with their evidence; extract the file again or ask about it if one of them is a rule.</Muted>
        </HowItWorks>
      )}
    </div>
  );
}

/** `onRead` reloads the owning sections (rules, questions, knowledge version). `onNext` opens the Business questions section. */
export function ReadBusinessRules({ ws, onRead, onNext, refreshKey = 0 }: { ws: string; onRead: () => void; onNext?: () => void; refreshKey?: number }) {
  const s = useSession();
  const [st, setSt] = React.useState<Rec | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const [done, setDone] = React.useState<Rec | null>(null);
  const load = React.useCallback(async () => {
    try { setSt(await api<Rec>("GET", `/v2/business/knowledge?workspace_id=${q(ws)}`)); } catch { setSt(null); }
  }, [ws]);
  React.useEffect(() => { void load(); }, [load, refreshKey]);
  if (!st) return null;
  const acc = st.accepted as Rec | null, pend = st.pending as Rec | null, src = st.source_revision_id;
  const readNow = Boolean(pend && pend.analysed && pend.source_revision_id === src);              // an analysis of the CURRENT sources is waiting for acceptance
  const never = !acc && !(pend && pend.analysed);
  const stale = !never && !readNow && (Boolean(st.observed_stale) || Boolean(pend && pend.analysed && pend.source_revision_id !== src));
  if (!never && !stale && !done) return null;
  const may = s.can("knowledge.confirm");
  return (
    <div data-testid="business-read" data-state={done ? "read" : stale ? "stale" : "never"} className="rounded-lg border border-border bg-surface p-4 grid gap-2">
      <p className="text-sm font-medium">{done ? "The business rules were read from the sources."
        : stale ? "The sources changed since the business rules were last read."
        : "The business rules have not been read from the sources yet."}</p>
      {!done && <Muted>Reading compares the extracted rule candidates, finds relations and the questions a person must answer. No model is called and nothing is accepted.</Muted>}
      {done ? (
        <div className="grid gap-2" data-testid="business-read-result">
          <span className="text-sm">{String(done.relations)} relation(s) and {String(done.gaps)} business question(s){done.replayed ? " (already read for these sources; nothing new)" : ""}.</span>
          <SourcesSummary s={done.sources as Rec | null | undefined} />
          <span className="text-sm">Next: answer the business questions, then press "Accept this knowledge" in the Business questions section.</span>
          {onNext && <div><Button size="sm" data-testid="business-read-next" onClick={onNext}>Go to the business questions</Button></div>}
        </div>
      ) : may ? (
        <div><Button variant="primary" data-testid="business-read-run" disabled={busy} onClick={async () => {
          setBusy(true); setErr(null);
          try { const r = await api<Rec>("POST", "/v2/business/analyse", { workspace_id: ws }); setDone(r); onRead(); await load(); }
          catch (e) { setErr(refusal(e)); }
          finally { setBusy(false); }
        }}>{stale ? "Read the business rules again" : "Read the business rules from the sources"}</Button></div>
      ) : <Muted testid="business-read-who">A knowledge owner or an administrator reads the business rules from the sources.</Muted>}
      {err && <Callout tone="danger" testid="business-read-error">{err}</Callout>}
    </div>
  );
}
