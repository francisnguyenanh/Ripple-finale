// The whole journey of ONE change, read from server data only (never from browser state): which step it is at, who has to act, what they do next,
// why something waits or is blocked, and which steps are done, failed, to be redone or only partly complete.
// One registry gives every stage its number, so the numbers can never skip (the old hard-coded titles jumped from 6 to 8).
// Pure functions: unit-tested without a browser. The wording of "who acts now" comes from `nextAction` (one source of truth for the current step).
import { fmtForm } from "./fmtForm";
import { nextAction, type Actor } from "./next";
import type { FlowData, Job } from "./types";
import { isDone, isTerminal, jobState } from "./types";

import { JOURNEY, stageNo, stageTitle, type StepKey } from "./registry";
export { JOURNEY, stageNo, stageTitle };
export type { StepKey };

export type JState = "done" | "current" | "waiting" | "blocked" | "failed" | "redo" | "partial" | "todo";
export interface JStep {
  key: StepKey; n: number; title: string; target: string; state: JState;
  /** Who has to act (or "Nobody" when it is finished). A role when no person is assigned: names are never invented. */
  who: string;
  /** What happens or what to do, or why it waits. */
  text: string;
  /** This account may act on it right now. */
  mine: boolean;
  /** Work that can run in parallel with the current step (for example optional reviewer notes). */
  also?: string;
}

const last = <T,>(a: T[]): T | null => (a.length ? a[a.length - 1] : null);

/** The facts of the data that decide each step. Kept separate so the tests can state them. */
export function facts(d: FlowData) {
  const dec = last(d.decisions);
  const L = d.loop;
  const cs = d.change;
  const named = !!d.review?.payload?.verification_contract_id;
  const contractsOk = (d.contracts ?? []).length > 0 && !d.contractsStale;
  const approvalCurrent = (() => {
    const snap = (d.approval?.execution_result?.snapshot ?? {}) as Record<string, unknown>;
    return !!snap.source_revision_id && snap.source_revision_id === d.head.source_revision_id && snap.decision_epoch === d.head.decision_epoch;
  })();
  const refreshDone = !!L && !!L.stages && L.stages.reanalysis.completed === L.stages.reanalysis.total && L.stages.verification === "PASSED";
  const resultDecided = !!cs?.dto?.completion?.decision;
  return {
    request: !!d.intent && d.intent.status === "CONFIRMED",
    draft: isDone(d.draft),
    analysis: isDone(d.impact) && isDone(d.simulation),
    proposal: isDone(d.proposal),
    check: contractsOk,
    review: isDone(d.review) && named,
    approval: !!d.approval && isDone(d.approval) && !!dec && dec.action === "approve" && (approvalCurrent || !!d.apply),         // after the apply the head moved because of it
    apply: !!d.apply && isDone(d.apply),
    refresh: refreshDone || cs?.dto?.state === "COMPLETED" || (!!cs && ["MACHINE_VERIFIED", "AWAITING_ACCEPTANCE"].includes(cs.dto.state)),
    accept: resultDecided,
    query: isDone(d.query),
    dec, named, approvalCurrent,
  };
}

export function activeKey(d: FlowData): StepKey | null {
  const f = facts(d);
  for (const s of JOURNEY) if (!f[s.key]) return s.key;
  return null;
}

function doneText(key: StepKey, d: FlowData): string {
  const f = facts(d);
  switch (key) {
    case "request": { const q = d.intent ? fmtForm(d.intent) : null; return q ? `Confirmed: ${q.title}, from ${q.from} to ${q.to}.` : "Confirmed."; }
    case "draft": {
      const r = (d.draft?.execution_result ?? {}) as Record<string, any>;
      const open = (d.gaps?.gaps ?? []).filter((g) => g.state !== "confirmed").length;
      return r.status === "PARTIAL" || open ? `Done, partly processed: ${open} open point(s) and ${(r.not_covered ?? []).length} source(s) the draft did not cover stay with people.` : "Done.";
    }
    case "analysis": { const h = (d.simulation?.execution_result?.harbor ?? null) as Record<string, any> | null; return h ? `Done: ${h.changed_count} of ${h.transactions} sample cases change their outcome.` : "Done (no behaviour simulation backs it: that is not a no-impact result)."; }
    case "proposal": return "Prepared: the exact before/after of the file is shown. Nothing is applied yet.";
    case "check": return `Stated by ${Array.from(new Set((d.contracts ?? []).map((c) => c.author_account_id))).join(", ")}: the value the file must hold after the change.`;
    case "review": { const checks = Object.values((d.review?.execution_result?.checks ?? {}) as Record<string, any>); return `Done: ${checks.filter((c) => c.status === "PASS").length} of ${checks.length} checks passed.`; }
    case "approval": return `Approved by ${f.dec?.actor_id}${f.dec?.reason ? ` ("${f.dec.reason}")` : ""}. Approving applied nothing by itself.`;
    case "apply": {
      const rest = [(d.intent?.exclusions ?? []).length ? "the exclusions" : "", d.intent?.effective_time ? "the effective date" : ""].filter(Boolean);
      return `Applied: a new revision of the source was published (the numeric change only).${rest.length ? ` Recorded only, NOT applied: ${rest.join(" and ")}.` : ""}`;
    }
    case "refresh": return d.change && d.change.dto.state === "COMPLETED" ? "Verified and the knowledge was updated." : "Verification and the knowledge update are finished; the result still needs a human decision.";
    case "accept": return `Decided by ${d.change?.dto?.completion?.decision?.actor_id ?? "a person"}${d.change?.dto?.completion?.decision?.acceptance_scope === "NARROW" ? ", in a NARROW scope: the change itself only, some items left unhandled" : ""}.`;
    case "query": return "Answered on the new revision.";
  }
}

const WHO_DEFAULT: Record<StepKey, string> = {
  request: "The requester", draft: "The requester", analysis: "The requester", proposal: "The requester",
  check: "An approver (not the requester)", review: "The requester runs it; a reviewer may add notes", approval: "An approver (not the requester)",
  apply: "A person with apply permission (the requester or an administrator)", refresh: "The system",
  accept: "A person with permission to accept results (an approver or an administrator; the knowledge candidate is accepted by a knowledge owner)", query: "The requester",
};
const TODO_TEXT: Record<StepKey, string> = {
  request: "Describe the change and confirm it.", draft: "Needs the confirmed request first.", analysis: "Needs the AI draft first.", proposal: "Needs the simulation first.",
  check: "Needs a proposal first.", review: "Needs the check after the change first.", approval: "Needs a finished review first.", apply: "Needs a recorded approval first.",
  refresh: "Starts by itself after the change is applied.", accept: "Needs finished verification first.", query: "Possible after the change is applied.",
};

/** Every step with its state. The active step carries the "who acts, what, why" of `nextAction`; the rest say what they are or what they wait for. */
export function journey(d: FlowData, me: Actor): JStep[] {
  const f = facts(d);
  const active = activeKey(d);
  const n = nextAction(d, me);
  const out: JStep[] = [];
  let seenActive = false;
  JOURNEY.forEach((s, i) => {
    const base = { key: s.key, n: i + 1, title: s.title, target: s.target };
    if (active === s.key) {
      seenActive = true;
      const failedJob: Partial<Record<StepKey, Job | null>> = { draft: d.draft, apply: d.apply, review: d.review, approval: d.approval };
      const failed = !!failedJob[s.key] && jobState(failedJob[s.key] as Job) === "FAILED";
      let state: JState = n.kind === "wait" ? "waiting" : n.kind === "stop" ? (failed ? "failed" : "blocked") : "current";
      let text = n.text;
      // a step that was done once and no longer applies is "to redo", not "to do"
      if (s.key === "check" && d.contractsStale) { state = "redo"; text = `The check was stated for an earlier revision (${d.contractsStale}). It no longer applies: confirm the check again for the new proposal.`; }
      if (s.key === "review" && (d.contracts ?? []).length && isDone(d.review) && !f.named) { state = "redo"; text = "The review ran before the check was stated, so it tested nothing against it. Run the review again."; }
      if (s.key === "approval" && d.approval && isDone(d.approval) && !f.approvalCurrent) { state = "redo"; text = "The files changed after the approval bundle was prepared. Prepare a new bundle; the old one cannot be approved."; }
      let who = n.who, mine = n.kind === "act" && /^You/.test(n.who);
      if (s.key === "refresh") { state = "waiting"; who = "The system"; mine = false; text = "is verifying the applied change and re-reading the sources it touches. This page updates by itself; nothing is reported as finished until that ends."; }
      if (s.key === "accept") {
        const km = d.change?.dto?.knowledge;
        const kmPending = !!km && !km.accepted && !!km.candidate_revision_id;
        const tail = kmPending ? ' First a knowledge owner presses "Accept knowledge candidate"; then a person with result permission presses "Accept result" (or asks for changes / rejects).' : ' A person with result permission presses "Accept result" (or asks for changes / rejects).';
        if (me.can("change.accept_result") || (kmPending && me.can("knowledge.confirm"))) { state = "current"; who = "You"; mine = true; text = `Read the verification result below.${tail} The change is not complete until a person decides.`; }
        else { state = "waiting"; who = WHO_DEFAULT.accept; mine = false; text = `must read the verification result and decide.${tail} The change is not complete until then.`; }
      }
      if (s.key === "query") {
        if (me.can("source.import")) { state = "current"; who = "You"; mine = true; text = "Ask a question on the new revision to see it answer from the updated sources (optional)."; }
        else { state = "waiting"; who = WHO_DEFAULT.query; mine = false; text = "may ask a question on the new revision (optional)."; }
      }
      const notes = d.notes?.open_change_requests ?? [];
      if (s.key === "approval" && notes.length) { state = "blocked"; who = "A reviewer"; mine = false; text = `(${notes.join(", ")}) asked for changes. The approval waits until that reviewer marks it ready or the requester prepares a new proposal.`; }
      out.push({ ...base, state, who, text, mine,
        also: (s.key === "review" || s.key === "approval") && !notes.length ? "Optional, in parallel: a reviewer may read the proposal and add notes." : undefined });
    } else if (!seenActive) {
      const dr = d.draft && (d.draft.execution_result as Record<string, any> | null)?.status === "PARTIAL";
      const partialRefresh = s.key === "refresh" && !!d.change && d.change.dto.state !== "COMPLETED" && d.change.dto.completion?.status !== "COMPLETED" && !!d.change.dto.alignment && d.change.dto.alignment.global !== "ALIGNED";
      out.push({ ...base, state: dr && s.key === "draft" ? "partial" : partialRefresh ? "partial" : "done", who: "Nobody", text: doneText(s.key, d), mine: false });
    } else {
      out.push({ ...base, state: "todo", who: WHO_DEFAULT[s.key], text: TODO_TEXT[s.key], mine: false });
    }
  });
  return out;
}

/** True while the change is running something on the server or waiting for a person (the page keeps reading). */
export const journeyOpen = (d: FlowData): boolean => activeKey(d) !== null || (!!d.apply && !isTerminal(d.apply));
