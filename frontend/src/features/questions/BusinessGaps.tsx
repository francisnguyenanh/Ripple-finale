// Business questions: gaps found in the business rules (a missing person, a missing condition, two sources that disagree, a suggested
// missing exception). A person answers in their own words, looks at exactly what the answer would change, then confirms. A preview records
// nothing; a confirmation records a decision and a proposal, never an accepted knowledge version and never a change to a file.
// A model may SUGGEST questions (labelled) and may READ a person's words into the structured form (previewed); the person still confirms.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, StatePill, Tech, TextArea, TextInput, Select } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const KIND_WORDS: Record<string, string> = {
  missing_actor: "Who acts?", missing_condition: "When does it apply?", missing_outcome: "What happens?", conflicting_facts: "Sources disagree",
  missing_exception: "Is there an exception?",
};

interface Proposals { status: string; statement: string; reason: string | null; gaps: number; relations: number; coverage: Rec | null; job_id: string | null }
interface Queue { snapshot_id: string | null; gaps: Rec[]; accepted_unknown: Rec[]; critical_open: number; critical_accepted_unknown: number; proposals: Proposals }
interface Reading { status: string; proposed_answer: Rec | null; quote: string | null; uncovered: string[]; message: string | null; preview: Rec | null }

function literal(v: string): string | number { return /^-?\d+$/.test(v.trim()) ? Number(v.trim()) : v.trim(); }
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function GapCard({ ws, gap, onDone }: { ws: string; gap: Rec; onDone: () => Promise<void> }) {
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const kind = gap.kind as string;
  const [choice, setChoice] = React.useState("");
  const [text, setText] = React.useState("");
  const [cond, setCond] = React.useState({ field: "", op: "eq", value: "", unit: "" });
  const [unknown, setUnknown] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [quote, setQuote] = React.useState("");
  const [preview, setPreview] = React.useState<Rec | null>(null);
  const [reading, setReading] = React.useState<Reading | null>(null);
  const [readingBusy, setReadingBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const answer = (): Rec | null => {
    if (unknown) return { accept_unknown: true, ...(reason.trim() ? { reason: reason.trim() } : {}) };
    if (kind === "conflicting_facts") return choice ? { choose_rule_id: choice } : null;
    if (kind === "missing_actor") return text.trim() ? { actor: text.trim() } : null;
    if (kind === "missing_outcome") return text.trim() ? { outcome: text.trim() } : null;
    if (kind === "missing_exception") return text.trim() ? { exception: text.trim() } : null;
    if (kind === "missing_condition" && cond.field.trim() && cond.value.trim())
      return { condition: { field: cond.field.trim(), op: cond.op, value: literal(cond.value), ...(cond.unit.trim() ? { unit: cond.unit.trim() } : {}) } };
    return null;
  };
  const reset = () => { setPreview(null); setReading(null); setError(null); };
  const explain = (e: unknown) => {
    if (e instanceof ApiError) setError(String((e.body as Rec).error ?? "The server refused this answer."));
    else throw e;
  };
  const canDecide = s.can("knowledge.confirm");
  const canRead = canDecide && s.can("ai.use");
  const a = answer();
  const suggested = gap.origin === "model_proposal";
  // What a confirmation records: the model's reading only when it is complete (READY); otherwise the controls.
  const effective: Rec | null = reading && reading.status === "READY" && reading.proposed_answer ? reading.proposed_answer : a;
  const shown: Rec | null = reading && reading.status === "READY" && reading.preview ? reading.preview : preview;

  const interpret = async () => {
    setError(null); setPreview(null); setReading(null); setReadingBusy(true);
    try {
      const start = await api<Rec>("POST", `/v2/business/gaps/${q(gap.gap_id)}/interpret`, { workspace_id: ws, text: quote.trim() });
      let got: Rec = start;
      for (let i = 0; i < 90 && !["done", "failed", "cancelled"].includes(String(got.status)); i++) {
        await sleep(1000);
        got = await api<Rec>("GET", `/v2/business/gaps/${q(gap.gap_id)}/interpret/${q(String(start.job_id))}?workspace_id=${q(ws)}`);
      }
      if (got.status === "done" && got.reading) setReading(got.reading as unknown as Reading);
      else setError(String(got.message ?? "The reading did not finish. The controls still work."));
    } catch (e) { explain(e); } finally { setReadingBusy(false); }
  };

  return (
    <article data-testid="business-gap" data-gap-id={gap.gap_id} data-kind={kind} data-priority={gap.priority} data-origin={suggested ? "model_proposal" : "analysis"}
      className="rounded-lg border border-border bg-surface p-4 grid gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <StatePill state={gap.priority === "critical" ? "blocked" : "pending"} label={gap.priority === "critical" ? "Needs an answer" : "Open"} />
        <span className="text-sm text-muted">{KIND_WORDS[kind] ?? kind}</span>
        {suggested && <StatePill state="pending" label="Suggested by a model - not a fact" testid="business-gap-origin" />}
      </div>
      <p className="font-medium" data-testid="business-gap-question">{gap.question}</p>
      <Muted>About: {((gap.affected ?? []) as Rec[]).map((r) => r.title ?? r.rule_id).join(" and ")}</Muted>
      <Tech><Muted>gap <code>{gap.gap_id}</code> - evidence {((gap.evidence_refs ?? []) as string[]).join(", ") || "none"} - rules {(gap.affected_rule_ids as string[]).join(", ")}</Muted></Tech>
      {canDecide ? (
        <div className="grid gap-3">
          {!unknown && kind === "conflicting_facts" && (
            <fieldset className="grid gap-2" onChange={reset}>
              <legend className="text-sm font-medium">Which one is the business rule?</legend>
              {((gap.options ?? []) as Rec[]).map((o) => (
                <label key={o.rule_id} className="flex items-center gap-2 text-sm">
                  <input type="radio" name={`opt-${gap.gap_id}`} value={o.rule_id} checked={choice === o.rule_id} onChange={() => { setChoice(o.rule_id); reset(); }} />
                  {o.label} <Tech><code>{o.rule_id}</code></Tech>
                </label>
              ))}
            </fieldset>
          )}
          {!unknown && (kind === "missing_actor" || kind === "missing_outcome" || kind === "missing_exception") && (
            <Field label={kind === "missing_actor" ? "Who (a role or team name)" : kind === "missing_outcome" ? "What happens (a few words)" : "The exception (one plain sentence)"}>
              <TextInput data-testid="business-gap-text" value={text} onChange={(e) => { setText(e.target.value); reset(); }} />
            </Field>
          )}
          {!unknown && kind === "missing_condition" && (
            <div className="grid gap-2 sm:grid-cols-4">
              <Field label="About (field)"><TextInput value={cond.field} placeholder="request.net_amount_jpy" onChange={(e) => { setCond({ ...cond, field: e.target.value }); reset(); }} /></Field>
              <Field label="Comparison">
                <Select value={cond.op} onChange={(e) => { setCond({ ...cond, op: e.target.value }); reset(); }}>
                  {[["eq", "is"], ["ne", "is not"], ["gte", "at least"], ["gt", "more than"], ["lte", "at most"], ["lt", "less than"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </Select>
              </Field>
              <Field label="Value"><TextInput value={cond.value} onChange={(e) => { setCond({ ...cond, value: e.target.value }); reset(); }} /></Field>
              <Field label="Unit (optional)"><TextInput value={cond.unit} onChange={(e) => { setCond({ ...cond, unit: e.target.value }); reset(); }} /></Field>
            </div>
          )}
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="business-gap-unknown" checked={unknown} onChange={(e) => { setUnknown(e.target.checked); reset(); }} /> Leave this open on purpose</label>
          {unknown && <Field label="Why it stays open" hint={gap.priority === "critical" ? "Required for this question. It stays visible as an accepted unknown." : undefined}><TextInput data-testid="business-gap-reason" value={reason} onChange={(e) => { setReason(e.target.value); reset(); }} /></Field>}
          <Field label="Your answer in your own words" hint={canRead ? "Recorded with your name next to your decision. A model can read it into the form above; you still check and confirm." : "Recorded with your name next to your decision."}>
            <TextArea data-testid="business-gap-quote" rows={2} value={quote} onChange={(e) => { setQuote(e.target.value); setReading(null); }} />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button data-testid="business-gap-preview" disabled={pending || readingBusy || !a} onClick={async () => {
              setError(null); setReading(null);
              try {
                const r = await run(() => api<Rec>("POST", "/v2/business/gaps/answer-preview", { workspace_id: ws, gap_id: gap.gap_id, answer: a }));
                if (r) setPreview(r);
              } catch (e) { explain(e); }
            }}>See what this changes</Button>
            {canRead && (
              <Button data-testid="business-gap-interpret" disabled={pending || readingBusy || !quote.trim()} onClick={() => void interpret()}>
                {readingBusy ? "Reading your words..." : "Read my words into the form"}
              </Button>
            )}
            {shown && !shown.noop && effective && (
              <Button variant="primary" data-testid="business-gap-confirm" disabled={pending || readingBusy || !quote.trim()} onClick={async () => {
                const r = await run(() => api<Rec>("POST", "/v2/business/gaps/answer", { workspace_id: ws, gap_id: gap.gap_id, answer: effective, quote: quote.trim(), preview_hash: shown.preview_hash }), { stale: true });
                if (r) { notify.status("Decision recorded. It is a proposal until the knowledge is accepted."); await onDone(); }
              }}>Confirm this decision</Button>
            )}
          </div>
          {error && <Callout tone="danger" testid="business-gap-error">{error}</Callout>}
          {reading && reading.status !== "READY" && (
            <Callout tone="warn" testid="business-gap-interpret-uncovered">
              <p className="text-sm font-medium">{reading.status === "UNSUPPORTED" ? "Your words could not be turned into this question's form." : "Part of your answer could not be read, so nothing is proposed from it yet."}</p>
              {reading.uncovered.length > 0 && <ul className="list-disc pl-5 text-sm">{reading.uncovered.map((u, i) => <li key={i} data-testid="business-gap-uncovered-part">{u}</li>)}</ul>}
              {reading.message && <Muted>{reading.message}</Muted>}
              {reading.proposed_answer && <Muted>What was read so far: {JSON.stringify(reading.proposed_answer)} - not confirmable until the rest is clear.</Muted>}
            </Callout>
          )}
          {shown && (
            <div data-testid="business-gap-preview-result" data-source={reading && reading.status === "READY" ? "reading" : "controls"} className="rounded-md border border-border p-3 grid gap-1">
              <p className="text-sm font-medium">{shown.noop ? "Nothing would change" : reading && reading.status === "READY" ? "Read from your words - if you confirm:" : "If you confirm:"}</p>
              <ul className="list-disc pl-5 text-sm">{((shown.summary ?? []) as string[]).map((t, i) => <li key={i}>{t}</li>)}</ul>
              {reading && reading.status === "READY" && <Muted>Read as: {JSON.stringify(reading.proposed_answer)} from "{reading.quote}". Your own words are what gets recorded.</Muted>}
              <Tech><Muted>preview <code>{shown.preview_hash}</code> on snapshot <code>{shown.base_snapshot_id}</code></Muted></Tech>
            </div>
          )}
        </div>
      ) : <Muted>A person with authority over knowledge answers this question.</Muted>}
    </article>
  );
}

function KnowledgeVersion({ ws, refreshKey, onAccepted }: { ws: string; refreshKey: number; onAccepted: () => Promise<void> }) {
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [st, setSt] = React.useState<Rec | null>(null);
  const [ack, setAck] = React.useState(false);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/knowledge?workspace_id=${q(ws)}`).then((r) => { if (live) setSt(r); }).catch(() => { if (live) setSt(null); });
    return () => { live = false; };
  }, [ws, refreshKey]);
  if (!st) return null;
  const acc = st.accepted as Rec | null, prop = st.pending as Rec | null;
  const crit = prop ? ((prop.questions as Rec).critical_open as number) : 0;
  return (
    <div data-testid="business-version" data-revision={acc ? String(acc.business_revision) : "0"} className="rounded-lg border border-border bg-surface p-4 grid gap-2">
      <p className="text-sm">
        {acc ? <>Accepted business knowledge: <strong>version {acc.business_revision as number}</strong> ({acc.rules as number} rules, {acc.target_rules as number} decided by people)
          {(acc.coverage as Rec).partial ? " - some questions are still open" : ""}.</> : "No business knowledge has been accepted yet."}
      </p>
      <Tech><Muted>head <code>{(st.business_head as Rec).accepted_snapshot_id ?? "none"}</code> - index events waiting {(st.index as Rec).pending_events as number} - documents requested {(st.documents as Rec).jobs as number}</Muted></Tech>
      {prop && (
        <div data-testid="business-proposal" className="grid gap-2">
          <p className="text-sm">A proposal is ready: {prop.rules as number} rules, {prop.decisions as number} decision(s), {(prop.questions as Rec).open as number} open question(s){crit ? `, ${crit} important` : ""}.</p>
          {s.can("knowledge.confirm") && (
            <>
              {crit > 0 && <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="business-ack" checked={ack} onChange={(e) => setAck(e.target.checked)} /> Accept it although {crit} important question(s) are still open</label>}
              <div><Button variant="primary" data-testid="business-accept" disabled={pending || (crit > 0 && !ack)} onClick={async () => {
                const r = await run(() => api<Rec>("POST", "/v2/business/accept", { workspace_id: ws, snapshot_id: prop.snapshot_id, acknowledged_open_gaps: ack }), { stale: true });
                if (r) { notify.status("Business knowledge accepted. No file was changed."); await onAccepted(); }
              }}>Accept this knowledge</Button></div>
            </>
          )}
        </div>
      )}
      {((st.history ?? []) as Rec[]).length > 0 && (
        <details><summary className="text-sm cursor-pointer">Earlier versions</summary>
          <ul className="text-sm list-disc pl-5">{((st.history as Rec[])).map((h) => <li key={h.snapshot_id} data-testid="business-history-item">version {h.business_revision as number} - {h.committed_by as string} - {new Date(h.created_at as string).toLocaleString()}</li>)}</ul>
        </details>
      )}
    </div>
  );
}

/** Suggestions by a model: a status line, and for a person with authority a button to ask for them. Suggestions are labelled questions; a person answers or dismisses each. */
function Suggestions({ ws, proposals, onChanged }: { ws: string; proposals: Proposals; onChanged: () => Promise<void> }) {
  const s = useSession();
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const can = s.can("knowledge.confirm") && s.can("ai.use");
  const ask = async () => {
    setBusy(true); setErr(null);
    try {
      await api<Rec>("POST", "/v2/business/gaps/propose", { workspace_id: ws });
      for (let i = 0; i < 90; i++) {
        await sleep(1000);
        const now = await api<Queue>("GET", `/v2/business/gaps?workspace_id=${q(ws)}`);
        if (now.proposals.status === "completed" || now.proposals.status === "failed") break;
      }
      await onChanged();
    } catch (e) {
      if (e instanceof ApiError) setErr(String((e.body as Rec).error ?? "The suggestions could not be requested."));
      else throw e;
    } finally { setBusy(false); }
  };
  const running = proposals.status === "running" || proposals.status === "waiting_for_approval";
  return (
    <div data-testid="business-gaps-proposals" data-status={proposals.status} className="rounded-lg border border-border bg-surface p-4 grid gap-2">
      <p className="text-sm">{proposals.statement}</p>
      {proposals.coverage && (
        <Tech><Muted>model said {String(proposals.coverage.model_status)} - rules shown {String(proposals.coverage.rules_shown)} - not shown {String(proposals.coverage.rules_not_shown)} - rejected {String(proposals.coverage.rejected)} - job <code>{proposals.job_id}</code></Muted></Tech>
      )}
      {can && (proposals.status === "not_requested" || proposals.status === "failed" || running) && (
        <div><Button data-testid="business-gaps-propose" disabled={busy || running} onClick={() => void ask()}>{busy || running ? "Asking a model..." : "Ask a model for suggestions"}</Button></div>
      )}
      {err && <Callout tone="danger" testid="business-gaps-propose-error">{err}</Callout>}
    </div>
  );
}

export function BusinessGaps({ ws }: { ws: string }) {
  const [tick, setTick] = React.useState(0);
  const [data, setData] = React.useState<Queue | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const refresh = React.useCallback(async () => {
    try { setData(await api<Queue>("GET", `/v2/business/gaps?workspace_id=${q(ws)}`)); setErr(null); setTick((t) => t + 1); } catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws]);
  React.useEffect(() => { void refresh(); }, [refresh]);
  if (err) return <Callout tone="danger" testid="business-gaps-error">The business questions could not be loaded.</Callout>;
  if (!data) return null;
  if (!data.snapshot_id) return <Muted testid="business-gaps-none">No business rules have been read from the sources yet, so there are no business questions.</Muted>;
  return (
    <div className="grid gap-4" data-testid="business-gaps" data-count={data.gaps.length}>
      <KnowledgeVersion ws={ws} refreshKey={tick} onAccepted={refresh} />
      {data.proposals && <Suggestions ws={ws} proposals={data.proposals} onChanged={refresh} />}
      {data.critical_accepted_unknown > 0 && <Callout tone="warn" testid="business-gaps-unknown">{data.critical_accepted_unknown} important question(s) are left open on purpose.</Callout>}
      {data.gaps.length === 0 ? <Muted testid="business-gaps-empty">No business question is open.</Muted>
        : data.gaps.map((g) => <GapCard key={g.gap_id} ws={ws} gap={g} onDone={refresh} />)}
      {data.accepted_unknown.length > 0 && <Muted>Left open on purpose: {data.accepted_unknown.map((g) => g.question).join(" | ")}</Muted>}
    </div>
  );
}
