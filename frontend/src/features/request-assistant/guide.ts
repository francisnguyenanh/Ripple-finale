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
