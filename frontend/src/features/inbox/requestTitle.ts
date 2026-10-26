// Title of a My work row (SCN-15). The server sends the request's own words as `title` when it has them and falls back to the request id otherwise
// (`infra/v2_work_routes.py`: `title or id`). An id is not a title: the row then says honestly what it is (its family, e.g. "Change request") with a short id beside
// it, and keeps the full id for the Technical view and the link's accessible name. Nothing is guessed from other fields and no model is asked for a name.
export const FAMILY_LABEL: Record<string, string> = { intent_flow: "Change request", business_change: "Business knowledge change" };

export interface TitleSource { title?: string | null; proposal_job_id: string; params?: Record<string, string>; family?: string }
export interface RowTitle { text: string; fallback: boolean; id: string; short: string }

/** "intent-e7275769e82247e4b7bbde3ad5ceff9e" -> "intent-e72757…"; short ids are kept whole. */
export function shortId(id: string): string {
  if (id.length <= 16) return id;
  const cut = id.lastIndexOf("-");
  return cut > 0 && cut < id.length - 7 ? `${id.slice(0, cut + 7)}…` : `${id.slice(0, 12)}…`;
}

export function rowTitle(it: TitleSource): RowTitle {
  const raw = (it.title ?? "").trim();
  const ids = [it.proposal_job_id, ...Object.values(it.params ?? {})].filter(Boolean);
  const hit = ids.find((x) => x === raw);
  if (raw && !hit) return { text: raw, fallback: false, id: it.proposal_job_id, short: shortId(it.proposal_job_id) };
  const id = hit ?? it.proposal_job_id;
  return { text: FAMILY_LABEL[it.family ?? ""] ?? "Change request", fallback: true, id, short: shortId(id) };
}
