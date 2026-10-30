// One conversation component for every free-text surface that is not a change request: the person writes in their own words, the AI asks
// back in the same conversation, a structured PREVIEW appears (only the fields this request produced, each traced to the person's words),
// the person edits or confirms it, and only then is the existing guarded action offered. Confirming a preview is not an approval and
// executes nothing. The conversation is stored on the server (scoped to the account and workspace) and is part of the link.
import * as React from "react";
import { Send } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, TechField, TextArea, usePlain } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { Permission, Turns } from "@/features/changes/ChatBits";
import type { Chat, Rec, Understanding } from "@/features/changes/types";
import { EquivalentStep } from "@/components/EquivalentStep";

export interface Surface {
  purpose: "knowledge_qa" | "gap_reply" | "draft_feedback" | "whatif" | "question_reply";
  /** test id prefix, for example "ask" */
  prefix: string;
  heading: string;
  intro: string;
  placeholder: string;
  startLabel: string;
  /** the preview fields of this surface, built only from what the interpretation produced */
  preview: (u: Understanding) => React.ReactNode;
  /** optional controls to change the preview by hand (no AI call); calls `save(fields)` */
  edit?: (props: { u: Understanding; save: (fields: Rec) => Promise<void>; pending: boolean }) => React.ReactNode;
  /** the existing guarded action that follows a confirmed preview */
  action: (props: { chat: Chat; outcome: Rec; markDone: (ref: string) => Promise<void> }) => React.ReactNode;
}

const q = encodeURIComponent;

export function PurposeChat({ ws, surface, context, convId, onConvId, seed }: { ws: string; surface: Surface; context?: Rec; convId: string; onConvId: (id: string) => void; seed?: string }) {
  const s = useSession();
  const notify = useNotify();
  const p = usePlain();
  const { run, pending } = useAction();
  const tp = surface.prefix;
  const [chat, setChat] = React.useState<Chat | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [first, setFirst] = React.useState(seed ?? "");
  React.useEffect(() => { if (seed) setFirst((cur) => cur || seed); }, [seed]);
  const [reply, setReply] = React.useState("");
  const [ack, setAck] = React.useState(false);
  const polls = React.useRef(0);
  const lastPump = React.useRef(0);
  const limit = chat?.limits?.max_user_turn_chars ?? 2000;

  const load = React.useCallback(async () => {
    if (!convId) { setChat(null); return; }
    try { setChat(await api<Chat>("GET", `/v2/chat/${q(convId)}?workspace_id=${q(ws)}`)); setErr(null); }
    catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws, convId]);
  React.useEffect(() => { polls.current = 0; setAck(false); void load(); }, [load]);

  // Bounded looking again while a person's allowance or the AI reply is pending; never replaces what is being typed.
  React.useEffect(() => {
    if (!chat || chat.status !== "OPEN" || !(chat.awaiting_ai || chat.awaiting_allowance)) { polls.current = 0; return; }
    const max = chat.awaiting_ai ? 60 : 20;
    if (polls.current >= max) return;
    const t = window.setTimeout(() => {
      polls.current += 1;
      if (chat.awaiting_ai && s.sandbox && Date.now() - lastPump.current > 3000) { lastPump.current = Date.now(); api("POST", "/v2/sandbox/pump", { workspace_id: ws }).catch(() => undefined); }
      void load();
    }, chat.awaiting_ai ? 2000 : 6000);
    return () => window.clearTimeout(t);
  }, [chat, load, s.sandbox, ws]);

  const call = (path: string, body: Rec) => api<Chat>("POST", `/v2/chat/${q(convId)}${path}`, { workspace_id: ws, ...body });

  if (err) return err.status === 403 || err.status === 404
    ? <Callout tone="warn" testid={`${tp}-unavailable`}>This conversation is not available to your account. Nothing was changed.</Callout>
    : <Callout tone="danger" testid="state-error">The conversation could not be read (HTTP {err.status}). <Button onClick={() => void load()}>Try again</Button></Callout>;

  const head = (
    <>
      <h3 className="text-base font-semibold">{surface.heading}</h3>
      <Muted>{surface.intro}</Muted>
    </>
  );

  if (!convId) {
    const start = async () => {
      const text = first.trim();
      if (!text) { notify.status("Write something first."); return; }
      const r = await run(() => api<Chat>("POST", "/v2/chat/start", { workspace_id: ws, text, purpose: surface.purpose, ...(context ? { context } : {}) }));
      if (r) { setFirst(""); onConvId(r.conversation_id); }
    };
    return (
      <div data-testid={`${tp}-panel`} data-state="new" className="grid gap-3">
        {head}
        <TextArea data-testid={`${tp}-input`} rows={3} aria-label={surface.placeholder} placeholder={surface.placeholder} maxLength={2000} value={first} onChange={(e) => setFirst(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void start(); } }} />
        <div className="flex items-center gap-3">
          <Button variant="primary" data-testid={`${tp}-start`} disabled={pending} onClick={start}><Send size={16} aria-hidden="true" />{surface.startLabel}</Button>
          <span className="text-xs text-muted tnum">{first.length} / 2000</span>
        </div>
      </div>
    );
  }
  if (!chat) return <StatusBadge status="loading" />;

  const open = chat.status === "OPEN";
  const mine = chat.you_are_creator;
  const u = chat.understanding;
  const ackNeeded = !!u && ((u.assumptions ?? []).length > 0 || (u.out_of_scope ?? []).length > 0);
  const outcome = chat.outcome ?? null;
  const markDone = async (ref: string) => { await run(() => call("/executed", { ref })); await load(); };

  return (
    <div data-testid={`${tp}-panel`} data-state={chat.status + (chat.awaiting_ai ? "-ai" : chat.awaiting_allowance ? "-allow" : "")} className="grid gap-3">
      {head}
      <Turns chat={chat} account={s.account} />
      {chat.safety_notes.map((h) => <Muted key={h.code} testid={`${tp}-safety`}><span data-code={h.code}>{h.business}</span></Muted>)}
      {u && (
        <div data-testid={`${tp}-understanding`} data-state={u.status} data-version={String(chat.version)} className="rounded-md border border-border bg-surface-2 p-3 grid gap-2 anim-in">
          <p className="font-semibold">{u.status === "READY" ? "This is what I understood" : u.status === "CLARIFY" ? "I still need some details" : u.status === "UNSUPPORTED" ? "I cannot do this here" : "No usable reply"}</p>
          {u.status !== "UNSUPPORTED" && surface.preview(u)}
          {(u.questions ?? []).map((x, i) => <Callout key={i} tone="warn" testid={`${tp}-question`}>{p(x)}</Callout>)}
          {(u.out_of_scope ?? []).length > 0 && <Callout tone="warn" testid={`${tp}-out-of-scope`}>Not covered: {(u.out_of_scope ?? []).join("; ")}</Callout>}
          {(u.assumptions ?? []).length > 0 && <Muted testid={`${tp}-assumptions`}>Check: {(u.assumptions ?? []).join("; ")}</Muted>}
          <TechField label="conversation id" value={chat.conversation_id} />
          <TechField label="understanding version" value={chat.version} />
          <TechField label="source of this version" value={u.source ?? "model"} />
        </div>
      )}
      <Permission chat={chat} ws={ws} open={open} canRevoke={chat.can_allow || mine || s.can("knowledge.confirm")} onChanged={load} />
      {chat.awaiting_allowance && open && (
        <Callout tone="wait" testid={`${tp}-awaiting-allowance`}>
          <strong>The AI has not been asked yet</strong>
          {chat.ai_block
            ? <span data-testid={`${tp}-ai-block`} data-reason={chat.ai_block}>{({ suspended: "AI use is switched off for your account in this workspace; the workspace administrator can switch it on.", no_permission: "Your role does not include AI use; the workspace administrator can add it.", revoked: "The AI replies of this conversation were withdrawn.", quota_used: "This conversation used all of its AI replies.", window_ended: "The time window an administrator set for this conversation has ended; a message does not extend it.", sandbox_hold: "In this shared trial workspace a different person has to allow each AI reply." } as Record<string, string>)[chat.ai_block] ?? "The AI cannot be used right now."} Nothing was sent to the AI.</span>
            : <span>Nothing was sent to the AI yet. It answers when you send a message, or press the button.</span>}
          {mine && !chat.ai_block && (
            <div className="flex flex-wrap gap-2 mt-1">
              <Button variant="primary" data-testid={`${tp}-allow`} title={aiWhy(s, "intent.interpret")} disabled={pending || !s.aiKind("intent.interpret")} onClick={async () => { if (await run(() => call("/allow", { turns: 1 }))) await load(); }}>Ask the AI</Button><EquivalentStep kind="intent.interpret" to={surface.purpose === "knowledge_qa" ? "ask" : surface.purpose === "question_reply" ? "direct" : "change"} />
            </div>
          )}
          {!mine && chat.ai_block === "sandbox_hold" && chat.can_allow && (
            <div className="flex flex-wrap gap-2 mt-1">
              <Button variant="primary" data-testid={`${tp}-allow-other`} title={aiWhy(s, "intent.interpret")} disabled={pending || !s.aiKind("intent.interpret")} onClick={async () => { if (await run(() => call("/allow", { turns: 1 }))) await load(); }}>Allow the AI to reply</Button>
            </div>
          )}
        </Callout>
      )}
      {mine && open && !chat.awaiting_ai && (
        <>
          <Field label={u?.status === "CLARIFY" ? "Your answer" : "Add something"}>
            <TextArea data-testid={`${tp}-reply`} rows={2} maxLength={limit} value={reply} onChange={(e) => setReply(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.querySelector<HTMLButtonElement>(`[data-testid="${tp}-send"]`)?.click(); } }} />
          </Field>
          <div className="flex items-center gap-3">
            <Button variant="primary" data-testid={`${tp}-send`} disabled={pending} onClick={async () => {
              const text = reply.trim(); if (!text) { notify.status("Write your answer first."); return; }
              if (await run(() => call("/message", { text }))) { setReply(""); await load(); }
            }}><Send size={16} aria-hidden="true" />Send</Button>
            <span className="text-xs text-muted tnum">{reply.length} / {limit}</span>
          </div>
          {surface.edit && u && (
            <details data-testid={`${tp}-edit`} className="rounded-md border border-border px-3 py-2">
              <summary className="cursor-pointer text-sm font-medium">Change the preview myself</summary>
              <div className="mt-3">{surface.edit({ u, pending, save: async (fields) => { if (await run(() => call("/edit", { fields }))) await load(); } })}</div>
            </details>
          )}
        </>
      )}
      {mine && open && (
        <>
          {ackNeeded && (
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" data-testid={`${tp}-ack`} checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-1" />I have checked what the AI assumed and what is left out</label>
          )}
          <div className="flex flex-wrap gap-2">
            {u?.status === "READY" && (
              <Button variant="primary" data-testid={`${tp}-confirm`} disabled={pending || chat.awaiting_ai} onClick={async () => {
                if (ackNeeded && !ack) { notify.status("Tick the box to confirm you checked the assumptions and what is left out."); return; }
                if (await run(() => call("/confirm", { version: chat.version, acknowledged: ackNeeded && ack }), { stale: true })) await load();
              }}>Yes, this is what I mean</Button>
            )}
            <Button data-testid={`${tp}-cancel`} disabled={pending} onClick={async () => { if (await run(() => call("/cancel", {}))) { await load(); } }}>Cancel</Button>
          </div>
          <Muted>Confirming only agrees on the preview. It is not an approval and nothing is done until you press the button that follows.</Muted>
        </>
      )}
      {!mine && <Muted testid={`${tp}-not-yours`}>This conversation belongs to {chat.created_by}. Only they can answer, edit or confirm it.</Muted>}
      {chat.status === "CANCELLED" && <Muted testid={`${tp}-closed`}>This conversation was cancelled.</Muted>}
      {chat.status === "CONFIRMED" && outcome && (
        <div data-testid={`${tp}-outcome`} data-action={String(outcome.action)} data-done={outcome.executed ? "true" : "false"} className="rounded-md border border-primary p-3 grid gap-2">
          <p className="text-sm font-semibold">{outcome.executed ? "Done" : "Agreed - nothing has been done yet"}</p>
          {outcome.needs && !outcome.executed && <Muted>{p(outcome.needs)}</Muted>}
          {mine ? surface.action({ chat, outcome, markDone }) : <Muted>Only {chat.created_by} can take the next step from this preview.</Muted>}
        </div>
      )}
    </div>
  );
}
