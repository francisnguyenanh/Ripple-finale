import { stageNo, stageTitle } from "./registry";
import { StageCard } from "./StageCard";
import * as React from "react";
import { FileText, Quote } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, StatePill, Tech, TechField, TextArea, TextInput, usePlain } from "@/components/bits";
import { basename, draftPhrase, gapPhrase, humanize, newId, valueText } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { PurposeChat } from "@/features/freetext/PurposeChat";
import { feedbackSurface, gapReplySurface } from "@/features/freetext/surfaces";
import { useParam } from "@/lib/param";
import { useFlow } from "./flow";
import { CoveragePanel, prepareCoverage } from "./Coverage";
import { ApproveJobButton, JobLine, useCreateStage } from "./StageBits";
import type { Gap, Job, Rec } from "./types";
import { isDone, jobState, isTerminal } from "./types";
import { EquivalentStep } from "@/components/EquivalentStep";

export const draftResult = (j: Job | null): Rec | null => (j && jobState(j) === "COMPLETED" ? (j.execution_result ?? null) : null);

const ITEM_PHRASE: Record<string, string> = {
  PROPOSED: "Proposed edit", NO_OP: "Already at the new value", AMBIGUITY: "Needs your confirmation",
  MANUAL_CHANGE: "A person must edit this", UNSUPPORTED: "Not supported",
};

/** An open point that the earlier draft left open and the server kept: never "resolved" because a later AI answer left it out or excluded it. */
export const carriedWords = (x: { disposition?: string; carried_from?: unknown; disposition_basis?: { by?: string; reason?: string } }): string | null =>
  (!x.carried_from && (!x.disposition || x.disposition === "unresolved")) ? null
    : x.disposition === "potentially_affected"
      ? "Potentially affected - the same rule states a different value here. The AI left it out, the server did not close it; it stays open until a person decides" + (x.carried_from ? " (kept from the earlier draft)" : "")
    : x.disposition === "outside_this_request"
      ? `Outside this request${x.disposition_basis?.by ? ` (decided by ${x.disposition_basis.by}` : ""}${x.disposition_basis?.reason ? `${x.disposition_basis?.by ? "; " : " ("}reason: ${x.disposition_basis.reason}` : ""}${x.disposition_basis?.by || x.disposition_basis?.reason ? ")" : ""} - NOT resolved, kept from the earlier draft`
      : x.disposition === "proposed_remediation"
        ? "Proposed remediation only - still open until the edit is applied and verified (kept from the earlier draft)"
        : "Still unresolved - kept from the earlier draft (the new AI answer did not resolve it)";

/** Where an item lives. Business: the file name. Technical: file path and the exact location. */
function Where({ x }: { x: Rec }) {
  const loc = x.locator && (x.locator.key_path ?? x.locator.cell ?? x.locator.symbol ?? x.locator.line_start);
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <FileText size={14} aria-hidden="true" />
      <span className="font-medium">{basename(x.logical_path)}</span>
      <Tech><code>{String(x.logical_path)} {loc !== undefined ? JSON.stringify(loc) : ""}</code></Tech>
    </span>
  );
}

/** Business wording of one open point: the server sentence is machine-shaped ("file: STATUS - reason code"); evidence stays verbatim. */
function gapParts(g: Gap, p: (t: unknown) => string): { head: string; quote?: string } {
  const t = g.text ?? "";
  if (g.kind === "item") {
    const m = /^(.*?): ([A-Z_]+) - ([\s\S]*)$/.exec(t);
    if (m) return { head: `${basename(m[1])}: ${ITEM_PHRASE[m[2]] ?? p(m[2])}${m[3] ? ` - ${p(m[3])}` : ""}` };
  }
  if (g.kind === "unaddressed") {
    const m = /^(\S+)\s*(.*?): not classified by the model - ([\s\S]*)$/.exec(t);
    if (m && g.text_redacted) return { head: `A passage in ${basename(m[1])} was not classified by the AI (the source text is hidden for your role)` };
    if (m) return { head: `A passage in ${basename(m[1])} was not classified by the AI:`, quote: m[3] };
  }
  return { head: p(t) };
}

function GapItem({ g, draftId, arts }: { g: Gap; draftId: string; arts: string[] }) {
  const { ws, data, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [text, setText] = React.useState("");
  const [why, setWhy] = React.useState("");
  const p = usePlain();
  const { mode } = useSession();
  const parts = mode === "technical" ? { head: g.text } : gapParts(g, p);
  const usedByDraft = (((data?.draft?.execution_result ?? {}) as Rec).clarifications_used ?? []) as Rec[];
  const appliedHere = !!g.confirmation && usedByDraft.some((u) => u.actor_id === g.confirmation!.actor_id && u.epoch === g.confirmation!.epoch);
  const sj = data?.suggest[g.gap_id] ?? null;
  const sr = (sj?.execution_result ?? {}) as Rec;
  const { create } = useCreateStage();
  const [gr, setGr] = useParam("gr");
  const grOpen = gr.startsWith(`${g.key}~`);
  const grCid = grOpen ? gr.slice(g.key.length + 1) : "";
  return (
    <div data-testid="gap-item" data-gap-key={g.key} data-state={g.state} data-kind={g.kind} className="rounded-md border border-border p-3 grid gap-2">
      <p className="text-sm flex flex-wrap gap-2 items-center">
        <span className="rounded-full border border-border px-2 text-xs">{g.kind}</span>
        <StatePill state={g.state} label={gapPhrase(g.state)} testid="gap-state" />
        {g.confirmation && g.feedback?.status !== "rejected" && (
          appliedHere
            ? <span data-testid="gap-applied" className="rounded-full border border-border px-2 text-xs text-success">Applied in this draft</span>
            : <span data-testid="gap-pending" className="rounded-full border border-border px-2 text-xs text-warning">Not applied in this draft yet - revise the draft to use it</span>
        )}
        {g.feedback && g.feedback.status !== "none" && (
          <span data-testid="gap-feedback" data-status={g.feedback.status} title={g.feedback.reason} className={`rounded-full border border-border px-2 text-xs ${g.feedback.status === "rejected" || g.feedback.status === "withdrawn" ? "text-danger" : g.feedback.status === "applied" ? "text-success" : "text-warning"}`}>
            {({ pending: "Comment only: not used", applied: "Used by this draft", rejected: "Refused by this draft", not_applied_yet: "Confirmed: not used yet", withdrawn: "Withdrawn: not used" } as Record<string, string>)[g.feedback.status]}
          </span>
        )}
        <span>{g.kind === "question" ? <span data-ai-text>{parts.head}</span> : parts.head}</span>
        {carriedWords(g) && <span data-testid="gap-carried" data-disposition={g.disposition} className="rounded-full border border-border px-2 text-xs text-warning">{carriedWords(g)}</span>}
      </p>
      {parts.quote && <blockquote className="border-l-2 border-border pl-3 text-sm text-muted">{parts.quote}</blockquote>}
      {g.feedback && ["rejected", "withdrawn", "pending"].includes(g.feedback.status) && <Muted testid="gap-feedback-reason">{g.feedback.reason}{g.feedback.by ? ` (${g.feedback.by})` : ""}</Muted>}
      {(g.comments ?? []).map((c, i) => <Muted key={i} testid="gap-comment">Comment (not authoritative) by {c.actor_id}: {c.text}</Muted>)}
      {g.confirmation && (
        <p data-testid="gap-confirmation" className="text-sm">
          <strong>Decided by {g.confirmation.actor_id}</strong>: {g.confirmation.answer}{g.confirmation.rationale ? ` - why: ${g.confirmation.rationale}` : ""}
          <Tech> (epoch {g.confirmation.epoch})</Tech>
        </p>
      )}
      {sj && (
        <div data-testid="gap-suggestion" data-state={jobState(sj)} className="grid gap-1">
          <Muted>AI suggestion (not confirmed; {jobState(sj) === "COMPLETED" ? "finished" : "waiting"}): {jobState(sj) === "COMPLETED" ? `${sr.status ?? ""} - ${sr.answer ?? sr.question_for_human ?? ""}` : "waiting"}</Muted>
          {(sr.evidence_spans ?? []).map((sp: Rec, i: number) => (
            <blockquote key={i} className="border-l-2 border-border pl-3 text-sm text-muted"><Quote size={12} aria-hidden="true" className="inline mr-1" />{sp.quotes_redacted ? <em data-testid="citation-hidden">Cited source text hidden for your role.</em> : sp.quote}<span className="block text-xs">{basename(sp.artifact_path)}</span></blockquote>
          ))}
          <ApproveJobButton job={sj} label="Approve this model call (a different person than the requester)" testid="action-approve-suggest" />
        </div>
      )}
      {!g.confirmation && (
        <div className="grid gap-2">
          {s.can("knowledge.answer") || s.can("knowledge.confirm") ? (
            <div>
              <Button data-testid="gap-reply-chat" aria-expanded={grOpen} onClick={() => setGr(grOpen ? "" : `${g.key}~`)}>{grOpen ? "Close the chat" : "Reply in chat"}</Button>
            </div>
          ) : null}
          {grOpen && (
            <div className="rounded-md border border-border p-3">
              <PurposeChat ws={ws} surface={gapReplySurface(ws, async () => { await refresh(); })} context={{ draft_job_id: draftId, gap_key: g.key }} convId={grCid} onConvId={(id) => setGr(`${g.key}~${id}`)} />
            </div>
          )}
          <TextArea data-testid="gap-text" rows={2} placeholder="Your answer or comment" aria-label={`Answer for: ${g.text}`} value={text} onChange={(e) => setText(e.target.value)} />
          <TextInput data-testid="gap-why" placeholder="Why (recorded with your name)" aria-label="Why" value={why} onChange={(e) => setWhy(e.target.value)} />
          <div className="flex flex-wrap gap-2">
            {s.can("knowledge.answer") && (
              <Button data-testid="gap-comment-save" disabled={pending} onClick={async () => {
                const t = text.trim(); if (!t) { notify.status("Write the comment first."); return; }
                const r = await run(() => api("POST", "/v2/hitl/answer", { workspace_id: ws, question_id: g.gap_id, answer: t, idempotency_key: newId("ans"), expected_version: (g.comments ?? []).length || null }), { stale: true });
                if (r) { setText(""); await refresh(); }
              }}>Save as comment (not authoritative)</Button>
            )}
            {s.can("change.prepare") && arts.length > 0 && (
              <><Button data-testid="gap-suggest" title={aiWhy(s, "gap.answer")} disabled={pending || !s.aiKind("gap.answer")} onClick={async () => {
                const r = await create("gap.answer", newId("ui-suggest"), { question: g.text, question_id: `suggest:${g.gap_id}`, artifact_ids: arts });
                if (r) await refresh();
              }}>Ask the AI to suggest (model call, needs approval)</Button><EquivalentStep kind="gap.answer" to="ask" /></>
            )}
            {s.can("knowledge.confirm") && (
              <Button variant="primary" data-testid="gap-confirm" disabled={pending} onClick={async () => {
                const t = text.trim(); if (!t) { notify.status("Write the answer to confirm first."); return; }
                const r = await run(() => api("POST", "/v2/change/gap/confirm", { workspace_id: ws, draft_job_id: draftId, gap_key: g.key, answer: t, rationale: why.trim() }), { stale: true });
                if (r) { setText(""); setWhy(""); notify.status("Decision recorded by you. This draft is now stale; the source is unchanged."); await refresh(); }
              }}>Confirm this answer as a decision</Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function GapPanel({ draftId, arts }: { draftId: string; arts: string[] }) {
  const { data } = useFlow();
  const G = data?.gaps;
  if (!G || !G.completed) return null;
  return (
    <div className="grid gap-3">
      <h3 className="font-semibold">Open points to answer ({G.gaps.length})</h3>
      <Muted>A comment and an AI suggestion are NOT authoritative. Only a confirmed answer (from a person with confirm rights) is given to the next draft, as a stated assumption. Nothing here edits a source or closes a finding.</Muted>
      <Muted testid="gap-wish-note">If your answer asks for behaviour the system does not have today (for example emergency orders following the new limit), it is recorded as what you want, not as a fact. Making the system do it needs a code change, which is planned for the final product.</Muted>
      {G.stale && <Callout tone="warn" testid="draft-stale">Out of date: {(G.stale_reasons ?? []).join("; ")}. Use "Generate GenAI draft" again to include the confirmed answers.</Callout>}
      {(G.clarifications_used ?? []).length > 0 && (
        <div data-testid="clarifications-used" className="text-sm grid gap-1">
          <p>This draft used {G.clarifications_used!.length} confirmed answer(s), each recorded with the name of the person who confirmed it:</p>
          <ul className="list-disc pl-5">
            {G.clarifications_used!.map((c, i) => <li key={c.decision_id ?? i}>Confirmed by <strong>{c.actor_id}</strong>{c.answer ? <>: <span data-verbatim>"{c.answer}"</span></> : null}{c.rationale ? <> - why: <span data-verbatim>{c.rationale}</span></> : null}</li>)}
          </ul>
        </div>
      )}
      {(G.feedback_log ?? []).length > 0 && (
        <div data-testid="feedback-log" className="text-sm grid gap-1">
          <p>Confirmed answers on this request, and what THIS draft did with each:</p>
          <ul className="list-disc pl-5">
            {G.feedback_log!.map((f) => (
              <li key={f.decision_id} data-testid="feedback-log-item" data-status={f.status}>
                <strong>{({ applied: "Used", rejected: "Refused", not_applied_yet: "Not used yet", withdrawn: "Withdrawn" } as Record<string, string>)[f.status]}</strong>: {f.question ? <span data-verbatim>{f.question} </span> : null}-&gt; <span data-verbatim>"{f.answer}"</span> (confirmed by {f.by}) - {f.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {G.gaps.map((g) => <GapItem key={g.gap_id} g={g} draftId={draftId} arts={arts} />)}
    </div>
  );
}

function FeedbackOnDraft({ draftId }: { draftId: string }) {
  const { ws, data, refresh } = useFlow();
  const [fb, setFb] = useParam("fb");
  const s = useSession();
  if (!s.can("intent.create")) return null;
  const gaps = (data?.gaps?.gaps ?? []) as unknown as Rec[];
  return (
    <details data-testid="draft-feedback" open={!!fb} onToggle={(e) => { if (!(e.target as HTMLDetailsElement).open && fb) setFb(""); }} className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium" onClick={() => { if (!fb) setFb("~"); }}>Give feedback on this draft in your own words</summary>
      <div className="mt-3">
        <PurposeChat ws={ws} surface={feedbackSurface(ws, gaps, async () => { await refresh(); })} context={{ draft_job_id: draftId }} convId={fb === "~" ? "" : fb} onConvId={setFb} />
      </div>
    </details>
  );
}

export function DraftStage() {
  const { ws, ids, data, setIds, refresh } = useFlow();
  const s = useSession();
  const p = usePlain();
  const { create, pending } = useCreateStage();
  const d = data;
  const confirmed = d?.intent?.status === "CONFIRMED";
  const running = !!d?.draft && !isTerminal(d.draft);
  const r = draftResult(d?.draft ?? null);
  const arts = Array.from(new Set(((r?.items ?? []) as Rec[]).map((x) => x.artifact_id).filter(Boolean)));
  const usage = d?.draft?.execution_usage ?? {};
  const [lin, setLin] = React.useState<Rec | null>(null);
  React.useEffect(() => {
    let live = true;
    if (!ids.draft || !ws) { setLin(null); return; }
    api<Rec>("GET", `/v2/change/draft-lineage?workspace_id=${encodeURIComponent(ws)}&draft_job_id=${encodeURIComponent(ids.draft)}`).then((x) => { if (live) setLin(x); }).catch(() => { if (live) setLin(null); });
    return () => { live = false; };
  }, [ids.draft, ws, d?.draft?.execution_status]);
  const notify = useNotify();
  const { run } = useAction();
  /** The conflict check and the relation coverage of THIS revision exist before the model is asked (deterministic, created once, no AI). */
  const busy = React.useRef(false);
  const generate = async (revise = false, withoutCheck = false) => {
    if (busy.current) return;               // a double click creates ONE job
    busy.current = true;
    try { await generateOnce(revise, withoutCheck); } finally { busy.current = false; }
  };
  const generateOnce = async (revise: boolean, withoutCheck: boolean) => {
    if (!withoutCheck) {
      const prep = await run(() => prepareCoverage(ws));
      if (!prep) return;
      if (prep.result === "waiting") { notify.status("The checks behind the draft are still running. Nothing was sent to the AI: press the button again in a moment."); return; }
      if (prep.result === "failed") { notify.status("A check behind the draft failed (see 'Before the draft'). Nothing was sent to the AI."); return; }
    }
    const id = newId("ui-draft");
    const res = await create("change.draft", id, revise && ids.draft ? { intent_id: ids.intent, revises_draft_job_id: ids.draft } : { intent_id: ids.intent });
    if (!res) return;
    setIds({ draft: id, impact: "", simulation: "", proposal: "", review: "", approval: "", apply: "", query: "" });
    await refresh();
  };
  const openPoints = (d?.gaps?.gaps ?? []).filter((g) => g.state !== "confirmed").length;
  const draftDone = !!r && !!d?.impact && (!d?.gaps?.stale || !!d?.apply);      // once the change is applied the draft is history, even if a later decision made it older
  const draftSummary = r ? (
    <>Draft {draftPhrase(r.status).toLowerCase()}: {(r.items ?? []).length} affected location(s), {openPoints} open point(s), {(r.not_covered ?? []).length} source(s) not covered, {(r.unaddressed ?? []).length} evidence item(s) not classified{d?.gaps?.stale ? ". OUT OF DATE: a decision was recorded after it" : ""}.</>
  ) : undefined;
  return (
    <StageCard title={stageTitle("draft")} testid="flow-draft" done={draftDone} summary={draftSummary}>
      <Muted>A model analyses your confirmed request against the stored evidence and drafts the impact and the proposed edits with citations. The server checks every citation and builds the typed edits itself; nothing is applied here.</Muted>
      <CoveragePanel />
      <JobLine name="draft" job={d?.draft ?? null} usePhase retry />
      {s.can("change.prepare") && (
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" id="action-run-draft" data-testid="action-run-draft" title={aiWhy(s, "change.draft")} disabled={!confirmed || running || pending || !s.aiKind("change.draft")} onClick={() => generate(false)}>Generate GenAI draft</Button><EquivalentStep kind="change.draft" to="change" />
          {r && !lin?.revised_by && <Button data-testid="action-revise-draft" title={aiWhy(s, "change.draft")} disabled={!confirmed || running || pending || !s.aiKind("change.draft")} onClick={() => generate(true)}>Revise this draft with confirmed feedback (uses the paid model)</Button>}
          <ApproveJobButton job={d?.draft ?? null} label="Approve this model call (a different person than the requester)" testid="action-approve-draft" />
        </div>
      )}
      {lin?.revised_by && (
        <Callout tone="warn" testid="draft-superseded">A newer draft replaced this one after new confirmed feedback. The impact, simulation, proposal, review and approval made for this draft are out of date and cannot be approved or applied.{" "}
          <button className="underline" data-testid="open-newer-draft" onClick={() => { setIds({ draft: lin.revised_by as string, impact: "", simulation: "", proposal: "", review: "", approval: "", apply: "", query: "" }); void refresh(); }}>Open the newer draft</button></Callout>
      )}
      {lin?.revises && <Muted testid="draft-revises">This draft revises an earlier one{(r?.revision?.new_clarification_ids ?? []).length ? ` and uses ${((r as Rec).revision.new_clarification_ids as string[]).length} newly confirmed clarification(s)` : ", with no newly confirmed clarification since"}. Earlier work is shown as out of date, not deleted.</Muted>}
      {d?.draft?.status === "PENDING_APPROVAL" && <Muted>In this shared trial workspace a second person must allow this model call before the worker runs it.</Muted>}
      {r && d?.draft && (
        <>
          <p className="text-sm flex flex-wrap items-center gap-2">
            <span>Draft status:</span><StatePill state={r.status} label={draftPhrase(r.status)} testid="draft-status" />
            <Tech><span className="text-muted">model {d.draft.execution_model_id ?? "n/a"} - tokens in {usage.input_tokens ?? "?"} / out {usage.output_tokens ?? "?"} - hash {String(r.draft_hash ?? "").slice(0, 12)}</span></Tech>
          </p>
          <Muted testid="draft-claim">{p(r.claim)}</Muted>
          {r.coverage && (
            <div data-testid="draft-basis" className="rounded-md border border-border p-3 grid gap-1 text-sm">
              <h3 className="font-semibold">What this draft is based on</h3>
              <p data-testid="draft-basis-files">Files read with a current extraction: <strong>{r.coverage.files_with_current_extraction} of {r.coverage.files_in_scope}</strong>
                {(r.coverage.files_without_extraction ?? []).length > 0 && <> - not read: {(r.coverage.files_without_extraction as string[]).map((x) => basename(x)).join(", ")}</>}.</p>
              <Callout tone={r.coverage.findings_for_this_revision ? "info" : "warn"} testid="draft-basis-findings"><span>{r.coverage.statement}</span></Callout>
              <Muted>Relations between rules are not read by the draft: they are computed for the impact analysis (stage {stageNo("analysis")}).</Muted>
            </div>
          )}
          <p data-testid="draft-summary" data-ai-text className="text-sm">{r.summary ?? (r.outcome ? `No AI call was made: ${p(r.outcome)}${(r.reasons ?? []).length ? " - " + (r.reasons as string[]).map(p).join(", ") : ""}` : "")}</p>
          {(r.questions_for_human ?? []).map((q: unknown, i: number) => <Callout key={i} tone="warn">Question: <span data-ai-text>{String(q)}</span></Callout>)}
          {(r.mismatches ?? []).map((m: Rec, i: number) => <Callout key={i} tone="warn">The AI read this as {String(m.model)}; your confirmed value {String(m.intent)} is used.</Callout>)}
          <h3 className="font-semibold">Affected locations ({(r.items ?? []).length})</h3>
          <div className="grid gap-2">
            {(r.items ?? []).map((x: Rec) => (
              <div key={x.ref} data-testid="draft-item" data-status={x.status} data-ref={x.ref} className="rounded-md border border-border p-3 grid gap-1">
                <p className="text-sm flex flex-wrap items-center gap-2"><span className="rounded-full border border-border px-2 text-xs">{ITEM_PHRASE[x.status] ?? x.status}</span><Where x={x} /></p>
                <p className="text-sm text-muted"><span data-ai-text>{x.rationale}</span>{x.reason ? ` - checked by the server: ${p(x.reason)}` : ""}</p>
                {carriedWords(x) && <Callout tone="warn" testid="draft-item-carried"><span data-disposition={x.disposition}>{carriedWords(x)}</span>{x.carried_from?.model_now ? <Tech> (the new AI answer said: {String(x.carried_from.model_now.action)})</Tech> : null}</Callout>}
                <Tech><Muted>model said: {x.model_action}</Muted></Tech>
                {x.status === "PROPOSED" && x.proposal_edit && (
                  <p className="text-sm">Edit: <strong>{String(x.proposal_edit.expected_current)} → {String(x.proposal_edit.proposed)}</strong><Tech> <code>{x.proposal_edit.path}</code></Tech></p>
                )}
                {x.citation?.quotes_redacted && <Muted testid="draft-citation-hidden">The cited source text is hidden for your role. The file and location it points to are shown; the server has checked the citation.</Muted>}
                {x.citation?.quote && <blockquote data-testid="draft-citation" className="border-l-2 border-border pl-3 text-sm text-muted"><Quote size={12} aria-hidden="true" className="inline mr-1" />{x.citation.quote}</blockquote>}
              </div>
            ))}
          </div>
          {d && ids.draft && <GapPanel draftId={ids.draft} arts={arts as string[]} />}
          <Lists r={r} />
          {ids.draft && <FeedbackOnDraft draftId={ids.draft} />}
        </>
      )}
    </StageCard>
  );
}

function Lists({ r }: { r: Rec }) {
  const p = usePlain();
  const { mode } = useSession();
  const block = (title: string, items: unknown[] | undefined, fmt: (i: Rec) => React.ReactNode, testid: string) =>
    items && items.length ? (
      <div key={testid} className="grid gap-1">
        <h3 className="font-semibold">{title} ({items.length})</h3>
        {items.map((i, k) => <Callout key={k} tone="warn" testid={testid}>{fmt(i as Rec)}</Callout>)}
      </div>
    ) : null;
  const open = Object.entries(((r.open_items ?? []) as Rec[]).reduce<Record<string, number>>((m, i) => { const k = `${humanize(i.type ?? "finding")}: ${String(i.reason)}`; m[k] = (m[k] ?? 0) + 1; return m; }, {}));
  return (
    <>
      {block("Not classified by the AI (unaddressed)", r.unaddressed, (i) => <>{basename(i.logical_path)} {i.location ?? ""}: {i.text_redacted ? <em data-testid="source-text-hidden">source text hidden for your role</em> : <span data-verbatim>{String(i.text)}</span>}{carriedWords(i) ? <> [{carriedWords(i)}]</> : null}</>, "draft-unaddressed")}
      {block("Unsupported or rejected citations", [...(r.unsupported ?? []), ...(r.rejected ?? [])], (i) => (mode === "technical" ? JSON.stringify(i) : `${basename(i.logical_path || i.artifact_path) || "A citation"}: ${p(i.reason ?? i.status ?? "the citation could not be checked")}`), "draft-unsupported")}
      {block("Not covered", r.not_covered, (i) => `${i.logical_path ? basename(i.logical_path) : "Your request"} ${p(i.reason ?? "")}`, "draft-not-covered")}
      {block("Stored findings kept open and unchanged (grouped; each stays open)", open, (i) => { const [k, n] = i as unknown as [string, number]; return `${p(k)}  x${n}`; }, "draft-open-item")}
    </>
  );
}

// ---- 2b. Preview before apply ------------------------------------------------
function Group({ title, testid, note, rows }: { title: string; testid: string; note: string; rows: React.ReactNode[] }) {
  return (
    <div data-testid={testid} data-count={rows.length} className="rounded-md border border-border p-3 grid gap-1">
      <h4 className="font-semibold">{title} ({rows.length})</h4>
      <Muted>{note}</Muted>
      {rows.length ? <ul className="list-disc pl-5 text-sm grid gap-1">{rows.map((x, i) => <li key={i}>{x}</li>)}</ul> : <Muted>none</Muted>}
    </div>
  );
}

export function PreviewStage() {
  const { data, ws } = useFlow();
  const p = usePlain();
  const d = data;
  const r = draftResult(d?.draft ?? null);
  if (!d || !r) return null;
  const impact = d.impact && isDone(d.impact) ? (d.impact.execution_result ?? {}) : null;
  const simRes = d.simulation && isDone(d.simulation) ? (d.simulation.execution_result ?? {}) : null;
  const cov = (simRes?.coverage ?? {}) as Rec;
  const items = (r.items ?? []) as Rec[];
  const edits = (r.edits ?? []) as Rec[];
  const sf = (d.intent?.structured_form ?? {}) as Rec;
  const quote = (x: Rec) => (x.citation?.quote ? <blockquote className="border-l-2 border-border pl-3 text-muted">{x.citation.quote}</blockquote> : x.citation?.quotes_redacted ? <p className="text-muted text-sm">Cited source text hidden for your role.</p> : null);
  const link = (x: Rec) => (x.candidate_ids ?? []).slice(0, 1).map((id: string) => <a key={id} className="text-primary underline ml-1" href={`#/ws/${encodeURIComponent(ws)}/explorer?node=${encodeURIComponent(id)}`}>(rule in the Explorer)</a>);
  const evid = items.filter((x) => x.status === "PROPOSED" || x.status === "NO_OP");
  const confirm = items.filter((x) => ["AMBIGUITY", "MANUAL_CHANGE", "UNSUPPORTED"].includes(x.status));
  const unchanged = items.filter((x) => x.status === "MANUAL_CHANGE" || x.status === "PROPOSED");
  const uncovered: string[] = [
    ...((r.unaddressed ?? []) as Rec[]).map((u) => `${basename(u.logical_path)} ${u.location ?? ""}: not classified by the AI`),
    ...((r.not_covered ?? []) as Rec[]).map((n) => `${n.logical_path || n.artifact_id ? basename(n.logical_path || n.artifact_id) : "Your request"}: ${p(n.reason)}`),
    ...(impact && ((impact.unknown ?? []) as Rec[]).length ? [`${((impact.unknown ?? []) as Rec[]).length} related rule(s): UNKNOWN - no confirmed relation evidence (unknown is not "unaffected"; the full list is in stage 3)`] : []),
    ...(impact ? Object.entries(((impact.excluded ?? []) as Rec[]).reduce<Record<string, number>>((m, u) => { const k = p(u.reason); m[k] = (m[k] ?? 0) + 1; return m; }, {})).map(([why, n]) => `${n} related rule(s) excluded - ${why}`) : []),
  ];
  const gapOpen = (d.gaps?.gaps ?? []).filter((g) => g.state !== "confirmed").length;
  const hSim = simRes?.harbor as Rec | undefined;
  const prevDone = !!d.proposal;
  const prevSummary = (
    <>
      {edits.length ? edits.map((e, i) => <span key={i}>{i ? "; " : ""}{basename(e.logical_path)}: {valueText(e.old, sf.unit, sf.currency)} <span aria-hidden="true">→</span><span className="sr-only">to</span> {valueText(e.new, sf.unit, sf.currency)}</span>) : "No automatic edit was proposed"}
      . {uncovered.length} item(s) not covered or unknown (unknown is not unaffected), {gapOpen} open point(s){simRes && hSim ? `, simulation: ${hSim.changed_count} of ${hSim.transactions} sample cases change` : ", no behaviour simulation"}.
    </>
  );
  return (
    <StageCard title="Preview before apply" testid="flow-preview" done={prevDone} summary={prevSummary}>
      <Muted>Nothing below is applied. Each line says who produced it: AI (a reading of the evidence), SERVER (deterministic, checked against stored state), NOT RUN / MISSING.</Muted>
      <h3 className="font-semibold">Current vs proposed</h3>
      {edits.length ? (
        <ul className="grid gap-1 text-sm">
          {edits.map((e, i) => (
            <li key={i} className="flex flex-wrap items-center gap-2">
              <span className="rounded-full border border-border px-2 text-xs">SERVER</span>
              <span>{basename(e.logical_path)}:</span>
              <strong>{valueText(e.old, sf.unit, sf.currency)} → {valueText(e.new, sf.unit, sf.currency)}</strong>
              <Tech><code>{e.proposal_edit?.path}</code></Tech>
            </li>
          ))}
        </ul>
      ) : <Muted>No automatic YAML edit was proposed (everything is manual, ambiguous or unaddressed).</Muted>}
      <h3 className="font-semibold">What the AI concluded</h3>
      <p className="text-sm"><span className="rounded-full border border-border px-2 text-xs mr-2">AI</span><span data-ai-text>{r.summary ?? ""}</span></p>
      <h3 className="font-semibold">Related documents and sources</h3>
      <div data-testid="preview-docs" className="grid gap-3">
        <Muted>Links between rules are proposed identity relations from the graph, not proven dependencies. A document appears under "impacted" only where a citation was re-checked by the server.</Muted>
        <Group title="Impacted, with evidence" testid="preview-docs-evidence" note="The server re-checked the quoted text in the stored source."
          rows={evid.map((x) => <>{<Where x={x} />} - {x.status === "PROPOSED" ? "will change only after you apply" : "already at the new value"}{quote(x)}{link(x)}</>)} />
        <Group title="Needs your confirmation" testid="preview-docs-confirm" note="Ambiguous, manual or unsupported items and open points: you decide; nothing is applied for them."
          rows={[...confirm.map((x) => <><Where x={x} /> - {ITEM_PHRASE[x.status] ?? x.status}{x.reason ? `: ${p(x.reason)}` : ""}{carriedWords(x) ? ` [${carriedWords(x)}]` : ""}{quote(x)}{link(x)}</>), ...(gapOpen ? [<>{gapOpen} open point(s) in the list above</>] : [])]} />
        <Group title="Not changed yet" testid="preview-docs-unchanged" note="Nothing here is changed until a person applies an approved proposal. A proposal edits one value; text and workbook edits stay manual."
          rows={unchanged.map((x) => <><Where x={x} />{x.status === "PROPOSED" ? " - changes only after apply and approval" : " - stays as it is: a person edits it"}{link(x)}</>)} />
        <Group title="Not covered / unknown" testid="preview-docs-uncovered" note="Sources or rules nothing here could examine. UNKNOWN is not 'unaffected'." rows={uncovered} />
      </div>
      {impact
        ? <p className="text-sm"><span className="rounded-full border border-border px-2 text-xs mr-2">SERVER</span>Direct {(impact.direct ?? []).length}, indirect {(impact.indirect ?? []).length}, unknown {(impact.unknown ?? []).length}, excluded {(impact.excluded ?? []).length}. <a className="text-primary underline" href={`#/ws/${encodeURIComponent(ws)}/explorer`}>Open the Explorer</a> to see the rules and their evidence.</p>
        : <Muted>Impact analysis not run yet (step 3).</Muted>}
      <h3 className="font-semibold">Behaviour before and after</h3>
      <p data-testid="preview-simulation" data-state={cov.submitted ? "RAN" : "UNAVAILABLE"} className="text-sm">
        <span className="rounded-full border border-border px-2 text-xs mr-2">{cov.submitted ? "SERVER" : "NOT RUN"}</span>
        {cov.submitted
          ? `Harbor itself was run over ${cov.submitted} public requisitions${hSim ? `: ${hSim.changed_count} change their route (details in step 3).` : "."}`
          : "No behaviour simulation exists for this change (only the scalar thresholds of the Harbor approval config can be simulated). The AI analysis above does NOT replace it."}
      </p>
      <h3 className="font-semibold">Still open</h3>
      <p data-testid="preview-open" className="text-sm">{gapOpen} open point(s); {(r.not_covered ?? []).length} source(s) not covered (for example code and emergency); {(r.unaddressed ?? []).length} evidence item(s) not classified.</p>
      <TechField label="draft hash" value={r.draft_hash ?? ""} />
    </StageCard>
  );
}

