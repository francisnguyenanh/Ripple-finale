// AI job display adapters for the extension panels (variants, memos, mining, interview, simulation). Pure mapping, no I/O.
// Two dimensions stay apart (CONTENT_AND_STATES): whether the JOB finished, and what the MODEL contributed after the server's checks. A finished job whose model output was
// refused, or a deterministic fallback, is never shown as an AI result. Unknown tokens are neutral, never success.
import type { Status } from "@/components/StatusBadge";

type Rec = Record<string, unknown>;

const JOB: Record<string, { status: Status; label: string }> = {
  not_requested: { status: "empty", label: "Not requested" },
  waiting_for_approval: { status: "waiting", label: "Waiting for approval" },
  queued: { status: "queued", label: "Queued" },
  running: { status: "running", label: "Model job running" },
  generating: { status: "running", label: "Model job running" },
  done: { status: "success", label: "Job finished" },
  available: { status: "success", label: "Job finished" },
  failed: { status: "failed", label: "Job failed" },
  cancelled: { status: "stale", label: "Job cancelled" },
  stale: { status: "stale", label: "Older knowledge version" },
  deferred: { status: "partial", label: "Deferred" },
};

export function aiJobBadge(state: unknown): { status: Status; label: string } {
  return JOB[String(state)] ?? { status: "info", label: `State: ${String(state ?? "unknown")}` };
}

/** True while a poll is worth doing (the job can still change by itself). */
export function aiJobPending(state: unknown): boolean {
  return ["waiting_for_approval", "queued", "running", "generating"].includes(String(state));
}

/** What the model contributed, from the server's `ai_outcome` (retained / rejected / none, degraded reason). */
export function contribution(outcome: unknown): { status: Status; label: string } {
  const o = (outcome && typeof outcome === "object" ? outcome : {}) as Rec;
  if (o.degraded) return { status: "partial", label: "Deterministic fallback, no model wording" };
  if (o.contribution === "retained") return { status: "info", label: `Model output kept after server checks (${Number(o.retained ?? 0)} kept, ${Number(o.rejected ?? 0)} refused)` };
  if (o.contribution === "rejected") return { status: "failed", label: "Model output refused by the server checks" };
  if (o.contribution === "none") return { status: "empty", label: "No model call was made" };
  return { status: "info", label: "Model contribution unavailable" };
}

/** Counts of refusals by reason (Technical detail stays with the raw reason codes). */
export function reasonsCount(items: unknown): [string, number][] {
  const counts = new Map<string, number>();
  for (const it of Array.isArray(items) ? (items as Rec[]) : []) {
    const r = String(it.reason ?? "unknown").split(":")[0];
    counts.set(r, (counts.get(r) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
