// Free-text request: words -> (AI questions) -> checked understanding -> the person confirms -> an
// ordinary saved request. Nothing here approves, drafts or applies. The AI replies under the member's own
// AI permission (server-checked, limited per conversation); nobody grants a reply. Messages are rendered as TEXT only.
import { useParam } from "@/lib/param";
import * as React from "react";
import { Send } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, HowItWorks, Muted, TechField, TextArea, TextInput, usePlain } from "@/components/bits";
import { humanize, num } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { Permission, Turns } from "./ChatBits";
import { AIInterpretationPreview } from "@/components/decision/AIInterpretationPreview";
import { useFlow } from "./flow";
import type { Chat, Ids, Rec, StageKey, Understanding } from "./types";
import { STAGES } from "./types";

const FIELD_ORDER: [string, string][] = [
  ["subject", "What is changing"], ["scope", "Applies to"], ["unit", "What the amount measures"], ["currency", "Currency"],
  ["old_value", "Current value"], ["new_value", "New value"], ["applies_to", "Cases it should cover"],
];

function show(k: string, v: unknown): string | null {
  if (v === null || v === undefined || v === "") return null;
  if (Array.isArray(v)) return v.length ? v.join(", ") : null;
  if (typeof v === "object") {
    const e = Object.entries(v as Rec);
    return e.length ? e.map(([a, b]) => `${humanize(a).toLowerCase()}: ${b}`).join("; ") : null;
  }
  return k === "old_value" || k === "new_value" ? num(v) : String(v);
}

/** The preview shows what the interpretation produced (any fields), in a stable order; nothing is hardcoded to one rule. */
function UnderstandingCard({ chat, u }: { chat: Chat; u: Understanding }) {
  const p = usePlain();
  const fields = u.fields ?? null;
  const keys = fields ? [...FIELD_ORDER.map(([k]) => k).filter((k) => k in fields), ...Object.keys(fields).filter((k) => !FIELD_ORDER.some(([x]) => x === k) && k !== "target_candidate_id")] : [];
  const label = (k: string) => FIELD_ORDER.find(([x]) => x === k)?.[1] ?? humanize(k);
  const heading =
    u.status === "READY" ? "This is what I understood"
    : u.status === "CLARIFY" ? "I still need some details"
    : u.status === "UNSUPPORTED" ? "This is not something I can set up here"
    : "No usable reply";
  const others = (u.safety ?? []).filter((x) => !chat.safety_notes.some((y) => y.code === x.code));
  return (
    <AIInterpretationPreview testid="chat-understanding" status={u.status} version={String(chat.version)} heading={heading}
      fields={fields ? keys.map((k) => {
        const src = u.sources?.[k] as Rec | undefined;
        return { key: k, label: label(k), value: show(k, fields[k]), source: src ? (src.quote ? `from your words: "${src.quote}"` : "from the stored rule") : null };
      }) : null}
      assumptions={u.assumptions ?? []} ids={{ fields: "chat-fields", field: (k) => `chat-field-${k}`, source: (k) => `chat-source-${k}`, assumptions: "chat-assumptions" }}
      footer={<>
        <TechField label="understanding version" value={chat.version} />
        <TechField label="source" value={u.source ?? ""} />
        <TechField label="conversation id" value={chat.conversation_id} />
        <TechField label="pinned source revision" value={chat.pinned_source_revision_id ?? ""} />
        <TechField label="pinned decision epoch" value={chat.pinned_decision_epoch ?? ""} />
      </>}>
      {(u.questions ?? []).map((q, i) => <Callout key={i} tone="warn" testid="chat-question">{p(q)}</Callout>)}
      {(u.changes ?? []).length > 0 && (
        <div>
          <Muted>What I read in your message:</Muted>
          <ul data-testid="chat-changes" className="list-disc pl-5 text-sm grid gap-1">
            {u.changes!.map((c, i) => (
              <li key={i} data-kind={c.kind} data-supported={c.supported ? "true" : "false"}>
                {c.summary} - {c.supported ? "a number change Ripple can prepare" : "recorded only, not applied automatically"}
                {c.quote ? ` (your words: "${c.quote}")` : " (not in your words)"}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(u.out_of_scope ?? []).length > 0 && <Callout tone="warn" testid="chat-out-of-scope">Not covered by this request: {u.out_of_scope!.join("; ")}</Callout>}
      {u.limits_note && <Muted testid="chat-limits-note">{p(u.limits_note)}</Muted>}
      {others.map((h) => (
        <Muted key={h.code} testid="chat-safety"><span data-code={h.code}>{h.business}</span></Muted>
      ))}
    </AIInterpretationPreview>
  );
}

function EditDetails({ chat, u, onChanged }: { chat: Chat; u: Understanding | null; onChanged: () => Promise<void> }) {
  const { ws } = useFlow();
  const { run, pending } = useAction();
  const [open, setOpen] = React.useState(false);
  const seed = React.useMemo(() => {
    const f = (u?.fields ?? {}) as Rec;
    const [sk, sv] = Object.entries((f.scope ?? {}) as Rec)[0] ?? ["", ""];
    const s = (v: unknown) => (v === null || v === undefined ? "" : String(v));
    return { subject: s(f.subject), scopeKey: sk, scopeVal: s(sv), unit: s(f.unit), currency: s(f.currency), old: s(f.old_value), neu: s(f.new_value), applies: ((f.applies_to ?? []) as string[]).join(", ") };
  }, [u]);
  const [v, setV] = React.useState(seed);
  // Re-seed only when a NEW understanding version arrives, never on a poll with the same version.
  const version = `${chat.conversation_id}:${chat.version}`;
  const seen = React.useRef(version);
  React.useEffect(() => { if (seen.current !== version) { seen.current = version; setV(seed); } }, [version, seed]);
  const set = (k: keyof typeof seed) => (e: React.ChangeEvent<HTMLInputElement>) => setV((p) => ({ ...p, [k]: e.target.value }));
  const submit = async () => {
    const fields: Rec = {
      subject: v.subject.trim(), unit: v.unit.trim() || null, currency: v.currency.trim(), old_value: v.old.trim(), new_value: v.neu.trim(),
      applies_to: v.applies.split(",").map((x) => x.trim()).filter(Boolean),
    };
    if (v.scopeKey.trim() && v.scopeVal.trim()) fields.scope = { [v.scopeKey.trim()]: v.scopeVal.trim() };
    if (await run(() => api("POST", `/v2/chat/${encodeURIComponent(chat.conversation_id)}/edit`, { workspace_id: ws, fields }))) { setOpen(true); await onChanged(); }
  };
  return (
    <details data-testid="chat-edit" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium">Edit the details myself</summary>
      <div className="grid gap-3 mt-3 sm:grid-cols-2">
        <Field label="What is changing"><TextInput data-testid="ce-subject" value={v.subject} onChange={set("subject")} /></Field>
        <Field label="What the amount measures"><TextInput data-testid="ce-unit" value={v.unit} onChange={set("unit")} /></Field>
        <Field label="Applies to - kind of scope"><TextInput data-testid="ce-scope-key" value={v.scopeKey} onChange={set("scopeKey")} /></Field>
        <Field label="Applies to - which one"><TextInput data-testid="ce-scope-val" value={v.scopeVal} onChange={set("scopeVal")} /></Field>
        <Field label="Currency"><TextInput data-testid="ce-currency" value={v.currency} onChange={set("currency")} /></Field>
        <Field label="Current value"><TextInput data-testid="ce-old" value={v.old} onChange={set("old")} /></Field>
        <Field label="New value"><TextInput data-testid="ce-new" value={v.neu} onChange={set("neu")} /></Field>
        <Field label="Cases it should cover (comma separated)"><TextInput data-testid="ce-applies" value={v.applies} onChange={set("applies")} /></Field>
      </div>
      <div className="mt-3"><Button data-testid="chat-edit-apply" disabled={pending} onClick={submit}>Check my edits</Button></div>
    </details>
  );
}

/** Why the AI has not answered, in the server's words, and the one thing that can be done about it. */
function AiHeld({ chat }: { chat: Chat }) {
  const { ws, refresh } = useFlow();
  const s = useSession();
  const { run, pending } = useAction();
  const mine = chat.you_are_creator;
  const why: Record<string, string> = {
    suspended: "AI use is switched off for your account in this workspace. The workspace administrator can switch it on again (Support & Access, Members).",
    no_permission: "Your role in this workspace does not include AI use. The workspace administrator can add it.",
    revoked: "The AI replies of this conversation were withdrawn. Edit the details yourself, or start a new request.",
    quota_used: "This conversation has used all of its AI replies. Edit the details yourself, or start a new request.",
    window_ended: "The time window an administrator set for this conversation has ended. A new message does not extend it. Ask an administrator, or start a new request.",
    sandbox_hold: "In this shared trial workspace a different person has to allow each AI reply.",
  };
  const block = chat.ai_block ?? null;
  const ask = async () => { if (await run(() => api("POST", `/v2/chat/${encodeURIComponent(chat.conversation_id)}/allow`, { workspace_id: ws, turns: 1 }))) await refresh(); };
  return (
    <Callout tone="wait" testid="chat-awaiting-allowance">
      <strong>The AI has not been asked yet</strong>
      {block ? <span data-testid="chat-ai-block" data-reason={block}>{why[block] ?? "The AI cannot be used for this conversation right now."} Nothing was sent to the AI.</span>
        : s.aiOff ? <span data-testid="chat-ai-block" data-reason="provider_off">AI actions are switched off on this server, so no reply was requested. Saved results can still be read. Nothing was sent to the AI.</span>
        : !s.aiKind("intent.interpret") ? <span data-testid="chat-ai-block" data-reason="role_not_enabled">AI interpretation of chat messages is not enabled on this server, so no reply was requested. The conversation is kept. Nothing was sent to the AI.</span>
        : <span>Nothing was sent to the AI yet. It answers when you send a message, or press the button.</span>}
      {mine && !block && (
        <div className="flex flex-wrap gap-2 mt-1">
          <Button variant="primary" data-testid="chat-allow" title={aiWhy(s, "intent.interpret")} disabled={pending || !s.aiKind("intent.interpret")} onClick={ask}>Ask the AI</Button>
        </div>
      )}
      {!mine && block === "sandbox_hold" && chat.can_allow && (
        <div className="flex flex-wrap gap-2 mt-1">
          <Button variant="primary" data-testid="chat-allow-other" title={aiWhy(s, "intent.interpret")} disabled={pending || !s.aiKind("intent.interpret")}
            onClick={async () => { if (await run(() => api("POST", `/v2/chat/${encodeURIComponent(chat.conversation_id)}/allow`, { workspace_id: ws, turns: 1 }))) await refresh(); }}>Allow the AI to reply</Button>
        </div>
      )}
    </Callout>
  );
}

export function ChatPanel() {
  const { ws, ids, data, setIds, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const C = data?.chat ?? null;
  const [proposed] = useParam("rqs");
  const [first, setFirst] = React.useState(proposed || "");
  React.useEffect(() => { if (proposed) setFirst((cur: string) => cur || proposed); }, [proposed]);
  const [reply, setReply] = React.useState("");
  const [ack, setAck] = React.useState(false);
  const limit = C && !C.unavailable ? C.limits.max_user_turn_chars : 2000;
  const call = (cid: string, path: string, body: Rec) => api<Chat & { intent_id?: string }>("POST", `/v2/chat/${encodeURIComponent(cid)}${path}`, { workspace_id: ws, ...body });

  const head = (
    <>
      <h3 className="text-base font-semibold">Describe the change in your own words</h3>
      <Muted>
        Write it the way you would tell a colleague. The AI helps turn it into a checked request. It never changes a document, and nothing starts until you confirm below and then confirm the saved request.
      </Muted>
      <Callout testid="chat-scope-note">
        <strong>What happens with your request</strong>
        <span>
          Ripple sets up and applies changes to the NUMBER of an existing rule (a limit, a threshold). A request about scope, approvers, conditions or exceptions is only NOTED: it is written down here and nothing is carried out for it.
        </span>
        <Muted testid="chat-privacy">
          Who can read this conversation: you, and people who hold the knowledge-confirmer permission (normally the workspace administrator). Reviewers, approvers and viewers cannot open it; what they see of your request is the saved request and its proposal.
        </Muted>
        <HowItWorks testid="chat-how" title="Who does which step"><Muted>
          You do these yourself: chat with the AI, confirm what you mean, confirm your saved request, ask for the draft and the analysis. Steps that need ANOTHER person: stating the check after the change and approving the change (an approver), and the optional review. You cannot approve your own change.
        </Muted></HowItWorks>
      </Callout>
    </>
  );

  if (C?.unavailable) {
    return (
      <div data-testid="chat-panel" data-state="unavailable" className="grid gap-3">
        {head}
        <Callout tone="warn" testid="chat-unavailable">This conversation is not available to your account (it may belong to someone else, or it does not exist). Nothing was changed.</Callout>
      </div>
    );
  }

  if (!C) {
    const start = async () => {
      const text = first.trim();
      if (!text) { notify.status("Write what you want to change first."); return; }
      const r = await run(() => api<Chat>("POST", "/v2/chat/start", { workspace_id: ws, text }));
      if (!r) return;
      setFirst("");
      setIds({ chat: r.conversation_id });
      await refresh();
    };
    return (
      <div data-testid="chat-panel" data-state="new" className="grid gap-3">
        {head}
        <TextArea data-testid="chat-input" rows={3} aria-label="Tell Ripple what you want to change" maxLength={limit} value={first} onChange={(e) => setFirst(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void start(); } }} />
        <div className="flex items-center gap-3">
          <Button variant="primary" data-testid="chat-start" disabled={pending} onClick={start}><Send size={16} aria-hidden="true" />Send</Button>
          <span className="text-xs text-muted tnum">{first.length} / {limit}</span>
          <span className="text-xs text-muted">Enter sends, Shift+Enter adds a line.</span>
        </div>
      </div>
    );
  }

  const mine = C.you_are_creator;
  const open = C.status === "OPEN";
  const u = C.understanding;
  const state = C.status + (C.awaiting_ai ? "-ai" : C.awaiting_allowance ? "-allow" : "");
  const ackNeeded = !!u && ((u.assumptions ?? []).length > 0 || (u.out_of_scope ?? []).length > 0);

  const send = async () => {
    const text = reply.trim();
    if (!text) { notify.status("Write your answer first."); return; }
    if (await run(() => call(C.conversation_id, "/message", { text }))) { setReply(""); await refresh(); }
  };
  const confirm = async () => {
    if (ackNeeded && !ack) { notify.status("Tick the box to confirm you checked the assumptions and what is left out."); return; }
    const r = await run(() => call(C.conversation_id, "/confirm", { version: C.version, acknowledged: ackNeeded && ack }));
    if (!r) return;
    const cleared: Ids = {};
    if (ids.intent && ids.intent !== r.intent_id) {
      // A new understanding replaces a saved request: the server records which request replaced which and which work is out of date.
      for (const k of ["draft", "query", "whatif", ...STAGES] as StageKey[]) cleared[k] = "";
    }
    setIds({ ...cleared, intent: r.intent_id ?? undefined });
    setAck(false);
    await refresh();
  };

  return (
    <div data-testid="chat-panel" data-state={state} className="grid gap-3">
      {head}
      <Turns chat={C} account={s.account} />
      {C.safety_notes.map((h) => <Muted key={h.code} testid="chat-safety-note"><span data-code={h.code}>{h.business}</span></Muted>)}
      {u && <UnderstandingCard chat={C} u={u} />}
      <Permission chat={C} ws={ws} open={open} canRevoke={C.can_allow || mine || s.can("knowledge.confirm")} onChanged={refresh} />
      {C.awaiting_allowance && open && <AiHeld chat={C} />}
      {mine && open && (
        <>
          <Field label={C.awaiting_ai ? "Your next message (you can send it when the AI has replied)" : u?.status === "CLARIFY" ? "Your answer" : "Add something"}>
            <TextArea data-testid="chat-reply" rows={2} maxLength={limit} value={reply} onChange={(e) => setReply(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!C.awaiting_ai) void send(); } }} />
          </Field>
          <div className="flex items-center gap-3">
            <Button variant="primary" data-testid="chat-send" disabled={pending || C.awaiting_ai} onClick={send}><Send size={16} aria-hidden="true" />Send</Button>
            <span className="text-xs text-muted tnum">{reply.length} / {limit}</span>
          </div>
          {!C.awaiting_ai && <EditDetails chat={C} u={u} onChanged={refresh} />}
        </>
      )}
      {mine && open && (
        <>
          {ackNeeded && (
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" data-testid="chat-ack" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-1" />
              I have checked what the AI assumed and what is left out of this request
            </label>
          )}
          <div className="flex flex-wrap gap-2">
            {u?.status === "READY" && <Button variant="primary" data-testid="chat-confirm" disabled={pending || C.awaiting_ai} onClick={confirm}>Yes, this is what I mean</Button>}
            <Button data-testid="chat-cancel" disabled={pending} onClick={async () => { if (await run(() => call(C.conversation_id, "/cancel", {}))) await refresh(); }}>Cancel this conversation</Button>
          </div>
          <Muted>Confirming here only saves your request. It is not an approval. You confirm the saved request next (Confirm intent) before anything is analysed.</Muted>
        </>
      )}
      {!mine && <Muted testid="chat-not-yours">This conversation belongs to {C.created_by}. Only they can answer, edit or confirm it.</Muted>}
      {!open && <Muted testid="chat-closed">This conversation is {C.status.toLowerCase()}.</Muted>}
    </div>
  );
}

export function ChatCard() {
  return <Card title="Request" testid="flow-intent-chat"><ChatPanel /></Card>;
}
