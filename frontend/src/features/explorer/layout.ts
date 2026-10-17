// Column layout of the knowledge graph: files -> quoted evidence -> rules -> findings -> decisions.
// Pure and deterministic so it can be tested without a browser.
export interface GNode { id: string; type: string; label: string; state: string; detail?: Record<string, unknown>; provenance?: string; source_revision_id?: string | null; stale?: boolean; file_unchanged_since_read?: boolean }
export interface GEdge { id: string; from: string; to: string; type: string; relation_type?: string; state?: string; projection?: boolean; reason?: string }

export const COLUMN: Record<string, number> = { artifact: 0, evidence: 1, candidate: 2, finding: 3, decision: 4 };
export const COL_W = 330;
export const ROW_H = 84;

export function layout(nodes: GNode[]): Record<string, { x: number; y: number }> {
  const rows: number[] = [0, 0, 0, 0, 0];
  const pos: Record<string, { x: number; y: number }> = {};
  for (const n of nodes) {
    const c = COLUMN[n.type] ?? 4;
    pos[n.id] = { x: c * COL_W, y: rows[c] * ROW_H };
    rows[c] += 1;
  }
  return pos;
}

export function matches(n: GNode, q: string, state: string): boolean {
  if (state && n.state !== state) return false;
  if (q && !`${n.label} ${n.id}`.toLowerCase().includes(q.toLowerCase())) return false;
  return true;
}
