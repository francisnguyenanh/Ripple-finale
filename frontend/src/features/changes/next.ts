// Who has to do what next, computed from the real server state. "You" only where this account
// may really do it. Pure functions: unit-tested without a browser.
import { stageNo } from "./registry";
import { failureAdvice } from "@/lib/reasons";
import { intentPhrase } from "@/lib/fmt";
import type { FlowData, Job } from "./types";
import { isDone, jobState, phase } from "./types";

export interface Next { who: string; text: string; kind: "act" | "wait" | "stop" }
const A = (who: string, text: string, kind: Next["kind"] = "act"): Next => ({ who, text, kind });

export interface Actor { account: string; can: (p: string) => boolean; roles?: string[] }

/** An administrator holds the approver permissions but is not ASKED to do approver steps (the same rule as My work on the server): it is not shown as the one who acts. */
export function asksMe(me: Actor, perm: string): boolean {
  const adminOnly = !!me.roles && me.roles.includes("admin") && !me.roles.includes("change_approver");
  return me.can(perm) && !(adminOnly && (perm === "change.approve" || perm === "verification.contract.set"));
}

const iAsked = (me: Actor, job: Job | null) => !!job && !!job.requested_by && job.requested_by === me.account;

export function nextAction(d: FlowData, me: Actor): Next {
  const it = d.intent;
  const chat = d.chat && !d.chat.unavailable ? d.chat : null;
  if (it && chat && chat.status === "OPEN" && chat.intent_id === it.intent_id)
    return A("You", "You are changing the request. Edit the details or answer in the chat, then confirm the new understanding. Until then the saved request below stays as it is.");
  if (!it && chat) {
    if (chat.status === "CANCELLED") return A("You", "This conversation was cancelled. Start a new request.", "stop");
    if (chat.awaiting_ai) return A("The AI", "is replying. This page updates by itself.", "wait");
    if (!chat.you_are_creator) return A("The requester", "must answer the AI or confirm the understanding.", "wait");
    if (chat.awaiting_allowance) {
      const why: Record<string, string> = {
        suspended: "AI use is switched off for your account in this workspace; the workspace administrator can switch it on.",
        no_permission: "your role does not include AI use; the workspace administrator can add it.",
        revoked: "the AI replies of this conversation were withdrawn. Edit the details yourself or start a new request.",
        quota_used: "this conversation used all its AI replies. Edit the details yourself or start a new request.",
        window_ended: "the time window an administrator set for this conversation has ended; a message does not extend it. Ask an administrator or start a new request.",
        sandbox_hold: "in this shared trial a different person has to allow each AI reply.",
      };
      const b = chat.ai_block ?? "";
      return b ? A("You", `The AI cannot answer: ${why[b] ?? "it cannot be used for this conversation right now."}`, "stop")
        : A("You", 'The AI has not been asked yet. Press "Ask the AI", or send your next message: it answers under your own AI permission.');
    }
    if (chat.understanding?.status === "UNSUPPORTED")
      return A("You", "This is not something Ripple can set up automatically (see the note in the chat). Nothing was started. You can reword it as a number change or stop here.", "stop");
    if (chat.understanding?.status === "READY") return A("You", 'Check the understanding in the chat and confirm it ("Yes, this is what I mean").');
    return A("You", "Answer the AI questions in the chat, or edit the details yourself.");
  }
  if (!it) return A("You", "Describe the change in the chat above, or fill in the form under Advanced input, and save it.");
  if (it.status === "NEEDS_CLARIFICATION") return A("You", `The request could not be saved yet: ${(it.questions ?? []).join(" ")} Correct it and save again.`, "stop");
  if (it.status === "PARSED") {
    const owner = !!it.created_by && it.created_by === me.account;
    if (owner && me.can("intent.confirm_own")) return A("You", 'Confirm your request ("Confirm intent"). Confirming records your decision; it changes no document.');
    if (me.can("knowledge.confirm")) return A("You (confirmer rights)", 'You may confirm this request ("Confirm intent"); it is normally confirmed by the person who made it. Confirming changes no document.');
    return A(it.created_by ?? "The requester", "must confirm the request. It is in their My work list.", "wait");
  }
  if (it.status !== "CONFIRMED") return A("You", `This request is ${intentPhrase(it.status).toLowerCase()}. Save a new one.`, "stop");

  const dr = d.draft;
  const G = d.gaps;
  if (!dr)
    return me.can("change.prepare")
      ? A("You", "Ask for the AI draft (\"Generate GenAI draft\"). It runs under your own AI permission; nobody has to allow it.")
      : A("A requester", "must ask for the AI draft.", "wait");
  if (phase(dr) === "PENDING_APPROVAL")
    return iAsked(me, dr)
      ? A("A second person", "has to allow this AI run: this workspace is a shared trial where a different person allows each AI run, so you cannot allow your own.", "wait")
      : me.can("source.import")
        ? A("You", 'Allow the AI run ("Approve this model call") so the draft can be produced.')
        : A("A person with permission", "must allow the AI run.", "wait");
  if (jobState(dr) === "FAILED") { const f = failureAdvice(dr.execution_reason); return A("You", `The draft failed. ${f.what} ${f.retryHelps ? `Use Try again in stage ${stageNo("draft")}.` : f.next}`, "stop"); }
  if (!isDone(dr)) return A("The system", "is producing the draft. This page updates by itself.", "wait");
  if (G?.stale && !d.apply) return A("You", "A decision was recorded after this draft, so it is out of date. Ask for a new draft: it will use the decision.", "stop");
  if (!d.impact) return A("You", 'Run the impact analysis ("Run impact analysis"). The relations between rules are computed for you if they are missing; no AI is used for that.');
  if (!isDone(d.impact)) return A("The system", "is running the impact analysis.", "wait");
  if (!d.simulation) return A("You", "Run the simulation to see what would change before anything is proposed.");
  if (!isDone(d.simulation)) return A("The system", "is running the simulation.", "wait");
  if (!d.proposal) return A("You", "Prepare the proposal from the draft: it shows the exact before/after of the file. Nothing is applied yet.");
  if (!isDone(d.proposal)) return A("The system", "is preparing the proposal.", "wait");
  if (!(d.contracts ?? []).length)
    return asksMe(me, "verification.contract.set") && !iAsked(me, d.proposal)
      ? A("You (approver)", "State the check after the change: the value that must be in the file once it is applied. Type it from the confirmed request above, not from the proposal.")
      : A("An approver (not the requester)", "must state the check after the change. It is in their My work list.", "wait");
  if (!d.review) return me.can("source.import") ? A("You", "Run the review.") : A("A requester", "must run the review.", "wait");
  if (!isDone(d.review)) return A("The system", "is running the review.", "wait");
  if (!d.approval)
    return me.can("source.import") ? A("You", "Prepare the approval bundle: it is what the approver will read and decide on.") : A("A requester", "must prepare the approval bundle.", "wait");
  if (!isDone(d.approval)) return A("The system", "is preparing the approval bundle.", "wait");
  const last = d.decisions.length ? d.decisions[d.decisions.length - 1] : null;
  if (!last)
    return asksMe(me, "change.approve") && !iAsked(me, d.proposal)
      ? A("You (approver)", "Read the before/after, the limits and what stays unresolved, then approve or reject with a reason. Approving applies nothing by itself.")
      : A("An approver (not the requester)", "must approve or reject the change. It is in their My work list.", "wait");
  if (last.action !== "approve")
    return A("You", `The change was ${last.action === "reject" ? "rejected" : "sent back for changes"} by ${last.actor_id}${last.reason ? ` ("${last.reason}")` : ""}. Prepare a new proposal if you still want it.`, "stop");
  if (!d.apply)
    return me.can("change.apply") ? A("You", "Apply the approved change. This is the first step that changes a document.") : A("The requester", "must apply the approved change.", "wait");
  if (jobState(d.apply) === "FAILED") return A("You", `Applying failed${d.apply.execution_reason ? ": " + failureAdvice(d.apply.execution_reason).what : ""} Nothing was marked as updated.`, "stop");
  const L = d.loop;
  const loopDone = !!L && !!L.stages && L.stages.reanalysis.completed === L.stages.reanalysis.total && L.stages.verification === "PASSED";
  if (!isDone(d.apply) || (L && L.status !== "COMPLETE" && !loopDone))
    return A("The system", "is applying the change and re-checking the documents. This page updates by itself.", "wait");
  return A("You", 'Read "Where this change stands": what was updated and what still needs a person. You can then ask a question on the new revision.');
}

export type StepState = "done" | "current" | "blocked" | "future";
export const STEPS = [
  { key: "request", label: "Request" },
  { key: "confirm", label: "Confirm" },
  { key: "analyse", label: "Analyse" },
  { key: "simulate", label: "Simulate" },
  { key: "approve", label: "Check and approve" },
  { key: "apply", label: "Apply" },
  { key: "verify", label: "Verify and update" },
] as const;

export function stepStates(d: FlowData): Record<(typeof STEPS)[number]["key"], StepState> {
  const it = d.intent;
  const last = d.decisions.length ? d.decisions[d.decisions.length - 1] : null;
  const L = d.loop;
  const done = {
    request: !!it,
    confirm: !!it && it.status === "CONFIRMED",
    analyse: isDone(d.draft) && isDone(d.impact) && (!d.gaps?.stale || !!d.apply),   // once applied, the draft is history, not "blocked"
    simulate: isDone(d.simulation),
    approve: !!last && last.action === "approve",
    apply: !!d.apply && isDone(d.apply),
    verify: !!L && !!L.stages && L.stages.reanalysis.completed === L.stages.reanalysis.total && L.stages.verification === "PASSED",
  };
  const blocked: Partial<Record<keyof typeof done, boolean>> = {
    confirm: !!it && it.status === "NEEDS_CLARIFICATION",
    analyse: (!!d.draft && jobState(d.draft) === "FAILED") || (!!d.gaps?.stale && !d.apply),
    approve: !!last && last.action !== "approve",
    apply: !!d.apply && jobState(d.apply) === "FAILED",
  };
  const out = {} as Record<(typeof STEPS)[number]["key"], StepState>;
  let currentSet = false;
  for (const s of STEPS) {
    if (done[s.key]) out[s.key] = "done";
    else if (!currentSet) { out[s.key] = blocked[s.key] ? "blocked" : "current"; currentSet = true; }
    else out[s.key] = "future";
  }
  return out;
}
