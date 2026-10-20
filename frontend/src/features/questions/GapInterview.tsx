// Interview about one open question (AIX-K2 / LLM-K2). A session starts only when the person asks; each question is chosen by a model (or a labelled deterministic
// fallback), each answer is read by a model into the question's form, and the person confirms explicitly when the reading is complete. At most five turns; "I don't know"
// is always allowed; contradictions between answers stay visible. Hedged or vague words ("not sure which", "the usual way") are never confirmable: they get a follow-up.
// The person can stop at any time. Nothing is confirmed automatically and the session never writes knowledge by itself.
import * as React from "react";
import { answerField, answerText, answerValue, readingStatus } from "@/lib/fmt";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Tech, TextArea } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const STATUS: Record<string, { status: "running" | "success" | "partial" | "stale" | "info"; label: string }> = {
  OPEN: { status: "running", label: "Interview open" }, READY_TO_CONFIRM: { status: "info", label: "Ready for your confirmation" }, UNRESOLVED: { status: "partial", label: "Left unresolved" },
  CONFIRMED: { status: "success", label: "Answer confirmed by you" }, STALE: { status: "stale", label: "Knowledge moved; interview out of date" }, CLOSED: { status: "stale", label: "Closed" },
};
const QUESTIONER: Record<string, string> = { model: "Question chosen by a model", deterministic: "Standard question (no model)", deterministic_fallback: "Standard question",
  model_pending: "A model is choosing the question" };
// why the server asked its own question instead of the model's (the raw code stays in the Technical view)
const FALLBACK: Record<string, string> = { model_declined: "the model proposed none", not_grounded: "the model's question did not refer to this question", not_plain_text: "the model's question was not plain text",
  repeats_earlier_question: "the model repeated an earlier question", provider_unavailable: "no model is available" };
const PROBLEM: Record<string, string> = { hedged_answer: "you were not sure", vague_reference: "the words do not say exactly what happens or who acts" };
const ROLE: Record<string, string> = { proposed_answer: "Proposed answer (plain words; you confirm it)", partial_answer: "Partly answered: the value is plain, another part is open",
  unresolved_interpretation: "Unresolved interpretation: not a fact, kept in the history", no_reading: "No reading", confirmed_fact: "Confirmed by you" };
const WHY: Record<string, string> = { unknown_declared: "You said you do not know.", stopped_by_person: "You stopped the interview.", turn_cap_reached: "The turn limit was reached.",
  no_remaining_clarification: "The last answers left the same point unclear, so no further question would help." };

function problemsOf(t: Rec): Rec[] {                                               // the server's recheck (assessment) when present, else the problems recorded at reading time
  const a = t.assessment as Rec | undefined;
  if (a && Array.isArray(a.problems)) return a.problems as Rec[];
  return ((((t.reading as Rec | null)?.completeness as Rec | undefined)?.problems as Rec[] | undefined) ?? []);
}

/** EUI-05 structured preview of the LATEST answer: the slots the model's reading filled (checked by the server), what it left open, and fields two answers
 *  disagree on. It updates when a reading arrives (no model call per keystroke) and is never a confirmation: the person's words stay the record. */
function ReadingPreview({ turn, contradictions, turns }: { turn: Rec; contradictions: Rec[]; turns: Rec[] }) {
  const reading = (turn.reading ?? null) as Rec | null;
  const proposed = (reading?.proposed_answer ?? null) as Rec | null;
  const open = ((reading?.uncovered ?? []) as string[]);
  const disputed = new Set(contradictions.map((c) => String(c.field)));
  const replaced = turns.filter((t) => t.text && (t.assessment as Rec | undefined)?.superseded).length;
  return (
    <div className="grid gap-1 rounded-md border border-border p-2 text-sm" data-testid="interview-preview" data-reading={String(reading?.status ?? (turn.unknown ? "UNKNOWN" : "PENDING"))}>
      <p className="font-medium">Structured preview of your latest answer (read by a model, checked by the server; not confirmed)</p>
      <p><span className="text-muted">Your words:</span> "{String(turn.text)}"</p>
      {turn.unknown ? <Muted>You said you do not know; nothing is filled in.</Muted>
        : !reading ? <Muted testid="interview-preview-waiting">Waiting for the reading; nothing is interpreted yet.</Muted>
        : (
          <>
            <p><span className="text-muted">Reading:</span> {readingStatus(reading.status)}</p>
            {proposed && Object.keys(proposed).length > 0 ? (
              <ul className="grid gap-0.5">
                {Object.entries(proposed).map(([k, v]) => (
                  <li key={k} data-testid="interview-slot" data-field={k} data-state={disputed.has(k) ? "conflict" : "read"}>
                    <span className="font-medium">{answerField(k)}:</span> {answerValue(v)}{disputed.has(k) ? <span className="text-warning"> (another answer disagrees)</span> : null}
                  </li>
                ))}
              </ul>
            ) : <Muted>No field was filled from this answer.</Muted>}
            {open.length > 0 && <p data-testid="interview-slot-open"><span className="text-muted">Still open:</span> {open.join("; ")}</p>}
          </>
        )}
      {replaced > 0 && <Muted testid="interview-preview-replaced">{replaced} earlier answer{replaced === 1 ? " was" : "s were"} replaced by a later one and {replaced === 1 ? "is" : "are"} not used.</Muted>}
    </div>
  );
}

export function GapInterview({ ws, gapId, onConfirmed }: { ws: string; gapId: string; onConfirmed: () => Promise<void> }) {
  const [st, setSt] = React.useState<Rec | null>(null);
  const [text, setText] = React.useState("");
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const sid = st?.session_id ? String(st.session_id) : null;
  const [vocab, setVocab] = React.useState<Rec | null>(null);
  const [helped, setHelped] = React.useState(false);
  const openTurn = st?.status === "OPEN" && st?.open_question ? String((st.open_question as Rec).turn) : null;
  React.useEffect(() => {                                                          // EUI-05 wording help: read-only, no model call, refreshed per open question
    if (!sid || !openTurn) { setVocab(null); return; }
    let live = true;
    api<Rec>("GET", `/v2/business/interviews/${q(sid)}/vocabulary?workspace_id=${q(ws)}`).then((v) => { if (live) setVocab(v); }).catch(() => { if (live) setVocab(null); });
    return () => { live = false; };
  }, [ws, sid, openTurn]);
  const blank = /\[[^\[\]]{1,40}\]/;
  const fill = (word: string) => { setHelped(true); setText((t) => (blank.test(t) ? t.replace(blank, word) : t.trim() ? `${t.trim()} ${word}` : word)); };

  const waiting = !!st && (!!st.question_pending || ((st.turns ?? []) as Rec[]).some((t) => t.text && !t.reading && !t.unknown) || (st.status === "OPEN" && !st.open_question));
  React.useEffect(() => {                                                          // poll only while a question or a reading of an answer is pending
    if (!sid || !waiting) return;
    const t = window.setTimeout(() => {
      api<Rec>("GET", `/v2/business/interviews/${q(sid)}?workspace_id=${q(ws)}`).then(setSt).catch((e) => setErr(errorText(e)));
    }, 1500);
    return () => window.clearTimeout(t);
  }, [ws, sid, st]);

  const call = async (fn: () => Promise<Rec>) => {
    setBusy(true); setErr(null);
    try { setSt(await fn()); }
    catch (e) {
      const code = e instanceof ApiError ? String((e.body as Rec).code ?? "") : "";
      setErr(code === "unfilled_placeholder" ? "Replace each [blank] with your own words before sending." : code === "question_pending" ? "The next question is still being chosen; try again in a moment." : e instanceof ApiError && e.status === 403 ? "An interview needs knowledge authority and the AI permission." : errorText(e));
    } finally { setBusy(false); }
  };

  if (!st) return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" data-testid="interview-start" disabled={busy} onClick={() => void call(() => api<Rec>("POST", `/v2/business/gaps/${q(gapId)}/interview`, { workspace_id: ws }))}>
        {busy ? "Starting..." : "Answer through an interview"}
      </Button>
      <span className="text-xs text-muted">Up to five questions; each question and each reading of your answer can use a paid model call.</span>
      {err && <Callout tone="danger" testid="interview-error">{err}</Callout>}
    </div>
  );
  const badge = STATUS[String(st.status)] ?? { status: "info" as const, label: `State: ${String(st.status)}` };
  const open = (st.open_question ?? null) as Rec | null;
  const turns = (st.turns ?? []) as Rec[];
  const last = [...turns].reverse().find((t) => t.text);
  const reading = (last?.reading ?? null) as Rec | null;
  const stopButton = (
    <Button size="sm" variant="ghost" data-testid="interview-stop" disabled={busy} onClick={() => void call(() => api<Rec>("POST", `/v2/business/interviews/${q(String(sid))}/stop`, { workspace_id: ws }))}>Stop the interview</Button>
  );
  return (
    <section data-testid="interview" data-status={String(st.status)} aria-label="Interview" className="rounded-md border border-border p-3 grid gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge status={badge.status} label={badge.label} />
        <span data-testid="interview-turns-left">{String(st.turns_left)} of {String(st.cap)} turns left</span>
      </div>
      <ol className="grid gap-2 text-sm">
        {turns.filter((t) => t.text).map((t) => (
          <li key={String(t.turn)} data-testid="interview-turn" className="grid gap-0.5">
            <span><strong>Q{String(t.turn)}:</strong> {String(t.question ?? "")} <span className="text-muted">({QUESTIONER[String(t.questioner)] ?? String(t.questioner)})</span></span>
            <span><strong>You:</strong> {String(t.text)}</span>
            {t.unknown ? <Muted>You said you do not know.</Muted> : t.reading ? <Muted>Read by a model as: {readingStatus((t.reading as Rec).status)}{((t.reading as Rec).uncovered as string[] | undefined)?.length ? `; not covered: ${((t.reading as Rec).uncovered as string[]).join("; ")}` : ""}</Muted> : <Muted>Your answer is being read...</Muted>}
            {problemsOf(t).length ? (
              <Muted testid="interview-incomplete">Not confirmable yet: {problemsOf(t).map((p) => `${PROBLEM[String(p.code)] ?? String(p.code)}${p.words ? ` ("${String(p.words)}")` : ""}`).join("; ")}.</Muted>
            ) : null}
            {t.assessment ? <span className="text-xs text-muted" data-testid="interview-turn-role" data-role={String((t.assessment as Rec).role)}>
              {ROLE[String((t.assessment as Rec).role)] ?? String((t.assessment as Rec).role)}{(t.assessment as Rec).superseded ? " (a later answer replaced it)" : ""}</span> : null}
          </li>
        ))}
      </ol>
      {last && <ReadingPreview turn={last} contradictions={(st.contradictions ?? []) as Rec[]} turns={turns} />}
      {Array.isArray(st.contradictions) && st.contradictions.length > 0 && (
        <Callout tone="warn" testid="interview-contradiction">Two of your answers disagree about {(st.contradictions as Rec[]).map((c) => String(c.field)).join(", ")}. Answer the next question to settle it; nothing is chosen for you.</Callout>
      )}
      {st.question_pending ? <p role="status" className="text-sm text-muted">The next question is being chosen...</p> : null}
      {st.status === "OPEN" && open && !st.question_pending && (
        <div className="grid gap-1" data-testid="interview-open-question" data-questioner={String(open.questioner)}>
          <p><strong>Q{String(open.turn)}:</strong> {String(open.question)}</p>
          <Muted>{QUESTIONER[String(open.questioner)] ?? String(open.questioner)}{open.fallback_reason ? ` (${FALLBACK[String(open.fallback_reason)] ?? "the model's question could not be used"})` : ""}.</Muted>
          <Field label="Your answer">
            <TextArea data-testid="interview-answer" rows={2} maxLength={1200} value={text} onChange={(e) => setText(e.target.value)} />
          </Field>
          {vocab && (((vocab.vocabulary ?? []) as Rec[]).length > 0 || ((vocab.starters ?? []) as string[]).length > 0) && (
            <div className="grid gap-1 text-sm" data-testid="interview-help">
              <p className="font-medium">Help with wording (fills your answer; sends nothing)</p>
              {((vocab.starters ?? []) as string[]).length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-muted">Sentence starters:</span>
                  {((vocab.starters ?? []) as string[]).map((st0) => (
                    <Button key={st0} size="sm" variant="ghost" data-testid="interview-starter" onClick={() => { setHelped(true); setText((t) => (t.trim() ? `${t.trim()} ${st0}` : st0)); }}>{st0}</Button>))}
                </div>
              )}
              {((vocab.vocabulary ?? []) as Rec[]).length > 0 && (
                <div className="flex flex-wrap items-center gap-1" data-testid="interview-vocabulary">
                  <span className="text-muted">{String(vocab.label)}:</span>
                  {((vocab.vocabulary ?? []) as Rec[]).map((v) => (
                    <Button key={`${String(v.group)}-${String(v.text)}`} size="sm" variant="ghost" data-testid="interview-word" data-group={String(v.group)}
                      aria-label={`Use "${String(v.text)}" (used in ${String(v.used_in)} other accepted rule${Number(v.used_in) === 1 ? "" : "s"}; not established for this rule)`}
                      onClick={() => fill(String(v.text))}>{String(v.text)} <span className="text-xs text-muted">({String(v.used_in)})</span></Button>))}
                </div>
              )}
              {blank.test(text) && <Muted testid="interview-blank">Replace each [blank] with your own words before sending; a blank is not an answer.</Muted>}
              {helped && !blank.test(text) && text.trim() && <Muted testid="interview-helped">Check and edit the sentence: what you send is recorded as your own words.</Muted>}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="primary" data-testid="interview-send" disabled={busy || !text.trim() || blank.test(text)} onClick={() => void call(async () => {
              const r = await api<Rec>("POST", `/v2/business/interviews/${q(String(sid))}/answer`, { workspace_id: ws, text: text.trim() }); setText(""); setHelped(false); return r;
            })}>Send answer</Button>
            <Button size="sm" data-testid="interview-unknown" disabled={busy} onClick={() => void call(() => api<Rec>("POST", `/v2/business/interviews/${q(String(sid))}/answer`, { workspace_id: ws, text: "I don't know" }))}>I don't know</Button>
            {stopButton}
          </div>
        </div>
      )}
      {st.status === "READY_TO_CONFIRM" && (
        <div className="grid gap-1" data-testid="interview-ready">
          <p className="text-sm">The model read your last answer as complete. Check it; your own words are what gets recorded.</p>
          {reading?.proposed_answer ? <Muted>Read as: {answerText(reading.proposed_answer)} from "{String(reading.quote ?? last?.text ?? "")}".</Muted> : null}
          <div><Button size="sm" variant="primary" data-testid="interview-confirm" disabled={busy} onClick={() => void call(async () => {
            const r = await api<Rec>("POST", `/v2/business/interviews/${q(String(sid))}/confirm`, { workspace_id: ws }); await onConfirmed(); return r;
          })}>Confirm this answer</Button> {stopButton}</div>
        </div>
      )}
      {st.status === "UNRESOLVED" && <Muted testid="interview-unresolved">{st.unresolved_reason ? `${WHY[String(st.unresolved_reason)] ?? String(st.unresolved_reason)} ` : ""}Nothing was saved. The question stays open; you can still answer it in the form above.</Muted>}
      {err && <Callout tone="danger" testid="interview-error">{err}</Callout>}
      <Tech><span className="text-xs">session <code>{String(st.session_id)}</code> questioner mode <code>{String(st.questioner_mode)}</code> transcript <code>{String(st.transcript_hash ?? "")}</code></span></Tech>
    </section>
  );
}
