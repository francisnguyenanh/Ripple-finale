// Reviewer notes on ONE proposal: a comment, "ask for changes" or "ready for approval". A reviewer reads and writes here; a note
// approves and applies nothing. An open "ask for changes" stops the approval of THIS proposal until the reviewer marks it ready
// (or the requester sends a revised proposal); the server enforces it, this card only shows it.
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, TextArea } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useSession } from "@/session";
import { useFlow } from "./flow";

export interface Note { note_id: number; reviewer: string; verdict: "comment" | "request_changes" | "ready"; text: string; created_at: string }
export interface Notes { notes: Note[]; open_change_requests: string[] }

const WORDS: Record<Note["verdict"], string> = { comment: "commented", request_changes: "asked for changes", ready: "marked it ready for approval" };

/** The reviewer notes of one proposal, read again when a decision lands. Null while unread or unreadable. */
export function useReviewNotes(proposalId?: string): { st: Notes | null; set: (n: Notes) => void } {
  const { ws, data } = useFlow();
  const [st, setSt] = React.useState<Notes | null>(null);
  const marker = data ? `${data.decisions.length}` : "";
  React.useEffect(() => {
    if (!proposalId) { setSt(null); return; }
    let live = true;
    api<Notes>("GET", `/v2/changes/${encodeURIComponent(proposalId)}/review-notes?workspace_id=${encodeURIComponent(ws)}`)
      .then((x) => { if (live) setSt(x); }).catch(() => { if (live) setSt(null); });
    return () => { live = false; };
  }, [ws, proposalId, marker]);
  return { st, set: setSt };
}

export function ReviewNotes({ proposalId, proposalBy, st, set }: { proposalId: string; proposalBy?: string | null; st: Notes | null; set: (n: Notes) => void }) {
  const { ws } = useFlow();
  const s = useSession();
  const { run, pending } = useAction();
  const [text, setText] = React.useState("");
  if (!st) return null;
  const mayReview = s.can("change.review") && proposalBy !== s.account;
  const post = async (verdict: Note["verdict"]) => {
    if (verdict !== "ready" && !text.trim()) return;
    const r = await run(() => api<Notes>("POST", "/v2/changes/review-note", { workspace_id: ws, proposal_job_id: proposalId, verdict, text: text.trim() }));
    if (r) { set(r); setText(""); }
  };
  return (
    <div data-testid="review-notes" className="grid gap-2 rounded-md border border-border p-3">
      <h3 className="font-semibold">Reviewer notes</h3>
      {st.open_change_requests.length > 0 && (
        <Callout tone="warn" testid="review-open-request">
          {st.open_change_requests.join(", ")} asked for changes. This proposal cannot be approved until they mark it ready, or the requester sends a revised proposal.
        </Callout>
      )}
      {st.notes.length === 0 && <Muted testid="review-no-notes">No reviewer has written a note on this proposal. A review is optional; the approver can decide without one.</Muted>}
      <ul className="grid gap-1 text-sm">
        {st.notes.map((n) => (
          <li key={n.note_id} data-testid="review-note" data-verdict={n.verdict}>
            <strong>{n.reviewer}</strong> {WORDS[n.verdict]}{n.text ? `: ${n.text}` : ""} <span className="text-muted">{new Date(n.created_at).toLocaleString()}</span>
          </li>
        ))}
      </ul>
      {mayReview && (
        <div className="grid gap-2">
          <Field label="Your review note"><TextArea data-testid="review-note-text" rows={2} maxLength={1000} value={text} onChange={(e) => setText(e.target.value)} /></Field>
          <div className="flex flex-wrap gap-2">
            <Button data-testid="review-comment" title={!text.trim() ? "Write what you found first." : undefined} disabled={pending || !text.trim()} onClick={() => void post("comment")}>Add comment</Button>
            <Button data-testid="review-request-changes" title={!text.trim() ? "Say what has to change first." : undefined} disabled={pending || !text.trim()} onClick={() => void post("request_changes")}>Ask for changes</Button>
            <Button variant="primary" data-testid="review-ready" disabled={pending} onClick={() => void post("ready")}>Mark ready for approval</Button>
          </div>
          <Muted>A review note does not approve or apply anything. It is recorded with your name on this exact proposal.</Muted>
        </div>
      )}
    </div>
  );
}
