// Shapes of the existing /v2 responses used by the Changes flow (checked against the server code
// and the legacy shell). Result payloads that vary by job kind stay loosely typed (Rec) and are
// narrowed where they are read.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Rec = Record<string, any>;

export interface Job {
  job_id: string;
  kind: string;
  status?: string;
  execution_status?: string;
  execution_result?: Rec | null;
  execution_reason?: string | null;
  execution_usage?: { input_tokens?: number; output_tokens?: number } | null;
  execution_model_id?: string | null;
  execution_cost_usd?: string | number | null;
  requested_by?: string | null;
  cancel_requested_at?: string | null;
  payload?: Rec;
  /** Present on a FAILED AI job: whether a retry can run at all (false once every approved provider attempt was used). */
  retry?: { allowed: boolean; attempts_used: number; attempt_limit: number };
}

export const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"] as const;

export function jobState(j: Job): string {
  const s = j.execution_status;
  if (s && (TERMINAL as readonly string[]).includes(s)) return s;
  if (j.status === "CANCELLED" || j.cancel_requested_at) return "CANCELLED";
  return s || "QUEUED";
}
export const isTerminal = (j: Job | null | undefined): boolean => !!j && (TERMINAL as readonly string[]).includes(jobState(j));
export const isDone = (j: Job | null | undefined): boolean => !!j && jobState(j) === "COMPLETED";

/** Phase of a job that spends model budget: waiting for a second person is a phase of its own. */
export function phase(j: Job | null | undefined): string {
  if (!j) return "NONE";
  if (j.execution_status && (TERMINAL as readonly string[]).includes(j.execution_status)) return j.execution_status;
  if (j.execution_status === "RUNNING") return "RUNNING";            // allowed AND already running: say so, not "waiting to run"
  if (j.status === "PENDING_APPROVAL" || j.status === "APPROVED") return j.status;
  return jobState(j);
}

export interface Turn {
  seq: number;
  role: "user" | "assistant" | "edit" | "system";
  text: string;
  by?: string | null;
  job_id?: string | null;
}

export interface Understanding {
  status: "READY" | "CLARIFY" | "UNSUPPORTED" | "FAILED" | string;
  message?: string;
  questions?: unknown[];
  fields?: Rec | null;
  assumptions?: string[];
  problems?: string[];
  changes?: { kind: string; summary: string; supported: boolean; quote?: string }[];
  out_of_scope?: string[];
  sources?: Rec;
  safety?: { code: string; business: string }[];
  limits_note?: string;
  source?: string;
}

export interface Chat {
  conversation_id: string;
  status: "OPEN" | "CONFIRMED" | "CANCELLED" | string;
  created_by: string;
  turns: Turn[];
  understanding: Understanding | null;
  version: number;
  intent_id?: string | null;
  ai_used: number;
  ai_allowance: number;
  pinned_source_revision_id?: string | null;
  pinned_decision_epoch?: number | null;
  limits: { max_ai_turns: number; max_allowance_grant: number; max_user_turn_chars: number };
  awaiting_ai: boolean;
  awaiting_allowance: boolean;
  can_use_ai?: boolean;
  /** Why the AI is not answering: revoked | suspended | no_permission | sandbox_hold | quota_used; null/absent = it can be asked. */
  ai_block?: string | null;
  authorization: {
    live: boolean;
    replies_left: number;
    replies_used: number;
    replies_allowed: number;
    granted_by?: string | null;
    expires_at?: string | null;
    revoked_at?: string | null;
    revoked_by?: string | null;
    reserved_usd_per_reply?: string | null;
    worst_case_usd_left?: string | null;
    covers: string;
    does_not_cover: string[];
  };
  safety_notes: { code: string; business: string }[];
  you_are_creator: boolean;
  can_allow: boolean;
  unavailable?: boolean;
  purpose?: string;
  context?: Rec | null;
  outcome?: Rec | null;
}

export interface Intent {
  intent_id: string;
  status: string;
  raw_request?: string;
  target?: string | null;
  value?: unknown;
  questions?: unknown[];
  digest?: string;
  created_by?: string | null;
  structured_form?: Rec | null;
  exclusions?: unknown[];
  effective_time?: unknown;
}

export interface Gap {
  gap_id: string;
  key: string;
  kind: string;
  state: "unresolved" | "commented" | "confirmed" | string;
  text: string;
  comments?: { actor_id: string; text: string }[];
  confirmation?: { actor_id: string; epoch: number; answer: string; rationale?: string } | null;
  suggestion_jobs?: string[];
  text_redacted?: boolean;
  disposition?: "unresolved" | "outside_this_request" | string;
  carried_from?: { draft_job_id: string; ref?: string; status?: string; reason?: string; model_now?: Rec };
  disposition_basis?: { decision_id: string; by: string; epoch: number; reason?: string };
  feedback?: { status: "none" | "pending" | "applied" | "rejected" | "not_applied_yet" | "withdrawn"; reason: string; by?: string; decision_id?: string; epoch?: number; at?: string | null };
}
export interface Gaps {
  completed: boolean;
  stale: boolean;
  stale_reasons?: string[];
  gaps: Gap[];
  feedback_log?: { decision_id: string; epoch: number; by: string; question: string; answer: string; why?: string | null; status: "applied" | "rejected" | "not_applied_yet" | "withdrawn"; reason: string }[];
  clarifications_used?: { decision_id?: string; actor_id: string; epoch: number; question?: string; answer?: string; rationale?: string }[];
}

export interface ChangeDto extends Rec {
  apply_job_id: string;
  state: string;
}
export interface ChangeState {
  applyJob: Job;
  dto: ChangeDto;
  reJobs: (Job | null)[];
  revision: Rec | null;
}

export interface Decision { action: "approve" | "reject" | "request_changes" | string; actor_id: string; reason?: string }

export interface Contract {
  contract_id: string;
  logical_path: string;
  locator: string;
  expected: unknown;
  author_account_id: string;
  contract_sha256?: string;
  /** Stated for a proposal on a source revision that is no longer the head: it no longer applies. */
  stale?: boolean;
}

/** Reviewer notes of the proposal (open requests block approval). */
export interface ReviewNotesState { open_change_requests: string[]; notes: { actor_id: string; verdict: string; text: string }[]; total?: number; has_more?: boolean; next_before?: string | null }

export interface FlowData {
  chat: Chat | null;
  head: { source_revision_id?: string | null; knowledge_revision_id?: string | null; decision_epoch?: number; version?: number };
  intent: Intent | null;
  draft: Job | null;
  impact: Job | null;
  simulation: Job | null;
  proposal: Job | null;
  review: Job | null;
  approval: Job | null;
  apply: Job | null;
  query: Job | null;
  whatif: Job | null;
  loop: Rec | null;
  decisions: Decision[];
  diff: Rec | null;
  change: ChangeState | null;
  gaps: Gaps | null;
  suggest: Record<string, Job | null>;
  claims: Rec[] | null;
  arts: Rec[] | null;
  contracts: Contract[] | null;
  contractsStale?: string | null;
  /** The completed apply job of the proposal this page shows (known by the server even when the link has no apply id). */
  proposalAppliedJob?: string | null;
  notes?: ReviewNotesState | null;
  lineage: Rec | null;
}

export const STAGES = ["impact", "simulation", "proposal", "review", "approval", "apply"] as const;
export type StageKey = (typeof STAGES)[number] | "draft" | "query" | "whatif" | "intent" | "chat";
export type Ids = Partial<Record<StageKey, string>>;
