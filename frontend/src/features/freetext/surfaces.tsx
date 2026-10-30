// The four free-text surfaces: what the preview shows, how the person may adjust it, and the existing guarded action that follows.
import { failureAdvice } from "@/lib/reasons";
import * as React from "react";
import { Quote } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Tech, TextArea, TextInput, Select } from "@/components/bits";
import { humanize, newId, valueText } from "@/lib/fmt";
import { hashFor } from "@/lib/route";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { EquivalentStep } from "@/components/EquivalentStep";
import type { Rec, Understanding, Job } from "@/features/changes/types";
import { isTerminal, jobState } from "@/features/changes/types";
import type { Surface } from "./PurposeChat";

const q = encodeURIComponent;

const Row = ({ label, children, testid }: { label: string; children: React.ReactNode; testid?: string }) => (
  <>
    <dt className="text-muted">{label}</dt>
    <dd data-testid={testid}>{children}</dd>
  </>
);
const Facts = ({ children }: { children: React.ReactNode }) => <dl className="grid grid-cols-[minmax(8rem,12rem)_1fr] gap-x-3 gap-y-1 text-sm">{children}</dl>;
const From = ({ src }: { src?: Rec }) => (src ? <span className="block text-xs text-muted">{src.quote ? `from your words: "${src.quote}"` : `from ${src.from}`}</span> : null);

export const REACTION_WORDS: Record<string, string> = {
  exclude: "Leave this out of the change", ambiguous: "Needs clarification first", manual: "A person edits this by hand", keep: "This is right", question: "Needs an answer first",
};

/** Waits for the AI answer job a person started from a confirmed question: shown as stored, polled a bounded number of times. */
function AnswerJob({ ws, jobId }: { ws: string; jobId: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const [job, setJob] = React.useState<Job | null>(null);
  const polls = React.useRef(0);
  const load = React.useCallback(async () => { try { setJob(await api<Job>("GET", `/v2/jobs/${q(jobId)}?workspace_id=${q(ws)}`)); } catch { /* shown as waiting */ } }, [ws, jobId]);
  React.useEffect(() => { void load(); }, [load]);
  React.useEffect(() => {
    if ((job && isTerminal(job)) || polls.current >= 60) return;
    const t = window.setTimeout(() => { polls.current += 1; void load(); }, 2500);
    return () => window.clearTimeout(t);
  }, [job, load]);
  if (!job) return <Muted>Looking for the answer...</Muted>;
  const st = jobState(job);
  const r = (job.execution_result ?? {}) as Rec;
  return (
    <div data-testid="ask-answer" data-state={st} className="grid gap-2">
      {job.status === "PENDING_APPROVAL" && (
        <>
          <Muted>In this shared trial workspace a different person has to allow this AI run before it starts.</Muted>
          {s.can("source.import") && <Button data-testid="ask-approve-job" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${q(jobId)}/approve`, { workspace_id: ws }), { stale: true })) await load(); }}>Allow this AI run</Button>}
        </>
      )}
      {!isTerminal(job) && job.status !== "PENDING_APPROVAL" && <Muted>The AI is answering. This page checks by itself.</Muted>}
      {st === "FAILED" && <Callout tone="danger">The answer could not be produced. {failureAdvice(job.execution_reason).what} Nothing was changed.</Callout>}
      {st === "COMPLETED" && (
        <>
          <p className="text-sm"><span className="rounded-full border border-border px-2 text-xs mr-2">{String(r.status ?? "").toLowerCase() || "answer"}</span><strong data-testid="ask-answer-text" data-ai-text>{r.answer ?? r.question_for_human ?? "No answer was found in these files."}</strong></p>
          {((r.evidence_spans ?? []) as Rec[]).map((sp, i) => (
            <blockquote key={i} data-testid="ask-citation" className="border-l-2 border-border pl-3 text-sm text-muted"><Quote size={12} aria-hidden="true" className="inline mr-1" />{sp.quotes_redacted ? <em data-testid="citation-hidden">Cited source text hidden for your role.</em> : sp.quote}
              <span className="block text-xs">{String(sp.artifact_path ?? "").split("/").pop()}<Tech> @ {sp.source_revision_id}</Tech></span></blockquote>
          ))}
          <Muted>A claim the stored files do not support is withheld. This answer is not saved as knowledge.</Muted>
        </>
      )}
    </div>
  );
}

export function askSurface(ws: string): Surface {
  return {
    purpose: "knowledge_qa", prefix: "ask", heading: "Ask about your documents",
    intro: "Write your question the way you would ask a colleague. The AI picks the files to search and shows you before anything runs.",
    placeholder: "For example: what is the manager approval limit for standard orders?", startLabel: "Send",
    preview: (u: Understanding) => {
      const f = (u.fields ?? {}) as Rec;
      return (
        <Facts>
          <Row label="Question" testid="ask-field-question"><span data-ai-text>{f.question || <span className="text-muted">not known yet</span>}</span></Row>
          <Row label="Files to search" testid="ask-field-files">{(f.files ?? []).length ? (f.files as string[]).join(", ") : <span className="text-muted">none chosen yet</span>}</Row>
        </Facts>
      );
    },
    edit: ({ u, save, pending }) => <AskEdit ws={ws} u={u} save={save} pending={pending} />,
    action: ({ chat, outcome, markDone }) => <AskAction ws={ws} chat={chat} outcome={outcome} markDone={markDone} />,
  };
}

function AskEdit({ ws, u, save, pending }: { ws: string; u: Understanding; save: (f: Rec) => Promise<void>; pending: boolean }) {
  const f = (u.fields ?? {}) as Rec;
  const [question, setQuestion] = React.useState<string>(f.question ?? "");
  const [picked, setPicked] = React.useState<string[]>(f.artifact_ids ?? []);
  const [files, setFiles] = React.useState<Rec[]>([]);
  React.useEffect(() => {
    let live = true;
    (async () => {
      try {
        const head = await api<Rec>("GET", `/v2/workspaces/${q(ws)}/head`);
        if (head.source_revision_id) {
          const v = await api<Rec>("GET", `/v2/sources/revisions/${q(head.source_revision_id)}?workspace_id=${q(ws)}`);
          if (live) setFiles((v.artifacts ?? []) as Rec[]);
        }
      } catch { /* the preview can still be confirmed as it is */ }
    })();
    return () => { live = false; };
  }, [ws]);
  return (
    <div className="grid gap-3">
      <Field label="Question"><TextInput data-testid="ask-edit-question" value={question} onChange={(e) => setQuestion(e.target.value)} /></Field>
      <fieldset className="grid gap-1"><legend className="text-sm font-medium">Files to search</legend>
        {files.map((a) => (
          <label key={a.artifact_id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={picked.includes(a.artifact_id)}
            onChange={(e) => setPicked((p) => (e.target.checked ? [...p, a.artifact_id] : p.filter((x) => x !== a.artifact_id)))} />{String(a.logical_path).split("/").pop()}</label>
        ))}
      </fieldset>
      <div><Button data-testid="ask-edit-apply" disabled={pending} onClick={() => save({ question, artifact_ids: picked })}>Check my changes</Button></div>
    </div>
  );
}

function AskAction({ ws, outcome, markDone }: { ws: string; chat: unknown; outcome: Rec; markDone: (ref: string) => Promise<void> }) {
  const { run, pending } = useAction();
  const s = useSession();
  const ref = outcome.executed?.ref as string | undefined;
  return (
    <div className="grid gap-2">
      <p className="text-sm">Question: <span data-ai-text>{outcome.question}</span> - searching {(outcome.files ?? []).join(", ")}.</p>
      {!ref && (
        <div>
          <Button variant="primary" data-testid="ask-run" title={aiWhy(s, "gap.answer")} disabled={pending || !s.aiKind("gap.answer")} onClick={async () => {
            const id = newId("ui-ask");
            const r = await run(() => api<Rec>("POST", "/v2/jobs", { workspace_id: ws, kind: "gap.answer", job_id: id, payload: { question: outcome.question, question_id: id, artifact_ids: outcome.artifact_ids } }), { stale: true });
            if (r) await markDone(r.job_id ?? id);
          }}>Ask the AI to answer from these files</Button><EquivalentStep kind="gap.answer" to="ask" />
        </div>
      )}
      {ref && <AnswerJob ws={ws} jobId={ref} />}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------- reply to an open point
export function gapReplySurface(ws: string, onRecorded: () => Promise<void>): Surface {
  return {
    purpose: "gap_reply", prefix: "gr", heading: "Reply to this open point in your own words",
    intro: "The AI shows how your reply will be recorded (a comment or a decision, a fact or a wish) before anything is saved.",
    placeholder: "Write your reply", startLabel: "Send",
    preview: (u: Understanding) => {
      const f = (u.fields ?? {}) as Rec;
      return (
        <Facts>
          <Row label="Will be recorded as" testid="gr-field-kind">{f.kind === "decision" ? "A decision, with your name (the draft becomes out of date)" : "A comment (not authoritative)"}</Row>
          <Row label="Your words (kept exactly as typed)" testid="gr-field-typed"><span data-verbatim>{f.typed_text || <span className="text-muted">not known yet</span>}</span></Row>
          <Row label="Files you named" testid="gr-field-files">
            {((f.files ?? []) as Rec[]).length === 0
              ? <span className="text-muted">No file name in your words. The decision applies to this open point only; no file is added or left out by it.</span>
              : <ul className="grid gap-1">{((f.files ?? []) as Rec[]).map((x) => (
                  <li key={x.artifact_id} data-testid="gr-file" data-role={x.role}>
                    <strong>{x.name}</strong> - {x.role === "in_scope" ? "in scope" : x.role === "not_changed" ? "not changed by this request (it is NOT resolved: it stays open)" : "named, role not stated"}
                    <Tech><code> {x.logical_path} {x.artifact_id}</code></Tech>
                  </li>))}</ul>}
          </Row>
          <Row label="AI wording (added for reading; it does not replace your words)" testid="gr-field-answer"><span data-ai-text>{f.answer || <span className="text-muted">not known yet</span>}</span><From src={u.sources?.answer as Rec} /></Row>
          <Row label="A fact or a wish" testid="gr-field-wish">{f.wish_or_fact === "wish" ? "A wish: how you want it to work. It is not recorded as how things work today." : f.wish_or_fact === "fact" ? "A fact: how things work today." : <span className="text-muted">not known yet</span>}</Row>
          {f.rationale && <Row label="Why">{f.rationale}</Row>}
        </Facts>
      );
    },
    edit: ({ u, save, pending }) => <GapEdit u={u} save={save} pending={pending} />,
    action: ({ chat, outcome, markDone }) => <GapAction ws={ws} cid={chat.conversation_id} outcome={outcome} markDone={markDone} onRecorded={onRecorded} />,
  };
}

function GapEdit({ u, save, pending }: { u: Understanding; save: (f: Rec) => Promise<void>; pending: boolean }) {
  const f = (u.fields ?? {}) as Rec;
  const [v, setV] = React.useState({ answer: String(f.answer ?? ""), kind: String(f.kind ?? "comment"), wf: String(f.wish_or_fact ?? ""), why: String(f.rationale ?? "") });
  return (
    <div className="grid gap-3">
      <Field label="Answer"><TextArea data-testid="gr-edit-answer" rows={3} value={v.answer} onChange={(e) => setV({ ...v, answer: e.target.value })} /></Field>
      <Field label="Record it as">
        <Select data-testid="gr-edit-kind" value={v.kind} onChange={(e) => setV({ ...v, kind: e.target.value })}>
          <option value="comment">A comment (not authoritative)</option><option value="decision">A decision (needs permission to confirm)</option>
        </Select>
      </Field>
      <Field label="Is this a fact or a wish?">
        <Select data-testid="gr-edit-wish" value={v.wf} onChange={(e) => setV({ ...v, wf: e.target.value })}>
          <option value="">Choose</option><option value="fact">A fact: how it works today</option><option value="wish">A wish: how I want it to work</option>
        </Select>
      </Field>
      <Field label="Why (optional)"><TextInput data-testid="gr-edit-why" value={v.why} onChange={(e) => setV({ ...v, why: e.target.value })} /></Field>
      <div><Button data-testid="gr-edit-apply" disabled={pending} onClick={() => save({ answer: v.answer, kind: v.kind, wish_or_fact: v.wf || null, rationale: v.why || null })}>Check my changes</Button></div>
    </div>
  );
}

function GapAction({ ws, cid, outcome, markDone, onRecorded }: { ws: string; cid: string; outcome: Rec; markDone: (ref: string) => Promise<void>; onRecorded: () => Promise<void> }) {
  const { run, pending } = useAction();
  const notify = useNotify();
  if (outcome.executed) return <Muted>Recorded as a {outcome.kind === "decision" ? "decision" : "comment"}.</Muted>;
  const decision = outcome.kind === "decision";
  return (
    <div className="grid gap-2">
      <p className="text-sm">{decision ? "Recording a decision makes this draft out of date: ask for a new draft afterwards." : "A comment is saved with your name. It does not change the draft."}</p>
      <div>
        <Button variant="primary" data-testid="gr-run" disabled={pending} onClick={async () => {
          const r = decision
            ? await run(() => api("POST", "/v2/change/gap/confirm", { workspace_id: ws, draft_job_id: outcome.draft_job_id, gap_key: outcome.gap_key, chat_id: cid, rationale: outcome.rationale ?? "" }), { stale: true })
            : await run(() => api("POST", "/v2/hitl/answer", { workspace_id: ws, question_id: outcome.gap_id, answer: outcome.answer, idempotency_key: `chat-${cid}-${outcome.version}`, expected_version: null }), { stale: true });
          if (r) { notify.status(decision ? "Decision recorded by you. This draft is now out of date." : "Comment recorded (not authoritative)."); await markDone(decision ? String((r as Rec).decision_id ?? "") : "comment"); await onRecorded(); }
        }}>{decision ? "Record this decision" : "Save this comment"}</Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------- reply to a queue question
/** A reply to one question of the work queue: how it will be recorded (comment or decision, fact or wish, which rule) BEFORE anything is saved. */
export function questionReplySurface(ws: string, claims: Rec[], version: number | null, onRecorded: () => Promise<void>): Surface {
  const label = (id: string | null | undefined) => {
    const c = claims.find((x) => x.candidate_id === id);
    return c ? `${humanize(c.predicate)} = ${valueText(c.value, c.unit, c.unit)}${c.logical_path ? ` (${String(c.logical_path).split("/").pop()})` : ""}` : "the chosen rule";
  };
  return {
    purpose: "question_reply", prefix: "qr", heading: "Reply in your own words",
    intro: "Ripple shows how your answer will be recorded (a comment or a decision, a fact or a wish, and which rule) before anything is saved. An answer alone is never knowledge.",
    placeholder: "Reply in your own words", startLabel: "Send",
    preview: (u: Understanding) => {
      const f = (u.fields ?? {}) as Rec;
      return (
        <Facts>
          <Row label="Will be recorded as" testid="qr-field-kind">{f.kind === "decision" ? "A decision with your name that confirms one rule. It confirms knowledge only; no source file is changed." : "A comment (not authoritative: it is not knowledge until a person with authority confirms a rule)"}</Row>
          {f.kind === "decision" && <Row label="Rule being confirmed" testid="qr-field-claim">{label(f.claim_id)}</Row>}
          <Row label="Answer" testid="qr-field-answer"><span data-ai-text>{f.answer || <span className="text-muted">not known yet</span>}</span><From src={u.sources?.answer as Rec} /></Row>
          <Row label="A fact or a wish" testid="qr-field-wish">{f.wish_or_fact === "wish" ? "A wish: how you want it to work. It is not recorded as how things work today." : f.wish_or_fact === "fact" ? "A fact: how things work today." : <span className="text-muted">not known yet</span>}</Row>
          {f.forced_comment && <Row label="Good to know" testid="qr-field-forced">A wish cannot be confirmed as knowledge, so it is saved as a comment.</Row>}
          {f.rationale && <Row label="Why you are confirming this" testid="qr-field-why">{f.rationale}</Row>}
          {f.asks_source_change && <Row label="Next step" testid="qr-field-handoff">Changing the source itself is a change request. Confirming knowledge never edits a file.</Row>}
        </Facts>
      );
    },
    edit: ({ u, save, pending }) => <QuestionEdit u={u} claims={claims} save={save} pending={pending} />,
    action: ({ chat, outcome, markDone }) => <QuestionAction ws={ws} cid={chat.conversation_id} version={version} outcome={outcome} markDone={markDone} onRecorded={onRecorded} />,
  };
}

function QuestionEdit({ u, claims, save, pending }: { u: Understanding; claims: Rec[]; save: (f: Rec) => Promise<void>; pending: boolean }) {
  const f = (u.fields ?? {}) as Rec;
  const [v, setV] = React.useState({ answer: String(f.answer ?? ""), kind: String(f.kind ?? "comment"), claim: String(f.claim_id ?? ""), wf: String(f.wish_or_fact ?? ""), why: String(f.rationale ?? "") });
  return (
    <div className="grid gap-3">
      <Field label="Answer"><TextArea data-testid="qr-edit-answer" rows={3} value={v.answer} onChange={(e) => setV({ ...v, answer: e.target.value })} /></Field>
      <Field label="Record it as">
        <Select data-testid="qr-edit-kind" value={v.kind} onChange={(e) => setV({ ...v, kind: e.target.value })}>
          <option value="comment">A comment (not authoritative)</option><option value="decision">A decision (needs permission to confirm)</option>
        </Select>
      </Field>
      {v.kind === "decision" && (
        <Field label="Which rule do you confirm?">
          <Select data-testid="qr-edit-claim" value={v.claim} onChange={(e) => setV({ ...v, claim: e.target.value })}>
            <option value="">Choose</option>{claims.map((c) => <option key={c.candidate_id as string} value={c.candidate_id as string}>{humanize(c.predicate)} = {valueText(c.value, c.unit, c.unit)}</option>)}
          </Select>
        </Field>
      )}
      <Field label="Is this a fact or a wish?">
        <Select data-testid="qr-edit-wish" value={v.wf} onChange={(e) => setV({ ...v, wf: e.target.value })}>
          <option value="">Choose</option><option value="fact">A fact: how it works today</option><option value="wish">A wish: how I want it to work</option>
        </Select>
      </Field>
      <Field label="Why you are confirming this (optional)"><TextInput data-testid="qr-edit-why" value={v.why} onChange={(e) => setV({ ...v, why: e.target.value })} /></Field>
      <div><Button data-testid="qr-edit-apply" disabled={pending} onClick={() => save({ answer: v.answer, kind: v.kind, claim_id: v.claim || null, wish_or_fact: v.wf || null, rationale: v.why || null })}>Check my changes</Button></div>
    </div>
  );
}

function QuestionAction({ ws, cid, version, outcome, markDone, onRecorded }: { ws: string; cid: string; version: number | null; outcome: Rec; markDone: (ref: string) => Promise<void>; onRecorded: () => Promise<void> }) {
  const { run, pending } = useAction();
  const notify = useNotify();
  const s = useSession();
  if (outcome.executed) return <Muted>Recorded as a {outcome.kind === "decision" ? "decision" : "comment"}.{outcome.handoff_change ? " To change the source itself, start a change request in Changes." : ""}</Muted>;
  const decision = outcome.kind === "decision";
  return (
    <div className="grid gap-2">
      <p className="text-sm">{decision ? "Recording a decision confirms this rule as knowledge, with your name. It does not change any source file." : "A comment is saved with your name. It is not knowledge until a person with authority confirms a rule."}</p>
      {outcome.handoff_change && <p className="text-sm" data-testid="qr-handoff">To change the source itself, <a className="text-primary underline" href={hashFor(ws, "changes")}>start a change request</a>: it goes through proposal, approval and apply.</p>}
      <div>
        <Button variant="primary" data-testid="qr-run" disabled={pending || (decision && !s.can("knowledge.confirm"))} onClick={async () => {
          const r = decision
            ? await run(() => api("POST", "/v2/knowledge/confirm", { workspace_id: ws, claim_id: outcome.claim_id, rationale: outcome.rationale || outcome.answer }), { stale: true })
            : await run(() => api("POST", "/v2/hitl/answer", { workspace_id: ws, question_id: outcome.question_id, answer: outcome.answer, idempotency_key: `chat-${cid}`, expected_version: version }), { stale: true });
          if (r) { notify.status(decision ? "Decision recorded by you. The source file is unchanged." : "Comment recorded (not authoritative)."); await markDone(decision ? String((r as Rec).decision_id ?? "") : "comment"); await onRecorded(); }
        }}>{decision ? "Record this decision" : "Save this comment"}</Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------- draft feedback
export function feedbackSurface(ws: string, gaps: Rec[], onRecorded: () => Promise<void>): Surface {
  return {
    purpose: "draft_feedback", prefix: "fb", heading: "Give feedback on this draft in your own words",
    intro: "Say what is wrong or unclear about any part of the draft. The AI matches it to the items of the draft and shows each reaction before anything is saved.",
    placeholder: "For example: the workbook row is wrong, do not change it", startLabel: "Send",
    preview: (u: Understanding) => {
      const f = (u.fields ?? {}) as Rec;
      const reactions = (f.reactions ?? []) as Rec[];
      return (
        <div className="grid gap-2">
          {reactions.length === 0 && <Muted>No item of the draft is matched yet.</Muted>}
          <ul className="grid gap-2">
            {reactions.map((r, i) => (
              <li key={i} data-testid="fb-reaction" data-reaction={r.reaction} className="rounded-md border border-border bg-surface p-2 text-sm grid gap-1">
                <span><strong>{r.item}</strong> - {REACTION_WORDS[r.reaction] ?? humanize(r.reaction)}</span>
                {r.note && <span data-ai-text>{r.note}</span>}
                <span className="text-xs text-muted">{r.traced && r.quote ? `from your words: "${r.quote}"` : "the AI's wording, not a quote of yours"}</span>
                {!gaps.some((g) => g.key === `ref:${r.ref}`) && <span className="text-xs text-muted">This item has no open point, so the reaction stays in this conversation only.</span>}
              </li>
            ))}
          </ul>
          {f.clarification && <Muted>Clarification: <span data-ai-text>{f.clarification}</span></Muted>}
        </div>
      );
    },
    edit: ({ u, save, pending }) => <FeedbackEdit u={u} save={save} pending={pending} />,
    action: ({ chat, outcome, markDone }) => <FeedbackAction ws={ws} cid={chat.conversation_id} gaps={gaps} outcome={outcome} markDone={markDone} onRecorded={onRecorded} />,
  };
}

function FeedbackEdit({ u, save, pending }: { u: Understanding; save: (f: Rec) => Promise<void>; pending: boolean }) {
  const f = (u.fields ?? {}) as Rec;
  const [rs, setRs] = React.useState<Rec[]>(((f.reactions ?? []) as Rec[]).map((r) => ({ ref: r.ref, item: r.item, reaction: r.reaction, note: r.note ?? "" })));
  const [clar, setClar] = React.useState(String(f.clarification ?? ""));
  return (
    <div className="grid gap-3">
      {rs.map((r, i) => (
        <div key={i} className="grid gap-1 rounded-md border border-border p-2">
          <strong className="text-sm">{r.item}</strong>
          <Select aria-label={`Reaction for ${r.item}`} value={r.reaction} onChange={(e) => setRs(rs.map((x, j) => (j === i ? { ...x, reaction: e.target.value } : x)))}>
            {Object.entries(REACTION_WORDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </Select>
          <TextInput aria-label={`Note for ${r.item}`} value={r.note} onChange={(e) => setRs(rs.map((x, j) => (j === i ? { ...x, note: e.target.value } : x)))} />
          <Button onClick={() => setRs(rs.filter((_, j) => j !== i))}>Remove this reaction</Button>
        </div>
      ))}
      <Field label="Anything else about the draft (optional)"><TextInput value={clar} onChange={(e) => setClar(e.target.value)} /></Field>
      <div><Button data-testid="fb-edit-apply" disabled={pending} onClick={() => save({ reactions: rs, clarification: clar || null })}>Check my changes</Button></div>
    </div>
  );
}

function FeedbackAction({ ws, cid, gaps, outcome, markDone, onRecorded }: { ws: string; cid: string; gaps: Rec[]; outcome: Rec; markDone: (ref: string) => Promise<void>; onRecorded: () => Promise<void> }) {
  const { run, pending } = useAction();
  const notify = useNotify();
  const s = useSession();
  const reactions = (outcome.reactions ?? []) as Rec[];
  const withGap = reactions.map((r) => ({ r, gap: gaps.find((g) => g.key === `ref:${r.ref}`) })).filter((x) => x.gap);
  if (outcome.executed) return <Muted>{withGap.length} comment(s) recorded on the open points. {reactions.length - withGap.length} reaction(s) stay in this conversation only.</Muted>;
  return (
    <div className="grid gap-2">
      <p className="text-sm">{withGap.length} of {reactions.length} reaction(s) belong to an open point and will be saved there as comments (not authoritative). The rest stay in this conversation.</p>
      <p className="text-sm text-muted">Comments are saved as comments only: the next draft does NOT use them. To make the next draft take a point into account, a person with confirm rights confirms an answer for that open point (under the point: "Confirm this answer as a decision", or "Reply in chat"). Then "Revise this draft" uses it and shows it as applied. A new draft uses the paid model under your AI permission.</p>
      {s.can("knowledge.answer") && (
        <div>
          <Button variant="primary" data-testid="fb-run" disabled={pending || withGap.length === 0} onClick={async () => {
            const ok = await run(async () => {
              for (const { r, gap } of withGap) {
                await api("POST", "/v2/hitl/answer", { workspace_id: ws, question_id: gap!.gap_id, answer: `Feedback - ${REACTION_WORDS[r.reaction] ?? r.reaction}: ${r.note || r.item}`.slice(0, 600), idempotency_key: `chat-${cid}-${r.ref}`, expected_version: null });
              }
              return true;
            }, { stale: true });
            if (ok) { notify.status("Feedback recorded as comments on the open points."); await markDone("comments"); await onRecorded(); }
          }}>Save as comments on the open points</Button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------- what-if
export function whatIfSurface(onRun: (c: string, path: string, value: number) => Promise<string | null>): Surface {
  return {
    purpose: "whatif", prefix: "wi", heading: "Say it in your own words",
    intro: "Write it the way you would say it. Ripple checks it against what your sources say now and asks you if something is missing.",
    placeholder: "For example: what happens if we lower the manager approval threshold to 500,000 JPY for standard orders, keeping emergency orders unchanged?", startLabel: "Send",
    preview: (u: Understanding) => {
      const f = (u.fields ?? {}) as Rec;
      const cur = (f.current ?? []) as Rec[];
      return (
        <Facts>
          <Row label="Rule to try" testid="wi-field-rule">{f.label || <span className="text-muted">not known yet</span>}</Row>
          {f.rule_key && (
            <Row label="What your sources say now" testid="wi-field-current">
              {cur.length ? (
                <span className="grid gap-1">
                  {cur.map((c, i) => <span key={i} data-testid="wi-current-item">{valueText(c.value, c.unit, c.unit)} - {c.scope} - {c.file} - {c.state === "confirmed" ? "confirmed by a person" : "read by the AI, not yet confirmed"}</span>)}
                  {f.sources_disagree && <span className="text-warning" data-testid="wi-disagree">The sources do not agree on this value.</span>}
                </span>
              ) : <span className="text-muted">Not found in the analysed sources. The simulation uses the value in the Harbor configuration file.</span>}
            </Row>
          )}
          <Row label="Value to try" testid="wi-field-value">{f.value === null || f.value === undefined ? <span className="text-muted">not known yet</span> : Number(f.value).toLocaleString("en-US")}<From src={u.sources?.value as Rec} /></Row>
          {f.scope && <Row label="Applies to" testid="wi-field-scope">{f.scope}</Row>}
          {f.unchanged && <Row label="Stays the same" testid="wi-field-unchanged">{f.unchanged}</Row>}
          {f.simulated_by && <Row label="How it is run" testid="wi-field-how">{f.simulated_by}. Nothing is saved as knowledge and no document is edited.</Row>}
          {f.note && <Row label="Good to know" testid="wi-field-note">{f.note}</Row>}
        </Facts>
      );
    },
    edit: ({ u, save, pending }) => <WhatIfEdit u={u} save={save} pending={pending} />,
    action: ({ outcome, markDone }) => <WhatIfAction outcome={outcome} markDone={markDone} onRun={onRun} />,
  };
}

const RULES: [string, string][] = [
  ["routing_rules.standard_manager_threshold_jpy", "Manager approval threshold (JPY)"], ["routing_rules.capex_finance_threshold_jpy", "CAPEX finance threshold (JPY)"],
  ["matching_rules.price_tolerance_fixed_jpy", "Invoice price tolerance, fixed (JPY)"], ["matching_rules.price_tolerance_percent", "Invoice price tolerance, percent"],
  ["matching_rules.payment_term_days", "Payment terms (days)"],
];

function WhatIfEdit({ u, save, pending }: { u: Understanding; save: (f: Rec) => Promise<void>; pending: boolean }) {
  const f = (u.fields ?? {}) as Rec;
  const [rule, setRule] = React.useState<string>(f.rule_key ?? RULES[0][0]);
  const [val, setVal] = React.useState<string>(f.value === null || f.value === undefined ? "" : String(f.value));
  return (
    <div className="grid gap-3">
      <Field label="Rule">
        <Select data-testid="wi-edit-rule" value={rule} onChange={(e) => setRule(e.target.value)}>
          {RULES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </Select>
      </Field>
      <Field label="Value"><TextInput data-testid="wi-edit-value" value={val} onChange={(e) => setVal(e.target.value)} /></Field>
      <div><Button data-testid="wi-edit-apply" disabled={pending} onClick={() => save({ rule_key: rule, value: val.replace(/[ ,]/g, "") === "" ? null : Number(val.replace(/[ ,]/g, "")) })}>Check my changes</Button></div>
    </div>
  );
}

function WhatIfAction({ outcome, markDone, onRun }: { outcome: Rec; markDone: (ref: string) => Promise<void>; onRun: (c: string, path: string, value: number) => Promise<string | null> }) {
  const { pending } = useAction();
  const [busy, setBusy] = React.useState(false);
  if (outcome.executed) return <Muted>The what-if simulation was started. Its result is shown below.</Muted>;
  return (
    <div className="grid gap-2">
      <p className="text-sm">Try {outcome.label}: {Number(outcome.value).toLocaleString("en-US")}. Nothing is changed and no document is edited.</p>
      <div>
        <Button variant="primary" data-testid="wi-run" disabled={pending || busy} onClick={async () => {
          setBusy(true);
          try { const id = await onRun(outcome.case, outcome.path, outcome.value); if (id) await markDone(id); } finally { setBusy(false); }
        }}>Run what-if simulation</Button>
      </div>
    </div>
  );
}
