// Questions & Decisions: what a person must confirm, answer or resolve. Every control records a
// human decision through an existing route; nothing here edits a document.
import * as React from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Quote } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Denied, Callout, Card, ClaimLabel, ClaimTitle, Field, HowItWorks, Muted, Path, StatePill, Tech, TechField, TextArea, Select } from "@/components/bits";
import { basename, humanize, newId, ruleTitle, scopeText, valueText } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { PurposeChat } from "@/features/freetext/PurposeChat";
import { askSurface, questionReplySurface } from "@/features/freetext/surfaces";
import { useParam } from "@/lib/param";
import { StatusBadge } from "@/components/StatusBadge";
import type { Rec } from "@/features/changes/types";
import { BusinessGaps } from "./BusinessGaps";
import { BusinessAsk } from "./BusinessAsk";
import { BusinessChange } from "./BusinessChange";
import { ReadBusinessRules } from "./BusinessRead";
import { AwaitingRuns } from "./RunHold";
import { BusinessRules } from "./BusinessRules";
import { BusinessUnlinked } from "./BusinessUnlinked";
import { SectionNav } from "@/components/SectionNav";
import { ALL_SECTIONS, deepLinked, GROUP_LABEL, SECTIONS, selectedSection, visible, type SectionId } from "./sections";

const q = encodeURIComponent;

interface Data { hidden: Rec; items: Rec[]; claimsById: Record<string, Rec>; answersByQ: Record<string, Rec>; confirmed: Set<string>; overview: Rec; history: Rec; head: Rec }

async function load(ws: string): Promise<Data> {
  const [queue, overview, history, head] = await Promise.all([
    api<Rec>("GET", `/v2/hitl/queue?workspace_id=${q(ws)}`),
    api<Rec>("GET", `/v2/knowledge/overview?workspace_id=${q(ws)}`),
    api<Rec>("GET", `/v2/knowledge/history?workspace_id=${q(ws)}`),
    api<Rec>("GET", `/v2/workspaces/${q(ws)}/head`),
  ]);
  const claimsById: Record<string, Rec> = {};
  for (const c of (overview.claims ?? []) as Rec[]) claimsById[c.candidate_id] = c;
  const answersByQ: Record<string, Rec> = {};
  for (const a of (overview.answers ?? []) as Rec[]) answersByQ[a.question_id] = a;
  const confirmed = new Set<string>(((overview.decisions ?? []) as Rec[]).filter((d) => d.active && d.action === "confirm").map((d) => d.target));
  const seen = new Set<string>();
  const items: Rec[] = [];
  for (const it of (queue.items ?? []) as Rec[]) if (!seen.has(it.question_id)) { seen.add(it.question_id); items.push(it); }
  return { hidden: (queue.hidden ?? {}) as Rec, items, claimsById, answersByQ, confirmed, overview, history, head };
}

const provenanceOf = (c: Rec, confirmed: Set<string>) => (confirmed.has(c.candidate_id) ? "human-confirmed" : c.applicability === "UNKNOWN" ? "unknown" : "inferred");
const PROV_WORDS: Record<string, string> = { "human-confirmed": "Confirmed by a person", inferred: "Read by the AI - not confirmed", unknown: "Applicability unknown" };

/** A plain description of the rule a question is about (never the stored generic prompt). */
export function claimTitle(c: Rec): string {
  return `${ruleTitle(c.subject, c.predicate)} = ${valueText(c.value, c.unit, c.unit)}${c.logical_path ? ` (${basename(c.logical_path)})` : ""}`;
}
function bizPrompt(item: Rec): string {
  const c = (item.candidates ?? [])[0];
  if (c && /^Human confirmation required/.test(item.prompt ?? "")) return `Confirm this rule: ${claimTitle(c)}`;
  return item.prompt || item.question_id;
}

function ClaimRow({ ws, claim, prov, qid, rationale, onDone }: { ws: string; claim: Rec; prov: string; qid: string | null; rationale: string; onDone: () => Promise<void> }) {
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  return (
    <div data-testid="claim" data-claim-id={claim.candidate_id} data-provenance={prov} className="rounded-md border border-border p-3 grid gap-2">
      <p className="text-sm flex flex-wrap items-center gap-2">
        <span data-testid="provenance-badge" className="rounded-full border border-border px-2 text-xs">{prov}</span>
        <strong><ClaimTitle c={claim} /></strong>
        <span>= {valueText(claim.value, claim.unit, claim.unit)}</span>
        {claim.modality && <span className="text-muted">({humanize(claim.modality)})</span>}
      </p>
      <Muted>{PROV_WORDS[prov] ?? prov}. Source: <Path p={claim.logical_path || claim.artifact_id || "unknown file"} />. Applies to: {scopeText(claim.scope)}.</Muted>
      <Tech><Muted>claim <code>{claim.candidate_id}</code> / <code>{claim.predicate}</code> @ <code>{claim.source_revision_id ?? ""}</code></Muted></Tech>
      {prov === "unknown" && claim.applicability_reason && <Muted>Why unknown: {claim.applicability_reason}</Muted>}
      {((claim.evidence ?? []) as Rec[]).map((e, i) => <blockquote key={i} data-testid="claim-quote" className="border-l-2 border-border pl-3 text-sm text-muted"><Quote size={12} aria-hidden="true" className="inline mr-1" />{e.quote}</blockquote>)}
      {claim.evidence_redacted && <Muted>Source quotes are hidden for your role.</Muted>}
      {qid && s.can("knowledge.confirm") && prov !== "human-confirmed" && (
        <div>
          <Button variant="primary" disabled={pending} onClick={async () => {
            const r = await run(() => api("POST", "/v2/knowledge/confirm", { workspace_id: ws, claim_id: claim.candidate_id, ...(rationale.trim() ? { rationale: rationale.trim() } : {}) }), { stale: true });
            if (r) { notify.status(`Decision recorded: ${ruleTitle(claim.subject, claim.predicate)} confirmed by you.`); await onDone(); }
          }}>Confirm</Button>
        </div>
      )}
    </div>
  );
}

function QueueItem({ ws, item, data, open, setOpen, onDone }: { ws: string; item: Rec; data: Data; open: boolean; setOpen: (v: boolean) => void; onDone: () => Promise<void> }) {
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [answer, setAnswer] = React.useState("");
  const [rationale, setRationale] = React.useState("");
  const qid = item.question_id as string;
  const [conv, setConv] = useParam(`qr-${qid.slice(-8)}`);
  const ans = data.answersByQ[qid];
  const headBtn = React.useRef<HTMLButtonElement>(null);
  const status = item.status as string;
  return (
    <article data-testid="queue-item" data-question-id={qid} data-open={String(open)} className="rounded-lg border border-border bg-surface p-4 grid gap-3"
      onClick={(e) => { if (!open && e.target === e.currentTarget) setOpen(true); }}>
      <button ref={headBtn} type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex flex-wrap items-center gap-3 text-left cursor-pointer w-full">
        {open ? <ChevronDown size={18} aria-hidden="true" /> : <ChevronRight size={18} aria-hidden="true" />}
        <span className="flex-1 min-w-0 font-medium" data-testid="queue-item-prompt">
          <span>{bizPrompt(item)}</span>
          <Tech><span className="block text-xs text-muted font-normal">{item.prompt ?? qid}</span></Tech>
        </span>
        <StatePill state={status} label={status === "PENDING" ? "Waiting for a person" : status === "ANSWERED" ? "Answered - not yet a decision" : humanize(status)} testid="queue-item-status" />
        {ans && <span className="text-sm text-muted"><span data-testid="queue-item-answer">{ans.answer_text}</span> - answered by <span data-testid="answer-author">{ans.actor_id}</span></span>}
      </button>
      {open && (
        <div className="grid gap-3">
          {s.can("knowledge.confirm") && (
            <Field label="Rationale - why you are confirming this" hint="Recorded with your name next to your decision. It is used when you press Confirm on a rule below.">
              <TextArea id={`rat-${qid}`} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
            </Field>
          )}
          {((item.candidates ?? []) as Rec[]).map((cand) => {
            const claim = data.claimsById[cand.candidate_id] ?? { ...cand, evidence_redacted: !cand.evidence };
            return <ClaimRow key={cand.candidate_id} ws={ws} claim={claim} prov={provenanceOf(claim, data.confirmed)} qid={qid} rationale={rationale} onDone={onDone} />;
          })}
          {!(item.candidates ?? []).length && <Muted>No candidate claims are attached to this question.</Muted>}
          {s.can("knowledge.answer") && (
            <div className="grid gap-2">
              <Field label="Your answer" hint="Saved as a comment with your name. It is not knowledge until a person with authority confirms a rule."><TextArea id={`ans-${qid}`} rows={3} value={answer} onChange={(e) => setAnswer(e.target.value)} /></Field>
              <div>
                <Button variant="primary" disabled={pending} onClick={async () => {
                  const t = answer.trim(); if (!t) { notify.status("Write an answer first."); return; }
                  const r = await run(() => api<Rec>("POST", "/v2/hitl/answer", { workspace_id: ws, question_id: qid, answer: t, idempotency_key: newId("ans"), expected_version: item.version }), { stale: true });
                  if (r) { setAnswer(""); notify.status(`Answer recorded (version ${r.version_after ?? r.version ?? ""}). It is a draft until a knowledge owner confirms.`); await onDone(); }
                }}>Submit answer</Button>
              </div>
            </div>
          )}
          {(s.can("knowledge.answer") || s.can("knowledge.confirm")) && (
            <details data-testid="qr-details" className="rounded-md border border-border p-3">
              <summary className="cursor-pointer font-semibold text-sm">Or answer in your own words and let Ripple show how it will be recorded</summary>
              <div className="mt-3"><PurposeChat ws={ws} surface={questionReplySurface(ws, (item.candidates ?? []) as Rec[], (item.version as number | null) ?? null, onDone)} context={{ question_id: qid }} convId={conv} onConvId={setConv} /></div>
            </details>
          )}
          <div><Button data-testid="question-hide-toggle" onClick={(e) => { e.stopPropagation(); setOpen(false); headBtn.current?.focus(); }}>Hide details</Button></div>
        </div>
      )}
    </article>
  );
}

function Findings({ ws, data, onDone }: { ws: string; data: Data; onDone: () => Promise<void> }) {
  const s = useSession();
  const { run, pending } = useAction();
  const notify = useNotify();
  const [picked, setPicked] = React.useState<Record<string, string>>({});
  const [scopeText, setScopeText] = React.useState<Record<string, string>>({});
  const [focus] = useParam("finding");                              // a link from a change page points at ONE finding
  React.useEffect(() => { if (focus) window.setTimeout(() => document.querySelector(`[data-finding-id="${CSS.escape(focus)}"]`)?.scrollIntoView({ block: "center" }), 300); }, [focus, data]);
  const findings = (data.overview.findings ?? []) as Rec[];
  if (!findings.length)
    return <Muted testid="no-findings">No findings are recorded for this workspace. This only means no finding check has reported anything; it is not proof that the sources agree. Run the checks first (or open the Explorer to see what was and was not analysed).</Muted>;
  return (
    <div className="grid gap-3">
      {findings.map((f) => {
        const resolved = !!f.decision;
        const canResolve = s.can("conflict.resolve") && !resolved && f.kind === "CONFLICT";
        return (
          <article key={f.finding_id} data-testid="finding-row" data-finding-id={f.finding_id} data-state={resolved ? "RESOLVED" : "OPEN"} className={`rounded-lg border bg-surface p-4 grid gap-2 ${focus === f.finding_id ? "border-primary ring-2 ring-primary" : "border-border"}`}>
            <p className="text-sm flex flex-wrap items-center gap-2"><strong>{humanize(f.kind)}</strong><span className="rounded-full border border-border px-2 text-xs">{f.severity ?? "n/a"}</span><span className="text-muted">{f.reason}</span></p>
            {((f.claims ?? []) as Rec[]).map((c) => (
              <label key={c.candidate_id} data-testid="finding-side" data-claim-id={c.candidate_id} className="flex items-start gap-2 text-sm">
                {canResolve && <input type="radio" name={`auth-${f.finding_id}`} value={c.candidate_id} checked={picked[f.finding_id] === c.candidate_id} onChange={() => setPicked((p) => ({ ...p, [f.finding_id]: c.candidate_id }))} className="mt-1" />}
                <span><ClaimLabel c={c} /></span>
              </label>
            ))}
            {f.plain && <p className="text-sm" data-testid="finding-plain">{f.plain}{(f.runs ?? 1) > 1 ? ` (the same problem was found by ${f.runs} check runs; it counts once)` : ""}</p>}
            {f.todo && !resolved && <p className="text-sm text-muted" data-testid="finding-todo">What to do: {f.todo}</p>}
            {resolved && <Muted>Resolved by {f.decision.actor_id}.<Tech> authoritative claim {f.decision.authoritative_claim_id ?? "n/a"}</Tech></Muted>}
            {s.can("conflict.resolve") && !resolved && f.kind === "GAP" && (f.claims ?? []).length > 0 && (
              <div className="grid gap-2" data-testid="gap-resolve">
                <Field label="Where does this rule apply? (your own statement, recorded with your name)" hint="Not filled in for you. A serious gap cannot be accepted as unknown; a person with the right states the scope.">
                  <TextArea data-testid="gap-scope-text" rows={2} value={scopeText[f.finding_id] ?? ""} onChange={(e) => setScopeText((p) => ({ ...p, [f.finding_id]: e.target.value }))} />
                </Field>
                <div>
                  <Button disabled={!(scopeText[f.finding_id] ?? "").trim() || pending} data-testid="action-gap-resolve" onClick={async () => {
                    const t = (scopeText[f.finding_id] ?? "").trim();
                    const r = await run(() => api("POST", "/v2/conflict/resolve", { workspace_id: ws, conflict_id: f.finding_id, authoritative_claim_id: f.claims[0].candidate_id, rationale: t, scope: { stated_scope: t } }), { stale: true });
                    if (r) { notify.status("Recorded as your decision: where this rule applies."); await onDone(); }
                  }}>Record where this rule applies</Button>
                </div>
              </div>
            )}
            {canResolve && (
              <div>
                <Button disabled={!picked[f.finding_id] || pending} onClick={async () => {
                  const r = await run(() => api("POST", "/v2/conflict/resolve", { workspace_id: ws, conflict_id: f.finding_id, authoritative_claim_id: picked[f.finding_id] }), { stale: true });
                  if (r) { notify.status("Conflict resolved: the authoritative claim is recorded as your decision."); await onDone(); }
                }}>Resolve conflict</Button>
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}

function Versions({ ws, data, onDone }: { ws: string; data: Data; onDone: () => Promise<void> }) {
  const s = useSession();
  const { run, pending } = useAction();
  const notify = useNotify();
  const [from, setFrom] = React.useState("");
  const [to, setTo] = React.useState("");
  const revs = (data.history.revisions ?? []) as Rec[];
  const byId = Object.fromEntries(revs.map((r) => [r.revision_id, r]));
  const a = byId[from], b = byId[to];
  const claimsN = (data.overview.claims ?? []).length;
  const decided = ((data.overview.decisions ?? []) as Rec[]).filter((x) => x.active && x.action === "confirm").length;
  const h = data.head;
  return (
    <div className="grid gap-4">
      <Card title="Where the knowledge stands" testid="knowledge-explain">
        <ul className="list-disc pl-5 text-sm grid gap-1">
          <li data-testid="km-read"><strong>{claimsN} rule(s) read by the AI from your files.</strong> {decided} confirmed by a person, {Math.max(claimsN - decided, 0)} waiting.{claimsN ? "" : " Nothing has been read yet: analyse a file first."}</li>
          <li data-testid="km-decisions"><strong>{decided} decision(s) by people.</strong></li>
          <li data-testid="km-accepted"><strong>{h.knowledge_revision_id ? "A knowledge version is accepted." : "No knowledge version is accepted yet."}</strong> Confirming rules does not accept a version by itself.</li>
        </ul>
        <HowItWorks testid="knowledge-how">
          <p>A decision is one person confirming one rule or answer. It changes no document.</p>
          <p>Accepting a version is a separate step: it says "this confirmed set is what the system answers from".</p>
        </HowItWorks>
        <TechField label="knowledge revision" value={h.knowledge_revision_id ?? "none"} />
        <TechField label="decision epoch" value={h.decision_epoch ?? ""} />
      </Card>
      {s.can("knowledge.confirm") && (
        <Card title="Accept knowledge" testid="km-accept-panel">
          {h.source_revision_id ? (
            <>
              <p className="text-sm">Accepting records this confirmed set as the knowledge the system answers from, for the sources as they are now.</p>
              <Muted>The request carries exactly the state you see. If the workspace moved since this page loaded, the server refuses and nothing is published.</Muted>
              <Tech><Muted>pins source revision {h.source_revision_id} at decision epoch {String(h.decision_epoch)}</Muted></Tech>
              <div>
                <Button variant="primary" disabled={pending} onClick={async () => {
                  const r = await run(() => api("POST", "/v2/knowledge/accept", { workspace_id: ws, knowledge_revision_id: `km-${newId("v").slice(2)}`, source_revision_id: h.source_revision_id, decision_epoch: h.decision_epoch, expected_version: h.version, idempotency_key: newId("acc") }), { stale: true });
                  if (r) { notify.status("Knowledge revision accepted."); await onDone(); }
                }}>Accept knowledge</Button>
              </div>
            </>
          ) : <Muted>There is no source revision to accept knowledge for yet.</Muted>}
        </Card>
      )}
      <Card title="Knowledge versions">
        {revs.length ? revs.map((r) => (
          <p key={r.revision_id} data-testid="km-revision" data-revision-id={r.revision_id} data-current={String(r.revision_id === data.history.current_knowledge_revision_id)} className="text-sm flex flex-wrap items-center gap-2">
            <code>{r.revision_id}</code> <span className="text-muted">source <code>{r.source_revision_id}</code> - epoch {r.decision_epoch}</span>
            {r.revision_id === data.history.current_knowledge_revision_id && <span className="rounded-full border border-border px-2 text-xs">current</span>}
          </p>
        )) : <Muted>No knowledge version has been accepted yet.</Muted>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Compare from"><Select data-testid="km-compare-from" value={from} onChange={(e) => setFrom(e.target.value)}><option value="">Select a revision</option>{revs.map((r) => <option key={r.revision_id} value={r.revision_id}>{r.revision_id}</option>)}</Select></Field>
          <Field label="Compare to"><Select data-testid="km-compare-to" value={to} onChange={(e) => setTo(e.target.value)}><option value="">Select a revision</option>{revs.map((r) => <option key={r.revision_id} value={r.revision_id}>{r.revision_id}</option>)}</Select></Field>
        </div>
        <div data-testid="km-compare-result" className="text-sm grid gap-1">
          {a && b ? (
            <>
              <p>From: <code>{a.revision_id}</code> - source revision <code>{a.source_revision_id}</code> - decision epoch {a.decision_epoch}</p>
              <p>To: <code>{b.revision_id}</code> - source revision <code>{b.source_revision_id}</code> - decision epoch {b.decision_epoch}</p>
              <Muted>{a.source_revision_id === b.source_revision_id ? "Same source revision." : "Different source revisions."}</Muted>
            </>
          ) : <Muted>Choose two revisions to compare.</Muted>}
        </div>
      </Card>
    </div>
  );
}

function AskCard({ ws }: { ws: string }) {
  const [qa, setQa] = useParam("qa");
  return (
    <section aria-labelledby="q-ask" className="rounded-lg border border-border bg-surface p-4 grid gap-3" data-testid="ask-card">
      <h2 id="q-ask" className="sr-only">Ask about your documents</h2>
      <PurposeChat ws={ws} surface={askSurface(ws)} convId={qa} onConvId={setQa} />
    </section>
  );
}

export function QuestionsPage() {
  const s = useSession();
  const ws = s.route.ws!;
  const [data, setData] = React.useState<Data | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [open, setOpenSet] = React.useState<Set<string>>(new Set());
  const [uiSection, setUiSection] = useParam("ui_section");
  const [chg] = useParam("chg"), [bq] = useParam("q"), [finding] = useParam("finding"), [qa] = useParam("qa");
  const canAsk = s.can("ai.use");
  const available = SECTIONS.map((x) => x.id).filter((id) => id !== "source-ask" || canAsk) as SectionId[];
  const sel = selectedSection(uiSection, available, { chg, q: bq, finding, qa });
  const choose = (id: string) => setUiSection(id || ALL_SECTIONS);          // "All sections" is explicit: no value means the default section
  const [bizKey, setBizKey] = React.useState(0);                            // F-ACC-1: reload the business sections after the rules were read
  const sessionRefresh = s.refresh;
  const refresh = React.useCallback(async () => {
    try { setData(await load(ws)); setErr(null); void sessionRefresh(); } catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws, sessionRefresh]);
  React.useEffect(() => { void refresh(); }, [refresh]);
  React.useEffect(() => { const f = () => void refresh(); window.addEventListener("ripple:reload", f); return () => window.removeEventListener("ripple:reload", f); }, [refresh]);

  if (err && (err.status === 403 || err.status === 404)) return (<><PageHeader id="questions" /><Denied err={err} /></>);
  if (err) return (<><PageHeader id="questions" /><Callout tone="danger" testid={err.status === 403 || err.status === 404 ? "link-unavailable" : "state-error"}>{err.status === 403 || err.status === 404 ? "This page is not available to your account. Nothing was changed and nothing from that workspace is shown." : "The server could not complete the request."} <Button onClick={() => void refresh()}>Try again</Button></Callout></>);
  if (!data) return (<><PageHeader id="questions" /><StatusBadge status="loading" /></>);
  const attached = new Set<string>();
  for (const it of data.items) for (const c of (it.candidates ?? []) as Rec[]) attached.add(c.candidate_id);
  const loose = ((data.overview.claims ?? []) as Rec[]).filter((c) => !attached.has(c.candidate_id));
  const openFindings = ((data.overview.findings ?? []) as Rec[]).filter((f) => !f.decision).length;
  return (
    <>
      {/* SCN-02: with one change request open, its task frame is the next step; the open-questions line would contradict it (it counts questions, not this request) */}
      <PageHeader id="questions" next={chg && sel === "change" ? undefined : data.items.length ? "Open a question, check the evidence, then confirm or answer." : "No open question is waiting for a person right now."} />
      <SectionNav ariaLabel="Questions sections" testid="questions-sections" prefix="qsec" groupLabels={GROUP_LABEL} selected={sel} onSelect={choose} compact={deepLinked(sel, { chg, q: bq, finding, qa })}
        sections={SECTIONS.filter((x) => available.includes(x.id)).map((x) => ({ id: x.id, group: x.group, label: x.label, badge: x.id === "open" ? `(${data.items.length})` : x.id === "findings" ? `(${openFindings})` : undefined }))} />
      <div className="grid gap-8">
        <AwaitingRuns ws={ws} />
        {canAsk && <div hidden={!visible(sel, "source-ask")} data-testid="qsection-source-ask"><AskCard ws={ws} /></div>}
        <section aria-labelledby="q-ask-biz" hidden={!visible(sel, "ask")} data-testid="qsection-ask" className="grid gap-4">
          <h2 id="q-ask-biz" className="text-lg font-semibold">Ask about the business knowledge</h2>
          <BusinessAsk ws={ws} />
        </section>
        <section aria-labelledby="q-change-biz" hidden={!visible(sel, "change")} data-testid="qsection-change" className="grid gap-4">
          <h2 id="q-change-biz" className="text-lg font-semibold">{chg ? "Change request to a business rule" : "Ask for a change to a business rule"}</h2>
          <BusinessChange ws={ws} />
        </section>
        <section aria-labelledby="q-rules" hidden={!visible(sel, "rules")} data-testid="qsection-rules" className="grid gap-4">
          <h2 id="q-rules" className="text-lg font-semibold">Business rules</h2>
          <ReadBusinessRules ws={ws} refreshKey={bizKey} onRead={() => setBizKey((n) => n + 1)} onNext={() => choose("gaps")} />
          <BusinessRules key={`rules${bizKey}`} ws={ws} />
          <BusinessUnlinked ws={ws} />
        </section>
        <section aria-labelledby="q-biz" hidden={!visible(sel, "gaps")} data-testid="qsection-gaps" className="grid gap-4">
          <h2 id="q-biz" className="text-lg font-semibold">Business questions</h2>
          <ReadBusinessRules ws={ws} refreshKey={bizKey} onRead={() => setBizKey((n) => n + 1)} />
          <BusinessGaps key={`gaps${bizKey}`} ws={ws} />
        </section>
        <section aria-labelledby="q-open" hidden={!visible(sel, "open")} data-testid="qsection-open" className="grid gap-4">
          <h2 id="q-open" className="text-lg font-semibold">Open questions ({data.items.length})</h2>
          {((data.hidden.history ?? 0) + (data.hidden.repeat ?? 0) + (data.hidden.resolved ?? 0)) > 0 && (
            <Muted testid="questions-hidden-note">Not listed: {data.hidden.repeat ?? 0} repeat(s) of the same problem found by another check run, {data.hidden.history ?? 0} about an earlier reading of a file that has changed since (history), {data.hidden.resolved ?? 0} already decided. The same numbers are used on the change page.</Muted>
          )}
          {data.items.length > 0 && <Muted testid="questions-note">{data.items.length} question(s) are open. Each one asks a person to confirm what the AI read, or to answer. Confirming records your decision; it does not edit any file.</Muted>}
          {data.items.length ? data.items.map((it) => (
            <QueueItem key={it.question_id} ws={ws} item={it} data={data} open={open.has(it.question_id)}
              setOpen={(v) => setOpenSet((p) => { const n = new Set(p); if (v) n.add(it.question_id); else n.delete(it.question_id); return n; })} onDone={refresh} />
          )) : (
            <div data-testid="state-empty" className="rounded-lg border border-border bg-surface p-6 grid gap-3">
              <p className="flex items-center gap-3"><CheckCircle2 aria-hidden="true" className="text-success" /><span>No question is waiting for a human in this workspace.</span></p>
              {sel === "open" && (
                <div className="flex flex-wrap items-center gap-2" data-testid="open-empty-next">
                  <span className="text-sm text-muted">You can still:</span>
                  <Button size="sm" variant="secondary" data-testid="open-empty-ask" onClick={() => choose("ask")}>Ask about the business knowledge</Button>
                  <Button size="sm" variant="secondary" data-testid="open-empty-gaps" onClick={() => choose("gaps")}>See business questions</Button>
                  <Button size="sm" variant="secondary" data-testid="open-empty-all" onClick={() => choose("")}>Show all sections</Button>
                </div>
              )}
            </div>
          )}
          {loose.length > 0 && (
            <Card title="Claims without a question">
              <Muted>These were extracted but need no confirmation question; their provenance is shown as the server records it.</Muted>
              {loose.map((c) => <ClaimRow key={c.candidate_id} ws={ws} claim={c} prov={provenanceOf(c, data.confirmed)} qid={null} rationale="" onDone={refresh} />)}
            </Card>
          )}
        </section>
        <section aria-labelledby="q-find" hidden={!visible(sel, "findings")} data-testid="qsection-findings" className="grid gap-4">
          <h2 id="q-find" className="text-lg font-semibold">Findings and conflicts ({openFindings} open)</h2>
          <Findings ws={ws} data={data} onDone={refresh} />
        </section>
        <section aria-labelledby="q-ver" hidden={!visible(sel, "versions")} data-testid="qsection-versions" className="grid gap-4">
          <h2 id="q-ver" className="text-lg font-semibold">Knowledge versions</h2>
          <Versions ws={ws} data={data} onDone={refresh} />
        </section>
      </div>
    </>
  );
}
