// EUI-07: an interpretation statement links to engine rows only through its server-validated `metric_refs` (`s<n>.<kpi>.<baseline|scenario|delta>`, n = the
// 1-based row of THIS run's comparison). Nothing is matched by free text or by equal numbers, so two scenarios with the same value never cross-highlight.
export interface MetricRef { ref: string; row: string; scenario: number; kpi: string; part: "baseline" | "scenario" | "delta" }

const REF = /^s([1-9]\d*)\.([a-z0-9_]+)\.(baseline|scenario|delta)$/;

export function parseMetricRef(ref: unknown): MetricRef | null {
  const m = typeof ref === "string" ? REF.exec(ref) : null;
  return m ? { ref: ref as string, row: `s${m[1]}.${m[2]}`, scenario: Number(m[1]), kpi: m[2], part: m[3] as MetricRef["part"] } : null;
}

/** row key (`s<n>.<kpi>`) -> 1-based numbers of the statements citing it (reverse references). */
export function citedBy(statements: { metric_refs?: unknown }[]): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  statements.forEach((st, i) => {
    for (const r of Array.isArray(st.metric_refs) ? st.metric_refs : []) {
      const p = parseMetricRef(r);
      if (p && !(out[p.row] ?? []).includes(i + 1)) (out[p.row] ??= []).push(i + 1);
    }
  });
  return out;
}
