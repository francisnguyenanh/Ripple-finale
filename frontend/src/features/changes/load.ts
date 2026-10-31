// One read of everything the Changes flow shows. Read-only; no model call, no job creation.
import { api, ApiError } from "@/lib/api";
import type { Rec, Chat, ChangeState, FlowData, Ids, Intent, Job, Gaps, Contract, Decision, ReviewNotesState } from "./types";
import { isDone, jobState } from "./types";

const q = encodeURIComponent;

// A COMPLETED job never changes, so it is read once. Only jobs still running (and failed ones, which can be retried)
// are read again on each poll: this keeps the server and database load of a page with live work small.
const doneJobs = new Map<string, Job>();
async function getJob(ws: string, id?: string): Promise<Job | null> {
  if (!id) return null;
  const key = `${ws}|${id}`;
  const hit = doneJobs.get(key);
  if (hit) return hit;
  const job = await api<Job>("GET", `/v2/jobs/${q(id)}?workspace_id=${q(ws)}`);
  if (job.execution_status === "COMPLETED") doneJobs.set(key, job);
  return job;
}

const revCache = new Map<string, Rec>();
async function revisionView(ws: string, rev?: string | null): Promise<Rec | null> {
  if (!rev) return null;
  const key = ws + "|" + rev;
  if (!revCache.has(key)) revCache.set(key, await api<Rec>("GET", `/v2/sources/revisions/${q(rev)}?workspace_id=${q(ws)}`));
  return revCache.get(key) ?? null;
}

const diffCache = new Map<string, Rec>();

export async function fetchChange(ws: string, applyId: string): Promise<ChangeState> {
  const [applyJob, dto] = await Promise.all([
    api<Job>("GET", `/v2/jobs/${q(applyId)}?workspace_id=${q(ws)}`),
    api<Rec>("GET", `/v2/changes/${q(applyId)}?workspace_id=${q(ws)}`),
  ]);
  const re = (dto.reanalysis ?? {}) as Rec;
  const ids: string[] = Array.from(new Set([...(re.job_ids ?? []), ...(re.invalidation_job_ids ?? []), ...(re.downstream_job_ids ?? [])]));
  const [jobs, revision] = await Promise.all([
    Promise.all(ids.map((id) => getJob(ws, id))),
    dto.published_revision_id ? revisionView(ws, dto.published_revision_id as string) : Promise.resolve(null),
  ]);
  return { applyJob, dto: dto as ChangeState["dto"], reJobs: jobs, revision };
}

export async function loadFlow(ws: string, ids: Ids, can: (p: string) => boolean): Promise<FlowData> {
  const head = await api<FlowData["head"]>("GET", `/v2/workspaces/${q(ws)}/head`);
  const whatif = ids.whatif ? await getJob(ws, ids.whatif).catch(() => null) : null;
  let chat: Chat | null = null;
  if (ids.chat) {
    try {
      chat = await api<Chat>("GET", `/v2/chat/${q(ids.chat)}?workspace_id=${q(ws)}`);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status === 401) throw e;
      chat = { unavailable: true } as Chat;
    }
  }
  const [intent, impact, simulation, proposal, review, approval, apply, draft, query] = await Promise.all([
    ids.intent ? api<Intent>("GET", `/v2/intent/${q(ids.intent)}?workspace_id=${q(ws)}`) : Promise.resolve(null),
    getJob(ws, ids.impact), getJob(ws, ids.simulation), getJob(ws, ids.proposal), getJob(ws, ids.review),
    getJob(ws, ids.approval), getJob(ws, ids.apply), getJob(ws, ids.draft), getJob(ws, ids.query),
  ]);
  const lineage = ids.intent ? await api<Rec>("GET", `/v2/intent/${q(ids.intent)}/lineage?workspace_id=${q(ws)}`).catch(() => null) : null;
  const needClaims = !ids.intent && can("intent.create");
  const needArts = ((!!simulation && isDone(simulation)) || !!apply || (!!impact && isDone(impact))) && (can("source.import") || can("ai.use"));       // the file list is also what the AI investigation of UNKNOWN rules needs, right after the impact analysis
  const proposalDone = isDone(proposal);
  const loopWanted = !!ids.draft && !!ids.apply && !!apply && isDone(apply);
  const gaps: Gaps | null = isDone(draft)
    ? await api<Gaps>("GET", `/v2/change/gaps?workspace_id=${q(ws)}&draft_job_id=${q(ids.draft!)}`).catch(() => null)
    : null;
  const suggest: Record<string, Job | null> = {};
  if (gaps) for (const g of gaps.gaps) for (const jid of (g.suggestion_jobs ?? []).slice(-1)) suggest[g.gap_id] = await getJob(ws, jid).catch(() => null);

  const cachedDiff = ids.proposal ? diffCache.get(ids.proposal) : undefined;
  // the exact file diff is served to approvers and evidence readers only (SEC-01); other members get the proposal without it
  const diffAllowed = can("evidence.read") || can("change.approve");
  const [decisions, diff, change, claims, rev, contracts, loop] = await Promise.all([
    ids.approval ? api<{ decisions: Decision[] }>("GET", `/v2/jobs/${q(ids.approval)}/decisions?workspace_id=${q(ws)}`) : Promise.resolve(null),
    proposalDone && diffAllowed
      ? (cachedDiff ? Promise.resolve(cachedDiff) : api<Rec>("GET", `/v2/proposals/${q(ids.proposal!)}/diff?workspace_id=${q(ws)}`).catch((e) => (e instanceof ApiError && e.status === 403 ? null : Promise.reject(e))))
      : Promise.resolve(null),
    ids.apply ? fetchChange(ws, ids.apply) : Promise.resolve(null),
    needClaims ? api<{ claims: Rec[] }>("GET", `/v2/knowledge/overview?workspace_id=${q(ws)}`) : Promise.resolve(null),
    needArts && head.source_revision_id ? revisionView(ws, head.source_revision_id) : Promise.resolve(null),
    proposalDone ? api<{ contracts: Contract[]; stale?: boolean; stale_reason?: string | null; applied_job_id?: string | null }>("GET", `/v2/verification/contracts?workspace_id=${q(ws)}&proposal_job_id=${q(ids.proposal!)}`) : Promise.resolve(null),
    loopWanted ? api<Rec>("GET", `/v2/change/loop-status?workspace_id=${q(ws)}&draft_job_id=${q(ids.draft!)}&apply_job_id=${q(ids.apply!)}`).catch(() => null) : Promise.resolve(null),
  ]);
  if (diff && proposalDone && ids.proposal) diffCache.set(ids.proposal, diff);
  const notes = proposalDone ? await api<ReviewNotesState>("GET", `/v2/changes/${q(ids.proposal!)}/review-notes?workspace_id=${q(ws)}`).catch(() => null) : null;
  return {
    chat, head, intent, impact, simulation, proposal, review, approval, apply, draft, query, whatif, loop,
    decisions: decisions?.decisions ?? [], diff, change, gaps, suggest,
    lineage, claims: claims?.claims ?? null, arts: (rev?.artifacts as Rec[] | undefined) ?? null, contracts: contracts?.contracts ?? null,
    proposalAppliedJob: contracts?.applied_job_id ?? null,
    contractsStale: contracts?.stale ? (contracts.stale_reason ?? "the files changed after this proposal was prepared") : null, notes,
  };
}

/** True while something is still running on the server (drives bounded polling). */
export function isLive(d: FlowData): boolean {
  if (d.chat && !d.chat.unavailable && d.chat.awaiting_ai) return true;
  const jobs = [d.draft, d.query, d.impact, d.simulation, d.proposal, d.review, d.approval, d.apply, d.whatif];
  if (jobs.some((j) => j && !["COMPLETED", "FAILED", "CANCELLED"].includes(jobState(j)))) return true;
  const cs = d.change;
  if (!cs) return false;
  if (!["COMPLETED", "FAILED", "CANCELLED"].includes(jobState(cs.applyJob))) return true;
  return ["STAGED", "SOURCE_PUBLISHED", "REANALYSING", "MACHINE_VERIFIED", "AWAITING_ACCEPTANCE"].includes(cs.dto.state);
}
