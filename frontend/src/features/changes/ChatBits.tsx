// Parts of a conversation that every free-text surface shows the same way: the turns (text only) and the AI permission.
import { Bot, ShieldCheck, User } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Muted, TechField } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import type { Chat } from "./types";

export function Turns({ chat, account }: { chat: Chat; account: string }) {
  return (
    <ol data-testid="chat-turns" className="grid gap-2" aria-label="Conversation">
      {chat.turns.map((t) => {
        const pend = t.role === "assistant" && !t.text;
        const who = t.role === "user" ? (t.by === account ? "You" : t.by ?? "Someone") : t.role === "assistant" ? "AI" : t.role === "edit" ? "You (edited the details)" : "Note";
        const Icon = t.role === "assistant" ? Bot : User;
        return (
          <li key={t.seq} data-testid="chat-turn" data-role={t.role} data-seq={String(t.seq)} data-pending={pend ? "true" : "false"}
            className={`rounded-md px-3 py-2 text-sm flex gap-2 ${t.role === "assistant" ? "bg-surface-2" : t.role === "system" ? "border border-border text-muted" : "border border-border"}`}>
            <Icon size={16} aria-hidden="true" className="mt-0.5 shrink-0" />
            <span className="min-w-0 break-words"><strong>{who}: </strong><span>{pend ? "The AI is replying..." : t.text}</span></span>
          </li>
        );
      })}
    </ol>
  );
}

export function Permission({ chat, ws, open, canRevoke, onChanged }: { chat: Chat; ws: string; open: boolean; canRevoke: boolean; onChanged: () => Promise<void> }) {
  const { run, pending } = useAction();
  const Z = chat.authorization;
  const expires = Z.expires_at ? new Date(Z.expires_at).toLocaleTimeString() : "";
  return (
    <div data-testid="chat-authorization" data-live={Z.live ? "true" : "false"} className={`rounded-md border px-3 py-2 text-sm grid gap-1 ${Z.live ? "border-success" : "border-info"}`}>
      <p className="font-semibold flex items-center gap-2">
        <ShieldCheck size={16} aria-hidden="true" />
        {Z.live ? `AI replies for this conversation: ${Z.replies_left} more ${Z.replies_left === 1 ? "reply" : "replies"} available` : Z.revoked_at ? "AI replies withdrawn for this conversation" : "No AI reply is available right now"}
      </p>
      <p>
        {Z.live
          ? `${Z.granted_by?.startsWith("policy:") ? "Your AI permission in this workspace covers it" : `Allowed by ${Z.granted_by}`}, until ${expires}. ${Z.covers}`
          : `${chat.ai_block === "quota_used" ? "All the AI replies of this conversation are used" : "The AI is asked when you send a message, within your AI permission and this conversation's limit"}. Replies used so far: ${chat.ai_used}.`}
      </p>
      <Muted testid="chat-windows">Three separate limits: your AI permission (your role; an administrator can switch it off), this conversation (its replies and its time window, which your own new message renews while you hold the permission), and the server (armed or not, and its allowance: a message never extends these).</Muted>
      <Muted testid="chat-not-covered">This does NOT cover: {(Z.does_not_cover ?? []).join(", ")}. Those stay separate steps with their own people.</Muted>
      <TechField label="replies used/allowed" value={`${chat.ai_used}/${chat.ai_allowance} (max ${chat.limits.max_ai_turns})`} />
      <TechField label="reserved USD per reply (worst case)" value={Z.reserved_usd_per_reply ?? "n/a"} />
      <TechField label="worst case USD left" value={Z.worst_case_usd_left ?? "n/a"} />
      <TechField label="expires at" value={Z.expires_at ?? ""} />
      <TechField label="revoked by" value={Z.revoked_by ?? ""} />
      {Z.live && open && canRevoke && (
        <div>
          <Button data-testid="chat-revoke" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/chat/${encodeURIComponent(chat.conversation_id)}/revoke`, { workspace_id: ws }))) await onChanged(); }}>
            Withdraw the AI replies of this conversation
          </Button>
        </div>
      )}
    </div>
  );
}
