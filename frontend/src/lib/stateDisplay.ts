// Presentation tone of a raw state token. Pure display mapping: the raw token stays in data-state and in the
// Technical view, and a token this table does not know is shown neutral, never as success.
export type StateTone = "success" | "danger" | "muted" | "warning";

const SUCCESS = new Set(["COMPLETED", "CONFIRMED", "APPROVED", "PASSED"]);
const DANGER = new Set(["FAILED", "REJECTED", "STALE"]);
const MUTED = new Set(["NONE", "NOT_STARTED"]);
const WARNING = new Set(["RUNNING", "QUEUED", "PENDING", "PENDING_APPROVAL", "PARTIAL", "WAITING", "CHANGES_REQUESTED", "CLARIFY", "ABSTAINED", "CANCELLED", "NOT_EXECUTED", "UNKNOWN"]);

export function stateTone(state: string): StateTone {
  if (SUCCESS.has(state)) return "success";
  if (DANGER.has(state)) return "danger";
  if (WARNING.has(state)) return "warning";
  if (MUTED.has(state)) return "muted";
  return "muted";
}

export const TONE_CLASS: Record<StateTone, string> = { success: "text-success", danger: "text-danger", warning: "text-warning", muted: "text-muted" };

/** A label longer than this is an instruction, not a status: it wraps instead of being clipped into a pill. */
export const PILL_LABEL_MAX = 28;
