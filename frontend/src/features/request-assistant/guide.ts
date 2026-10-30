// EUI-02 guided assistant: suggested questions and typed action targets. Pure; computed from the task view this reader already received (no hidden fact, no model call).
type Rec = Record<string, unknown>;

export interface Suggestion { id: string; question: string }

/** Up to four questions grounded in what the task view shows THIS reader: their current step, a blocker, who is waited on, then the request's purpose. */
export function suggestQuestions(tv: Rec | null | undefined, asked: string[] = []): Suggestion[] {
  if (!tv) return [];
  const out: Suggestion[] = [];
  const mine = ((tv.obligations ?? []) as Rec[]).filter((o) => o.obligation_me && o.state === "current");
  const eligible = ((tv.obligations ?? []) as Rec[]).filter((o) => !o.obligation_me && o.eligible_me && o.state === "current");
  if (mine.length) out.push({ id: "my_step", question: `What do I need to check before I ${lower(String(mine[0].label))}?` });
  else if (eligible.length) out.push({ id: "eligible", question: `Should I act on "${String(eligible[0].label)}", or is someone else expected to?` });
  if (((tv.blockers ?? []) as Rec[]).length) out.push({ id: "blocker", question: "What is blocking this request, and what would unblock it?" });
  if (((tv.waiting_on ?? []) as Rec[]).length) out.push({ id: "waiting", question: "Who is this request waiting on, and why?" });
  out.push({ id: "purpose", question: tv.family === "business_change" || (tv.request_ref as Rec | undefined)?.family === "business_change"
    ? "What does this change do, and which rules does it affect?" : "What is this request about, and where does it stand?" });
  out.push({ id: "decisions", question: "What has been decided so far, and what is still open?" });
  const seen = new Set(asked.map(norm));
  return out.filter((s) => !seen.has(norm(s.question))).slice(0, 4);
}

function lower(s: string): string { return s ? s[0].toLowerCase() + s.slice(1) : s; }
function norm(s: string): string { return s.replace(/\s+/g, " ").trim().toLowerCase(); }

/** Server action code -> the test id of the EXISTING control on the owning page that the chip focuses (never clicks). Unknown or absent = no target (safe fallback). */
const TARGETS: Record<string, Record<string, string>> = {
  business_change: { approve_knowledge: "rev-reason", approve_file_writes: "rev-reason", commit_knowledge: "rev-commit", apply_source_edits: "rev-apply-source",
                     clarify: "rev-clarify-ref" },
};

export function actionTarget(family: string, action: string): string | null {
  return TARGETS[family]?.[action] ?? null;
}

/** EUI-02/03: where a cited fact is shown on the SAME page, from its kind and the server's typed key (never from its text). No new read: the target is a section
 *  the page already rendered for this reader. null = no in-page target (shown as text). */
export function citationTarget(fact: Rec | null | undefined): string | null {
  const kind = String(fact?.kind ?? ""), key = String(fact?.key ?? "");
  if (kind === "impact" && /^impact:rk-[0-9a-f]{16}$/.test(key)) return `[data-ref-key="${key.slice("impact:".length)}"]`;
  if (kind === "rule_change") return '[data-testid="rev-meaning"]';
  if (kind === "decision") return '[data-testid="rev-decisions"]';
  if (kind === "blocker") return '[data-testid="task-blockers"]';
  if (["obligation", "waiting", "action", "state", "step", "axis"].includes(kind)) return '[data-testid="task-frame"]';
  return null;
}

/** An action chip is offered only while the CURRENT task view still says the reader may do it and is asked to now (rechecked on use). */
export function actionStillCurrent(tv: Rec | null | undefined, action: string): boolean {
  return ((tv?.permitted_actions ?? []) as Rec[]).some((a) => a.action === action && a.allowed && a.current);
}

// R11 waiting, stopping and no-answer wording. Each sentence says only what the record shows: a question still queued can be stopped before it reaches the model;
// one already being answered cannot be withdrawn, and if an answer comes it is shown and its cost counted. Nothing claims the provider call or the charge was stopped.
export const SLOW_AFTER_MS = 20_000;

/** The note under a pending question: whether Stop can still take effect, and after 20 s that it is taking longer than usual. */
export function pendingNote(phase: unknown, waitedMs: number): string | null {
  const running = phase === "running";
  const slow = waitedMs >= SLOW_AFTER_MS ? "This is taking longer than usual. You can keep waiting; if no answer comes, this will say so. " : "";
  const stop = running ? "It is being answered now and can no longer be stopped; if an answer comes, it is shown here and its cost is counted." : "";
  return slow || stop ? (slow + stop).trim() : null;
}

/** What pressing Stop did, as the server reports it after the request. */
export function cancelNote(outcome: unknown): string | null {
  if (outcome === "cancelled") return "Stopped: the question had not been sent to the model.";
  if (outcome === "running") return "It is already being answered and can no longer be stopped. If an answer comes, it is shown here and its cost is counted.";
  if (outcome === "finished") return "It had already finished.";
  return null;
}

/** Why no answer came, in plain words, with the reference support can use to find the job (the stored technical reason stays in the Technical view). */
export function unavailableText(answer: Rec | null | undefined): string {
  const why: Record<string, string> = {
    provider_timeout: "The assistant did not answer in time.",
    attempt_limit: "The answer could not be completed and is not tried again automatically.",
    provider_failed: "The model service did not return a usable answer.",
  };
  const ref = answer?.reference ? ` Reference: ${String(answer.reference)}.` : "";
  return `${why[String(answer?.reason_code ?? "")] ?? "No answer was produced."} Nothing was concluded; you can ask again, which is a new paid question.${ref}`;
}
