// UX-09: the engine-only numbers of a deterministic sample run, as one typed shape (no AI estimate, no invented denominator). Pure adapter of the Harbor simulation
// result; the shown cases are capped and the full count is kept, so a capped table always says how many it shows of how many.
export const CASE_LIMIT = 60;

type Rec = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export interface OutcomeRow { key: string; before: number; after: number }
export interface Outcome {
  total: unknown; invoices: boolean; changedCount: number; unexplained: number;
  routes: OutcomeRow[]; cases: Rec[]; caseLimit: number;
}

export function outcomeOf(h: Rec): Outcome {
  const before = (h.by_route_before ?? {}) as Record<string, number>;
  const after = (h.by_route_after ?? {}) as Record<string, number>;
  const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)]));
  return {
    total: h.transactions, invoices: h.case === "match", changedCount: Number(h.changed_count ?? 0), unexplained: Number(h.unexplained_changes ?? 0),
    routes: keys.map((k) => ({ key: k, before: before[k] ?? 0, after: after[k] ?? 0 })),
    cases: ((h.changed ?? []) as Rec[]).slice(0, CASE_LIMIT), caseLimit: CASE_LIMIT,
  };
}
