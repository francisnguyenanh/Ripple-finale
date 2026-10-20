// Triage of the loaded business questions (UX-07): grouped by what they ask, the questions that need an answer first inside each group, with exact counts of
// what was loaded. Pure: no request, no new fact; the server's order is kept apart from that. A filter only hides loaded cards, it decides nothing.
export interface TriageItem { gap_id: string; kind: string; priority?: string }
export interface TriageGroup<T> { key: string; label: string; items: T[]; critical: number }
export type TriageFilter = "" | "critical" | `kind:${string}`;

export const KIND_ORDER = ["missing_actor", "missing_condition", "missing_outcome", "missing_exception", "conflicting_facts"];

export function matches(g: TriageItem, filter: TriageFilter | string): boolean {
  return filter === "" || (filter === "critical" ? g.priority === "critical" : `kind:${g.kind}` === filter);
}

export function triage<T extends TriageItem>(gaps: T[], labels: Record<string, string>, filter: TriageFilter = ""): TriageGroup<T>[] {
  const keep = gaps.filter((g) => matches(g, filter));
  const kinds = [...KIND_ORDER.filter((k) => keep.some((g) => g.kind === k)), ...[...new Set(keep.map((g) => g.kind))].filter((k) => !KIND_ORDER.includes(k))];
  return kinds.map((k) => {
    const of = keep.filter((g) => g.kind === k);
    const items = [...of.filter((g) => g.priority === "critical"), ...of.filter((g) => g.priority !== "critical")];
    return { key: k, label: labels[k] ?? "Other questions", items, critical: items.filter((g) => g.priority === "critical").length };
  });
}

export function filterCounts(gaps: TriageItem[]): { key: TriageFilter; count: number }[] {
  const kinds = [...KIND_ORDER.filter((k) => gaps.some((g) => g.kind === k)), ...[...new Set(gaps.map((g) => g.kind))].filter((k) => !KIND_ORDER.includes(k))];
  return [{ key: "", count: gaps.length }, { key: "critical", count: gaps.filter((g) => g.priority === "critical").length },
    ...kinds.map((k) => ({ key: `kind:${k}` as TriageFilter, count: gaps.filter((g) => g.kind === k).length }))];
}
