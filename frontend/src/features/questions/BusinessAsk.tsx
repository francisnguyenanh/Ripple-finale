// Ask about the business knowledge. The answer is stored by the server: opening, reloading, switching between the Business and Technical view or going back never asks a model again.
// Text from the model, the sources and people is rendered as plain text only (React escapes it); nothing here sets HTML.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, StatePill, Tech, TextArea } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useParam } from "@/lib/param";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";
import { RunHold } from "./RunHold";

const q = encodeURIComponent;
const STATUS_WORDS: Record<string, string> = { ANSWERED: "Answered", PARTIAL: "Partly answered", NEEDS_CLARIFICATION: "More detail needed", ABSTAINED: "Not answered" };
const JOB_WORDS: Record<string, string> = { waiting_for_approval: "Waiting to start", waiting_for_slot: "Allowed, waiting for a worker slot", running: "Working on it", failed: "Could not answer", cancelled: "Cancelled" };
const REASON_WORDS: Record<string, string> = {
  authority_revoked: "The person who asked no longer has permission to ask.",
  snapshot_moved: "The business knowledge changed before the answer was made. Ask again to use the latest version.",
  follow_up_not_valid: "The earlier question this one follows up is not available.",
  budget_exceeded: "The question and its context are too large to answer.",
};

function reasonText(reason: string | null): string {
  if (!reason) return "The question could not be answered.";
  const key = Object.keys(REASON_WORDS).find((k) => reason.startsWith(k));
  return key ? REASON_WORDS[key] : "The question could not be answered.";
}

function AnswerCard({ data, mode }: { data: Rec; mode: string }) {
  const a = data.answer as Rec;
  const tech = a.technical as Rec | undefined;
  const pin = (tech?.pin ?? {}) as Rec;
  return (
    <article data-testid="qa-answer" data-status={a.status} data-snapshot={String(pin.snapshot_id ?? "")} data-mode={mode} className="rounded-lg border border-border bg-surface p-4 grid gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <StatePill state={a.status as string} label={STATUS_WORDS[a.status as string] ?? (a.status as string)} testid="qa-status" />
        <span className="text-sm text-muted">{a.view === "observed" ? "What the sources say" : "Accepted business knowledge"}</span>
      </div>
      {a.newer_knowledge_exists && <Callout tone="warn" testid="qa-newer">The business knowledge has changed since this answer. Ask again to use the latest version.</Callout>}
      <p data-testid="qa-text" className="text-base">{a.answer as string}</p>
      {((a.conflicts ?? []) as Rec[]).map((c, i) => (
        <Callout key={i} tone={c.decided ? "info" : "warn"} testid="qa-conflict">The sources disagree: {((c.titles ?? []) as string[]).map((t) => `“${t}”`).join(" and ")}. {c.decided ? `A person decided the business rule (${(c.decided_title as string) ?? "the decided rule"}); the other source still says otherwise until it is corrected.` : "No winner was chosen; a person has to decide which one is the business rule."}</Callout>
      ))}
      {((a.claims ?? []) as Rec[]).length > 0 && (
        <ul className="grid gap-2">
          {(a.claims as Rec[]).map((c, i) => (
            <li key={i} data-testid="qa-claim" data-basis={c.basis} data-origin={(c.origin as string) ?? "model"} className="rounded-md border border-border p-3 grid gap-1">
              <span>{c.text as string}</span>
              {c.applicability === "does_not_apply" && <Muted testid="qa-claim-not-applicable">Checked: this rule does not apply to this case (its condition is not met).</Muted>}
              <Muted>{((c.authority_labels ?? []) as string[]).join(", ")}{((c.sources ?? []) as string[]).length ? ` - ${(c.sources as string[]).join("; ")}` : ""}{c.origin === "server_applicability" ? " - added by the rule check, not written by the AI" : ""}</Muted>
              <Tech><Muted>refs {((c.refs ?? []) as string[]).map((r) => r).join(", ") || "withheld"} - basis <code>{c.basis as string}</code> - origin <code>{(c.origin as string) ?? "model"}</code></Muted></Tech>
            </li>
          ))}
        </ul>
      )}
      {a.withheld_claims > 0 && <Muted testid="qa-withheld">{a.withheld_claims as number} part(s) of this answer rest on source text that your role cannot read.</Muted>}
      {a.implementation_note && <Callout tone="info" testid="qa-implementation">{a.implementation_note as string} The source files and code are changed by a person, not by this product.</Callout>}
      {a.uncertainty && <Muted testid="qa-uncertainty">Not known: {a.uncertainty as string}</Muted>}
      {((a.clarifying_questions ?? []) as string[]).length > 0 && (
        <div data-testid="qa-clarify" data-status={a.status}><p className="text-sm font-medium">{a.status === "PARTIAL" ? "The parts that depend on missing facts stay open. To complete this answer, please say:" : "To answer this, please say:"}</p><ul className="list-disc pl-5 text-sm">{(a.clarifying_questions as string[]).map((t, i) => <li key={i}>{t}</li>)}</ul></div>
      )}
      <Muted testid="qa-retrieval-note">{a.retrieval_note as string}</Muted>
      {tech && (
        <Tech>
          <div data-testid="qa-technical" className="rounded-md border border-border p-3 grid gap-1 text-sm">
            <p>snapshot <code>{String(pin.snapshot_id)}</code> ({String(pin.snapshot_state)}), business revision {String(pin.business_revision)}, source revision <code>{String(pin.source_revision_id)}</code></p>
            <p>retrieval <code>{String((tech.retrieval as Rec)?.method)}</code>, learned embeddings: {String((tech.retrieval as Rec)?.learned_embeddings)}; semantic: {String((tech.semantic as Rec)?.status ?? "n/a")}; sufficiency: {String(tech.sufficiency)}</p>
            <p>validation: {((tech.reasons ?? []) as string[]).join(", ") || "none"}; dropped claims: {((tech.dropped_claims ?? []) as Rec[]).length}</p>
            {((tech.dropped_claims ?? []) as Rec[]).map((d, i) => <p key={i} className="text-muted">dropped: {d.reason as string}</p>)}
            <table className="text-xs"><tbody>
              {((tech.pack_items ?? []) as Rec[]).map((i) => <tr key={i.item_id as string}><td><code>{i.item_id as string}</code></td><td>{i.kind as string}</td><td>{String(i.authority ?? "")}</td><td>{((i.via ?? []) as string[]).join("+")}</td><td>{String(i.relevance ?? "")}</td></tr>)}
            </tbody></table>
          </div>
        </Tech>
      )}
    </article>
  );
}

export function BusinessAsk({ ws }: { ws: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const [qid, setQid] = useParam("q");
  const [question, setQuestion] = React.useState("");
  const [view, setView] = React.useState<"target" | "observed">("target");
  const [data, setData] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [followUp, setFollowUp] = React.useState(false);
  const [reload, setReload] = React.useState(0);
  const askWhy = React.useId();
  const mode = s.mode;
  React.useEffect(() => {
    if (!qid) { setData(null); return; }
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const r = await api<Rec>("GET", `/v2/business/questions/${q(qid)}?workspace_id=${q(ws)}&mode=${mode}`);
        if (!live) return;
        setData(r); setErr(null);
        if (r.status === "running" || r.status === "waiting_for_approval" || r.status === "waiting_for_slot") timer = setTimeout(() => void tick(), 1500);
      } catch (e) {
        if (!live) return;
        if (e instanceof ApiError) setErr(e.status === 404 || e.status === 403 ? "This question is not available to your account." : "The answer could not be loaded.");
        else throw e;
      }
    };
    void tick();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [qid, ws, mode, reload]);
  const canAsk = s.can("knowledge.answer") && s.can("ai.use");
  const submit = async () => {
    const body: Rec = { workspace_id: ws, question: question.trim(), view };
    if (followUp && qid) body.follow_up_of = qid;
    const r = await run(() => api<Rec>("POST", "/v2/business/questions", body));
    if (r) { setQid(r.question_id as string); setQuestion(""); setFollowUp(false); }
  };
  return (
    <div className="grid gap-4" data-testid="business-ask">
      {canAsk ? (
        <div className="rounded-lg border border-border bg-surface p-4 grid gap-3">
          <Field label={followUp ? "Your follow-up question" : "Your question"} hint="Answers use the accepted knowledge and say what they cannot tell. Asking the same question again shows the stored answer.">
            <TextArea data-testid="qa-input" rows={2} maxLength={1000} value={question} onChange={(e) => setQuestion(e.target.value)} />
          </Field>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm"><input type="radio" name="qa-view" data-testid="qa-view-target" checked={view === "target"} onChange={() => setView("target")} /> Decided knowledge</label>
            <label className="flex items-center gap-2 text-sm"><input type="radio" name="qa-view" data-testid="qa-view-observed" checked={view === "observed"} onChange={() => setView("observed")} /> What the sources say</label>
            <Button variant="primary" data-testid="qa-ask" title={!question.trim() ? "Write your question first." : pending ? "The question is being sent." : undefined} aria-describedby={!question.trim() ? askWhy : undefined} disabled={pending || !question.trim()} onClick={() => void submit()}>Ask</Button>
            {!question.trim() && <span id={askWhy} data-testid="qa-ask-why" className="text-sm text-muted">Write your question first.</span>}
            {qid && data?.status === "done" && !followUp && <Button data-testid="qa-followup" onClick={() => setFollowUp(true)}>Ask a follow-up</Button>}
          </div>
        </div>
      ) : <Muted testid="qa-no-ask">You can read answers, but your role cannot ask new questions.</Muted>}
      {err && <Callout tone="danger" testid="qa-error">{err}</Callout>}
      {data && data.status !== "done" && data.status !== "failed" && data.status !== "cancelled" && <div data-testid="qa-pending" data-status={String(data.status)}>
        <RunHold ws={ws} status={String(data.status)} hold={data.hold as Rec | undefined} onChanged={() => setReload((n) => n + 1)} /></div>}
      {data && (data.status === "failed" || data.status === "cancelled") && <Callout tone="danger" testid="qa-failed">{JOB_WORDS[data.status as string]}: {reasonText(data.reason as string | null)}</Callout>}
      {data && data.status === "done" && data.answer && <>
        <Muted testid="qa-question">Question: {String(data.question)}</Muted>
        <AnswerCard data={data} mode={mode} />
      </>}
    </div>
  );
}
