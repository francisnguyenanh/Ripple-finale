// Private request assistant (RQA-04, EUI-02): questions about ONE request, answered from what this person may read about it. Opening the panel, switching views and
// reading the conversation never call a model or create anything; suggested questions (from the task view) and the composer show at once. "Ask AI" on a suggestion and
// "Ask" send one paid question (stated next to the buttons); "Edit question" only fills the composer. The first question creates the private thread in the same
// idempotent call. "Go to" chips focus an existing control on the page and never submit it. Answers show their blocks as facts, interpretations or suggestions with the
// facts they cite; refused blocks are counted, not shown. Who holds a step or may act ("Who acts") is a sentence written by the server from a checked assertion, never the
// model's own wording: since answer/5 the server writes it from the reader's task record (origin task_record); stored checks/6 answers may show a corrected sentence;
// an answer stored under older checks says so and hides its free-text authority statements. An answer asked under different access is withheld until asked again. The assistant never acts: next steps are
// the request's own controls, named here, never submitted from the chat. "I agree" in the chat is not an approval.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, TextArea } from "@/components/bits";
import { StatusBadge, type Status } from "@/components/StatusBadge";
import { OriginTag } from "@/components/decision/Origin";
import { actionStillCurrent, actionTarget, citationTarget, suggestQuestions } from "./guide";
import { useSession } from "@/session";

type Rec = Record<string, unknown>;
const q = encodeURIComponent;
const STATE: Record<string, { status: Status; label: string }> = {
  pending: { status: "running", label: "Being answered" }, answered: { status: "info", label: "Answered from the facts" }, withheld: { status: "stale", label: "Withheld: your access changed" },
  unavailable: { status: "failed", label: "No answer" }, cancelled: { status: "stale", label: "Cancelled" },
};
const OUTCOME: Record<string, string> = { answered: "Answered", partial: "Partly answered", insufficient_context: "The facts do not answer this", out_of_scope: "Outside this request",
  refused: "The answer did not pass the checks" };
const KIND: Record<string, string> = { authority: "Who acts", cause: "Why", fact: "Fact", interpretation: "Interpretation", suggestion: "Suggestion" };
// what a cited fact is, for a Business reader (the fact id and its full text stay in the Technical view)
const FACT_KIND: Record<string, string> = { state: "Stage", request: "Request", obligation: "Step", waiting: "Waiting", blocker: "Blocker", action: "Action", step: "Progress",
  axis: "Completion", rule_change: "Rule change", impact: "Impact", decision: "Decision", review_note: "Reviewer note" };

function newKey(): string {
  return (globalThis.crypto && "randomUUID" in globalThis.crypto) ? globalThis.crypto.randomUUID() : `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function RequestAssistant({ ws, family, id, tv }: { ws: string; family: "intent_flow" | "business_change"; id: string; tv?: Rec | null }) {
  const technical = useSession().mode === "technical";
  const [loaded, setLoaded] = React.useState(false);
  const [st, setSt] = React.useState<Rec | null>(null);
  const [text, setText] = React.useState("");
  const [err, setErr] = React.useState<string | null>(null);
  const [jump, setJump] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const inflight = React.useRef<{ question: string; key: string } | null>(null);   // one logical question keeps its key across retries and lost responses
  const composer = React.useRef<HTMLTextAreaElement>(null);
  const paidId = React.useId();
  const tid = st?.thread_id ? String(st.thread_id) : null;

  React.useEffect(() => {                                                          // read only: this person's own threads of this request; opening creates nothing
    let live = true;
    api<Rec>("GET", `/v2/requests/${family}/${q(id)}/assistant/threads?workspace_id=${q(ws)}`)
      .then(async (r) => {
        if (!live) return;
        const list = ((r.threads ?? []) as Rec[]).filter((t) => Number(t.messages ?? 0) > 0);
        if (list.length) { const t = await api<Rec>("GET", `/v2/request-assistant/threads/${q(String(list[0].thread_id))}?workspace_id=${q(ws)}`); if (live) setSt(t); }
        if (live) setLoaded(true);
      })
      .catch((e) => { if (live) setErr(e instanceof ApiError && e.status === 404 ? "This request is not available to your account." : errorText(e)); });
    return () => { live = false; };
  }, [ws, family, id]);

  const msgs = (st?.messages ?? []) as Rec[];
  const pending = msgs.some((m) => m.state === "pending");
  React.useEffect(() => {                                                          // poll only while an answer is pending; reading never calls a model
    if (!tid || !pending) return;
    const t = window.setTimeout(() => { api<Rec>("GET", `/v2/request-assistant/threads/${q(tid)}?workspace_id=${q(ws)}`).then(setSt).catch((e) => setErr(errorText(e))); }, 1500);
    return () => window.clearTimeout(t);
  }, [ws, tid, st, pending]);

  const refusal = (e: unknown) => {
    if (e instanceof ApiError) {
      const code = String((e.body as Rec | null)?.code ?? "");
      if (code === "assistant_not_permitted") return "Asking needs the assistant permission, or it is suspended for you. Nothing was asked.";
      if (code === "sequence_conflict") return "The conversation changed in another window. Reload it, then ask again.";
      if (code === "thread_full") return "This conversation is full; start a new one.";
      if (code === "context_overflow") return "This request has more required facts than one question can carry. Nothing was asked; use the request's own pages.";
    }
    return `${errorText(e)} Your question is kept below; asking again sends the same question once.`;
  };
  const keyFor = (question: string) => {
    if (inflight.current?.question === question) return inflight.current.key;
    inflight.current = { question, key: newKey() };
    return inflight.current.key;
  };
  const ask = async (raw: string) => {                                             // one explicit click = one question (the first one: thread + message in one idempotent call)
    const question = raw.replace(/\s+/g, " ").trim();
    if (!question || busy) return;
    setBusy(true); setErr(null); setJump(null);
    const key = keyFor(question);
    try {
      const r = tid
        ? await api<Rec>("POST", `/v2/request-assistant/threads/${q(tid)}/messages`, { workspace_id: ws, question, idempotency_key: key, after_seq: msgs.length ? Number(msgs[msgs.length - 1].seq) : 0 })
        : await api<Rec>("POST", `/v2/requests/${family}/${q(id)}/assistant/ask`, { workspace_id: ws, question, idempotency_key: key });
      setSt(r); inflight.current = null;
      setText((t) => (t.replace(/\s+/g, " ").trim() === question ? "" : t));
    } catch (e) {
      setErr(refusal(e));
      setText((t) => (t.trim() ? t : question));                                   // the draft survives a failure; a different draft is never overwritten
    } finally { setBusy(false); }
  };
  const cancel = async (mid: string) => {
    if (!tid) return;
    try { setSt(await api<Rec>("POST", `/v2/request-assistant/threads/${q(tid)}/messages/${q(mid)}/cancel`, { workspace_id: ws })); } catch (e) { setErr(refusal(e)); }
  };
  const go = (action: string) => {                                                 // navigation only: focus the existing control, never submit it
    const target = actionStillCurrent(tv, action) ? actionTarget(family, action) : null;
    const el = target ? document.querySelector<HTMLElement>(`[data-testid="${target}"]`) : null;
    if (!el) { setJump("That step's control is not available on this page right now. Use the request's own page and controls."); return; }
    for (let p = el.parentElement; p; p = p.parentElement) if (p instanceof HTMLDetailsElement) p.open = true;
    el.scrollIntoView({ block: "center" });
    el.focus();
    setJump(null);
  };
  const suggestions = suggestQuestions(tv, msgs.map((m) => String(m.question)));

  return (
    <aside data-testid="request-assistant" aria-label="Ask about this request" className="rounded-md border border-border p-3 grid gap-2 content-start">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-semibold">Ask about this request</h3>
        <StatusBadge status="info" label="Private to you" />
      </div>
      <Muted>The assistant reads only what you may read about this request, answers from those facts and never acts. Writing "I agree" here approves nothing.</Muted>
      {err && <Callout tone="danger" testid="assistant-error">{err}</Callout>}
      {!loaded && !err && <p role="status" className="text-sm text-muted">Loading...</p>}
      {msgs.length > 0 && (
        <ol className="grid gap-3 text-sm" aria-live="polite">
          {msgs.map((m) => <AssistantMessage key={String(m.message_id)} m={m} technical={technical} onCancel={(mid) => void cancel(mid)}
            onAction={go} actionable={(a) => actionStillCurrent(tv, a) && actionTarget(family, a) !== null}
            onSource={tid ? (mid, ref) => api<Rec>("GET", `/v2/request-assistant/threads/${q(tid)}/messages/${q(mid)}/citations/${q(ref)}/source?workspace_id=${q(ws)}`) : undefined} />)}
        </ol>
      )}
      {jump && <Muted testid="assistant-jump-unavailable">{jump}</Muted>}
      {loaded && suggestions.length > 0 && (
        <div className="grid gap-1" data-testid="assistant-suggestions">
          <p className="text-sm font-medium">Suggested questions</p>
          <ul className="grid gap-2">
            {suggestions.map((sg) => (
              <li key={sg.id} data-testid="assistant-suggestion" data-id={sg.id} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="min-w-0">{sg.question}</span>
                <Button size="sm" variant="primary" data-testid="assistant-suggestion-ask" aria-label={`Ask AI: ${sg.question}`} aria-describedby={paidId}
                  disabled={busy || pending} onClick={() => void ask(sg.question)}>Ask AI</Button>
                <Button size="sm" variant="ghost" data-testid="assistant-suggestion-edit" aria-label={`Edit question: ${sg.question}`}
                  onClick={() => { setText(sg.question); composer.current?.focus(); }}>Edit question</Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {loaded && (
        <>
          <Field label="Your question about this request">
            <TextArea ref={composer} data-testid="assistant-question" rows={2} maxLength={800} value={text} onChange={(e) => setText(e.target.value)} />
          </Field>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="primary" data-testid="assistant-ask" aria-describedby={paidId} disabled={busy || pending || !text.trim()} onClick={() => void ask(text)}>Ask</Button>
            <span id={paidId} data-testid="assistant-paid-note" className="text-xs text-muted">Each question you send (Ask or Ask AI) is one paid model call. Editing a question sends nothing.</span>
          </div>
        </>
      )}
    </aside>
  );
}

/** Mark and focus the section a citation points to; nothing found = a polite status note (the page may not show that part to this reader, or not here). */
function showCitation(selector: string): boolean {
  document.querySelectorAll("[data-highlight]").forEach((el) => el.removeAttribute("data-highlight"));
  const el = document.querySelector<HTMLElement>(selector);
  if (!el) return false;
  if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
  el.setAttribute("data-highlight", "true");
  el.scrollIntoView({ block: "center" });
  el.focus();
  return true;
}

/** One question and its checked answer (presentational: no request, no state). Used by the panel and by the design catalogue with synthetic data. */
const SOURCE_WHY: Record<string, string> = {
  needs_evidence_rights: "The source of this fact needs evidence rights; it is not shown.", stale_context: "The request changed since this answer; ask again to see current sources.",
  no_source_locator: "This fact comes from the request itself, not from a source file.", no_source_binding: "No source place is recorded for this rule.",
  access_changed: "Your access changed since this answer.", not_cited_by_this_answer: "This answer does not cite that fact.",
};
const SOURCE_STATE: Record<string, string> = { current: "current", changed_since: "changed since", absent_now: "no longer in the sources" };

export function AssistantMessage({ m, technical, onCancel, onAction, actionable, onSource }:
  { m: Rec; technical: boolean; onCancel?: (messageId: string) => void; onAction?: (action: string) => void; actionable?: (action: string) => boolean;
    onSource?: (messageId: string, factRef: string) => Promise<Rec> }) {
  const badge = STATE[String(m.state)] ?? { status: "info" as Status, label: String(m.state) };
  const ans = (m.answer ?? null) as Rec | null;
  const cites = (m.citations ?? {}) as Record<string, Rec>;
  const [citeNote, setCiteNote] = React.useState<string | null>(null);
  const [sources, setSources] = React.useState<Record<string, Rec | "loading">>({});
  const showSource = async (r: string) => {                                         // EUI-03: server-resolved, read-only, no model call
    if (!onSource) return;
    setSources((x) => ({ ...x, [r]: "loading" }));
    try { const res = await onSource(String(m.message_id), r); setSources((x) => ({ ...x, [r]: res })); }
    catch { setSources((x) => ({ ...x, [r]: { available: false, reason: "unavailable" } })); }
  };
  return (
    <li data-testid="assistant-message" data-state={String(m.state)} className="grid gap-1">
      <span><strong>You:</strong> {String(m.question)}</span>
      <div className="flex flex-wrap items-center gap-2"><StatusBadge status={badge.status} label={badge.label} />
        {ans?.status && m.state === "answered" ? <span data-testid="assistant-outcome" className="text-xs">{OUTCOME[String(ans.status)] ?? String(ans.status)}</span> : null}
        {m.state === "answered" && m.current === false ? <span className="text-xs text-warning">Historical: the request changed since (as of {new Date(String(m.as_of)).toLocaleString()})</span> : null}
        {m.state === "pending" && onCancel ? <Button size="sm" variant="ghost" data-testid="assistant-cancel" onClick={() => onCancel(String(m.message_id))}>Stop</Button> : null}
      </div>
      {m.state === "withheld" ? <Muted>{String(m.reason)}</Muted> : null}
      {m.state === "unavailable" ? <Muted testid="assistant-unavailable">No answer was produced{ans?.reason ? ` (${String(ans.reason)})` : ""}. Nothing was concluded; you can ask again.</Muted> : null}
      {((ans?.blocks ?? []) as Rec[]).map((b, i) => (
        <div key={i} data-testid="assistant-block" data-kind={String(b.kind)} data-corrected={b.corrected ? "true" : undefined} className="grid gap-0.5">
          <span>{b.kind === "authority" ? <OriginTag origin="source" label="Task record" /> : <OriginTag origin="ai" />}{" "}
            <span className="text-xs text-muted">{KIND[String(b.kind)] ?? String(b.kind)}{b.corrected ? " (corrected: the assistant's claim did not match the task record)" : ""}:</span> {String(b.text)}</span>
          {((b.fact_refs ?? []) as string[]).filter((r) => cites[r]).map((r) => (
            <span key={r} className="flex flex-wrap items-baseline gap-1">
              <Muted testid="assistant-citation">{technical ? `${r}: ${String(cites[r].text)}` : `${FACT_KIND[String(cites[r].kind)] ?? "Fact"}: ${String(cites[r].display ?? cites[r].text)}`}</Muted>
              {onSource && ["impact", "rule_change"].includes(String(cites[r].kind)) && (
                <Button size="sm" variant="ghost" data-testid="assistant-citation-source" data-kind={String(cites[r].kind)} onClick={() => void showSource(r)}>Source</Button>)}
              {citationTarget(cites[r]) && <Button size="sm" variant="ghost" data-testid="assistant-citation-show" data-kind={String(cites[r].kind)} onClick={() => setCiteNote(showCitation(citationTarget(cites[r])!) ? null : "That part is not shown on this page.")}>Show on this page</Button>}
              {sources[r] && sources[r] !== "loading" && (
                <span className="basis-full grid gap-0.5 text-xs" data-testid="assistant-source" data-available={String((sources[r] as Rec).available)}>
                  {(sources[r] as Rec).available
                    ? ((sources[r] as Rec).sources as Rec[]).map((x, i) => (
                      <span key={i} data-testid="assistant-source-item" data-state={String(x.state)}>
                        {technical ? String(x.logical_path) : String(x.logical_path).split("/").pop()}{x.locator ? ` · ${String(x.locator)}` : ""} ({SOURCE_STATE[String(x.state)] ?? String(x.state)})
                        {x.excerpt ? <code className="ml-1">{String(x.excerpt)}</code> : null}
                      </span>))
                    : <span data-testid="assistant-source-unavailable">{SOURCE_WHY[String((sources[r] as Rec).reason)] ?? "The source is not available."}</span>}
                </span>
              )}
            </span>))}
        </div>
      ))}
      {citeNote && <Muted testid="assistant-citation-missing">{citeNote}</Muted>}
      {ans?.legacy_checks ? <Muted testid="assistant-legacy">{String(ans.legacy_note)}</Muted> : null}
      {((ans?.rejected ?? []) as Rec[]).length ? <Muted testid="assistant-rejected">{((ans?.rejected ?? []) as Rec[]).length} part(s) of the answer were refused by the checks and are not shown.</Muted> : null}
      {((ans?.limitations ?? []) as string[]).length ? <Muted>Not covered: {((ans?.limitations ?? []) as string[]).join("; ")}</Muted> : null}
      {((ans?.next_actions ?? []) as Rec[]).length ? (
        <div data-testid="assistant-next" className="grid gap-1 text-sm text-muted">
          <span>Your next steps on this request (use its controls): {((ans?.next_actions ?? []) as Rec[]).map((a) => String(a.label)).join("; ")}.</span>
          {onAction && <span className="flex flex-wrap gap-2">{((ans?.next_actions ?? []) as Rec[]).filter((a) => actionable?.(String(a.action))).map((a) => (
            <Button key={String(a.action)} size="sm" variant="secondary" data-testid="assistant-action-go" data-action={String(a.action)} onClick={() => onAction(String(a.action))}>
              Go to: {String(a.label)}</Button>))}</span>}
        </div>) : null}
    </li>
  );
}
