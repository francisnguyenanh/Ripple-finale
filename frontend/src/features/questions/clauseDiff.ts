// SCN-20: Before / After of a rule as clauses. Presentation of the server's own strings only (`rule_text` joins clauses with " | "): no clause is reworded,
// reordered, merged or interpreted, and no model is involved. When both sides are clause lists and every clause has a unique "label: value" label on each side,
// clauses are matched by label (unchanged / changed / added / removed), so a clause inserted in the middle does not mark the rest as changed. Otherwise (a
// duplicate or missing label) only identical clauses are matched (longest common subsequence) and the rest is shown as removed / added — never a guessed pair.
// A string that is not a clause list (no delimiter, empty) is not structured: the caller shows it verbatim.
export const CLAUSE_SEP = " | ";
export type Mark = "same" | "changed" | "added" | "removed";
export interface Clause { text: string; mark: Mark }
export interface ClauseDiff { structured: boolean; aligned: "label" | "exact" | null; before: Clause[]; after: Clause[] }

export function splitClauses(s: unknown): string[] | null {
  return typeof s === "string" && s.includes(CLAUSE_SEP) ? s.split(CLAUSE_SEP) : null;
}

function labelOf(c: string): string | null {
  const i = c.indexOf(": ");
  return i > 0 ? c.slice(0, i) : null;
}

function uniqueLabels(cs: string[]): (string)[] | null {
  const ls = cs.map(labelOf);
  return ls.every((l): l is string => l !== null) && new Set(ls).size === ls.length ? ls : null;
}

/** Indexes of a longest common subsequence of identical clauses (stable, left-most). */
function lcs(a: string[], b: string[]): [Set<number>, Set<number>] {
  const n = a.length, m = b.length;
  const t: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) t[i][j] = a[i] === b[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
  const ia = new Set<number>(), ib = new Set<number>();
  for (let i = 0, j = 0; i < n && j < m;) {
    if (a[i] === b[j]) { ia.add(i); ib.add(j); i++; j++; } else if (t[i + 1][j] >= t[i][j + 1]) i++; else j++;
  }
  return [ia, ib];
}

export function clauseDiff(before: unknown, after: unknown): ClauseDiff {
  const b = splitClauses(before), a = splitClauses(after);
  if (!b || !a) return { structured: false, aligned: null, before: [], after: [] };
  const bl = uniqueLabels(b), al = uniqueLabels(a);
  if (bl && al) {
    const inA = new Map(al.map((l, i) => [l, a[i]])), inB = new Map(bl.map((l, i) => [l, b[i]]));
    return {
      structured: true, aligned: "label",
      before: b.map((c, i) => ({ text: c, mark: !inA.has(bl[i]) ? "removed" : inA.get(bl[i]) === c ? "same" : "changed" })),
      after: a.map((c, i) => ({ text: c, mark: !inB.has(al[i]) ? "added" : inB.get(al[i]) === c ? "same" : "changed" })),
    };
  }
  const [ib, ia] = lcs(b, a);
  return {
    structured: true, aligned: "exact",
    before: b.map((c, i) => ({ text: c, mark: ib.has(i) ? "same" : "removed" })),
    after: a.map((c, i) => ({ text: c, mark: ia.has(i) ? "same" : "added" })),
  };
}
