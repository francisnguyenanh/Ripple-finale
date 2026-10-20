import * as React from "react";
import { Copy } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, copyText, Muted, StatePill, Tech, TechField, TextInput } from "@/components/bits";
import { hashFor } from "@/lib/route";
import { humanize, intentPhrase } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useSession } from "@/session";
import { useFlow } from "./flow";
import { alignNext, nextAction } from "./next";
import { TaskViewContext, responsibility } from "@/features/request-assistant/taskView";

export { fmtForm } from "./fmtForm";
import { fmtForm, scopeGap } from "./fmtForm";
import type { Intent } from "./types";

export function RequestCard() {
  const { ws, data, shareLink, refresh } = useFlow();
  const { run, pending } = useAction();
  const [msg, setMsg] = React.useState("");
  const [link, setLink] = React.useState("");
  const box = React.useRef<HTMLInputElement>(null);
  const it = data?.intent ?? null;
  const chat = data?.chat && !data.chat.unavailable ? data.chat : null;
  const savedChat = chat && (chat as { conversation_id?: string }).conversation_id ? chat : null;      // a chat object without an id is nothing saved yet

  const copy = async () => {
    const url = shareLink();
    setLink(url);
    setMsg((await copyText(url))
      ? "Copied. This is optional: the next person finds this change in their own My work list."
      : "Your browser did not allow automatic copying. Select the link in the box and press Ctrl+C (Cmd+C on a Mac) to copy it.");
    box.current?.focus();
    box.current?.select();
  };
  const linkBox = (
    <TextInput ref={box} readOnly data-testid="request-link" aria-label="Link to this request (read-only, select to copy)" value={link || shareLink()} onFocus={(e) => e.currentTarget.select()} className="min-w-0 flex-1" />
  );
  const copyBtn = (
    <Button data-testid="copy-request-link" disabled={!it && !savedChat} onClick={copy}><Copy size={16} aria-hidden="true" />Copy request link</Button>
  );

  if (!it) {
    return (
      <Card title="Your request" testid="request-card" data={{ saved: "false" }}>
        <Muted>{savedChat ? "Your conversation is saved and stays in your My work list. Nobody has to allow the AI to reply. The link is optional (for example to open it in another window)." : "Nothing is saved yet. After you save the request it appears in My work for you and, at each step, for the person who has to act next. You never need to send a link."}</Muted>
        <div className="flex flex-wrap gap-2 items-center">{savedChat ? copyBtn : null}{savedChat && linkBox}</div>
        {savedChat && <p data-testid="copy-status" role="status" aria-live="polite" className="text-sm text-muted">{msg}</p>}
      </Card>
    );
  }
  const f = fmtForm(it);
  const shown = it.status === "PARSED" ? "DRAFT" : it.status;
  const canReopen = !!chat && chat.you_are_creator && chat.status === "CONFIRMED" && chat.intent_id === it.intent_id;
  return (
    <Card title="Your request" testid="request-card" data={{ saved: "true" }}>
      <p data-testid="request-summary" className="text-xl font-semibold">{f.title}: {f.from} <span aria-hidden="true">→</span><span className="sr-only">to</span> {f.to}</p>
      <Muted>Applies to: {f.scope}. Orders you expect it to cover: {f.applies}.</Muted>
      <ScopeGap it={it} />
      {((it.exclusions ?? []).length > 0 || !!it.effective_time) && (
        <Callout tone="warn" testid="request-recorded-only">
          <strong>Recorded only, not used by the draft or the simulation</strong>
          <span>{(it.exclusions ?? []).length > 0 ? `Exclusions you stated: ${(it.exclusions as unknown[]).map((x) => String(typeof x === "string" ? x : JSON.stringify(x))).join("; ")}. ` : ""}{it.effective_time ? `Effective date you stated: ${typeof it.effective_time === "string" ? it.effective_time : JSON.stringify(it.effective_time)}. ` : ""}Ripple changes one value; a person has to handle these parts.</span>
        </Callout>
      )}
      <p><StatePill state={shown} label={intentPhrase(shown)} testid="request-status" data={{ "server-status": it.status }} /></p>
      <TechField label="intent id" value={it.intent_id} />
      <TechField label="digest" value={it.digest ?? ""} />
      <TechField label="structured form" value={it.structured_form ?? {}} />
      <div className="flex flex-wrap gap-2 items-center">{copyBtn}{linkBox}</div>
      <p data-testid="copy-status" role="status" aria-live="polite" className="text-sm text-muted">{msg}</p>
      {canReopen && (
        <div className="flex flex-wrap gap-2 items-center">
          <Button data-testid="chat-reopen" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/chat/${encodeURIComponent(chat!.conversation_id)}/reopen`, { workspace_id: ws }))) await refresh(); }}>Change this request</Button>
          <Muted>The saved request stays as it is until you confirm the new one. Work already done for it becomes out of date.</Muted>
        </div>
      )}
      <Muted>The link gives no extra rights: whoever opens it can only do what their own account is allowed to do. It contains no password.</Muted>
    </Card>
  );
}

const KIND_WORDS: Record<string, string> = { "change.draft": "AI draft", impact: "Impact analysis", simulation: "Simulation", proposal: "Proposal", review: "Review", approval: "Approval", apply: "Applied change" };

/** What replaced this request / what this request replaced, as the SERVER recorded it (a reload, the Back button and another person see the same). */
export function SupersededCard() {
  const { ws, data } = useFlow();
  const L = data?.lineage;
  if (!L) return null;
  const newer = L.superseded_by as string | null;
  const work = ((newer ? L.stale_jobs : L.replaced_work) ?? []) as { kind: string; job_id: string }[];
  if (!newer && !L.supersedes) return null;
  const applied = newer ? L.applied : L.replaced_applied;
  return (
    <Card title={newer ? "This request was replaced by a newer one" : "This request replaced an earlier one"} testid="superseded-card" data={{ state: newer ? "replaced" : "replacement" }}>
      {newer ? (
        <p className="text-sm">The person who wrote it changed it and confirmed a new version. Work done for this version is out of date and is NOT reused. <a className="text-primary underline" data-testid="superseded-open-new" href={hashFor(ws, "changes", undefined, { intent: newer, chat: (L.conversation_id as string) || undefined })}>Open the newer request</a></p>
      ) : (
        <p className="text-sm">The previous request is out of date: it and the work done for it are not reused for this one. An administrator has to confirm this request, then the steps are done again from step 2.</p>
      )}
      {work.length > 0 ? (
        <ul className="list-disc pl-5 text-sm">
          {work.map((w) => <li key={w.job_id}>{KIND_WORDS[w.kind] ?? humanize(w.kind)} - <StatePill state="STALE" label="Out of date" testid="superseded-state" /><Tech> <code>{w.job_id}</code></Tech></li>)}
        </ul>
      ) : <Muted>Nothing had been started for the earlier version.</Muted>}
      {applied && <Callout tone="danger">A change was already applied for the earlier version. The newer request does not undo it; it starts from the current sources.</Callout>}
    </Card>
  );
}

/** The next step of the change, aligned with the server's task projection ("You" only where this person is asked now). `inline` = shown inside the task frame
 *  (the page's one responsibility summary); standalone only where no task frame is shown (a new request, or the projection could not be read). */
export function NextCard({ inline = false }: { inline?: boolean }) {
  const { data, waitExhausted, checkNow } = useFlow();
  const s = useSession();
  const shared = React.useContext(TaskViewContext);
  if (!data) return null;
  if (!inline && shared?.tv) return null;                                  // the task frame carries it
  const n = alignNext(nextAction(data, { account: s.account, can: s.can, roles: s.roles }), responsibility(shared?.tv));
  return (
    <div data-testid="next-action" data-who={n.who} data-kind={n.kind} data-inline={String(inline)}
      className={inline ? "grid gap-1 text-sm border-t border-border pt-2 mt-1" : `rounded-lg border bg-surface p-4 grid gap-2 ${n.kind === "wait" ? "border-info" : n.kind === "stop" ? "border-danger" : "border-primary"}`}>
      {inline && n.kind === "wait" ? null                                   /* waiting: the task frame above already names who holds the step */
        : inline ? <p className="text-sm" data-testid="next-how"><strong>How: </strong><span>{n.text}</span></p>
        : <p className="text-sm"><strong>{n.kind === "wait" ? "Waiting for: " : "Next: "}{n.who}</strong>{/^[A-Z"']/.test(n.text) ? ": " : " "}<span>{n.text}</span></p>}
      {n.kind === "wait" && (
        <>
          <Muted testid="wait-note">{waitExhausted ? 'Automatic checking has stopped. Press "Check now" to look again.' : "This page checks by itself every few seconds; you do not need to create the request again."}</Muted>
          <div><Button data-testid="check-now" onClick={checkNow}>Check now</Button></div>
        </>
      )}
    </div>
  );
}

/** Shown next to every decision on the request (confirm, approve): the stated scope does not name every case the requester expects it to cover. */
export function ScopeGap({ it, where }: { it: Intent; where?: string }) {
  const gap = scopeGap(it);
  if (!gap.length) return null;
  const f = fmtForm(it);
  return (
    <Callout tone="warn" testid="scope-gap">
      <strong>The scope does not cover everything you expect</strong>
      <span>The change applies to {f.scope}, but the request also expects it to cover: {gap.join(", ")}. Those cases would not change.{where ? ` ${where}` : ""}</span>
    </Callout>
  );
}
