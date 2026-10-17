// The Explorer tree is a PRESENTATION projection of the knowledge graph: source file -> rules read from it -> (evidence, issues, decisions).
// It never clones a record: every rule, issue and decision keeps its canonical id and is counted once, even when an issue involves rules
// from several files (it is listed under each of them and the totals below still count it once). It follows strict containment only
// (a rule has exactly one source file), so there is no cycle to recurse into; "related rule" links are plain references, not children,
// and a link between two rules is a proposed identity guess, never a dependency.
import type { GEdge, GNode } from "./layout";

export interface RuleRow { id: string; node: GNode; file: string; evidence: GNode[]; issues: GNode[]; decisions: GNode[]; related: { node: GNode; edge: GEdge }[] }
export interface FileRow { id: string; node: GNode; rules: RuleRow[] }
export interface Tree {
  files: FileRow[];
  /** the container (file row id, rule row id) of any node id, for deep links and search */
  home: Map<string, { file: string; rule?: string }>;
  byId: Map<string, GNode>;
  /** unique totals (never counted twice) */
  totals: { files: number; rules: number; evidence: number; issues: number; decisions: number; confirmed: number; pending: number };
  /** issues no rule points at (still shown, never dropped) */
  looseIssues: GNode[];
}

export function buildTree(nodes: GNode[], edges: GEdge[]): Tree {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const files: FileRow[] = nodes.filter((n) => n.type === "artifact").sort((a, b) => a.label.localeCompare(b.label)).map((n) => ({ id: n.id, node: n, rules: [] }));
  const fileById = new Map(files.map((f) => [f.id, f]));
  const rules = new Map<string, RuleRow>();
  const fileOf = new Map<string, string>();
  for (const e of edges) if (e.type === "source-of" && fileById.has(e.from) && byId.get(e.to)?.type === "candidate") fileOf.set(e.to, e.from);
  const orphan: FileRow = { id: "(unknown-file)", node: { id: "(unknown-file)", type: "artifact", label: "(unknown file)", state: "not-analysed" }, rules: [] };
  for (const n of nodes) {
    if (n.type !== "candidate") continue;
    const fid = fileOf.get(n.id) ?? orphan.id;
    const r: RuleRow = { id: n.id, node: n, file: fid, evidence: [], issues: [], decisions: [], related: [] };
    rules.set(n.id, r);
    (fileById.get(fid) ?? orphan).rules.push(r);
  }
  if (orphan.rules.length) { files.push(orphan); fileById.set(orphan.id, orphan); }
  const issueRules = new Map<string, string[]>();
  for (const e of edges) {
    const to = byId.get(e.to), from = byId.get(e.from);
    if (!to || !from) continue;
    if (e.type === "evidence-of" && from.type === "evidence" && rules.has(e.to)) rules.get(e.to)!.evidence.push(from);
    else if (e.type === "involved-in" && rules.has(e.from) && to.type === "finding") {
      rules.get(e.from)!.issues.push(to);
      issueRules.set(to.id, [...(issueRules.get(to.id) ?? []), e.from]);
    } else if (e.type === "relation" && rules.has(e.from) && rules.has(e.to)) {
      rules.get(e.from)!.related.push({ node: to, edge: e });
      rules.get(e.to)!.related.push({ node: from, edge: e });
    }
  }
  for (const e of edges) {
    if (e.type !== "decided-by") continue;
    const dec = byId.get(e.from);
    if (!dec) continue;
    if (rules.has(e.to)) rules.get(e.to)!.decisions.push(dec);
    else for (const rid of issueRules.get(e.to) ?? []) rules.get(rid)!.decisions.push(dec);
  }
  const home = new Map<string, { file: string; rule?: string }>();
  for (const f of files) {
    home.set(f.id, { file: f.id });
    for (const r of f.rules) {
      home.set(r.id, { file: f.id, rule: r.id });
      for (const x of [...r.evidence, ...r.decisions]) if (!home.has(x.id)) home.set(x.id, { file: f.id, rule: r.id });
      for (const x of r.issues) if (!home.has(x.id)) home.set(x.id, { file: f.id, rule: r.id });
    }
  }
  const looseIssues = nodes.filter((n) => n.type === "finding" && !issueRules.has(n.id));
  const cands = [...rules.values()];
  return {
    files, home, byId, looseIssues,
    totals: {
      files: files.filter((f) => f.id !== orphan.id || f.rules.length).length, rules: cands.length,
      evidence: nodes.filter((n) => n.type === "evidence").length, issues: nodes.filter((n) => n.type === "finding").length, decisions: nodes.filter((n) => n.type === "decision").length,
      confirmed: cands.filter((r) => r.node.state === "confirmed").length, pending: cands.filter((r) => r.node.state === "pending-confirmation").length,
    },
  };
}

/** Ids that must be open to show `id` (a file, a rule, or anything inside a rule). Unknown ids open nothing. */
export function openPath(t: Tree, id: string): string[] {
  const h = t.home.get(id);
  if (!h) return [];
  return h.rule ? [h.file, h.rule] : [h.file];
}

const lower = (v: unknown) => String(v ?? "").toLowerCase();
export function nodeText(n: GNode): string {
  const d = (n.detail ?? {}) as Record<string, unknown>;
  return lower([n.label, n.id, d.subject, d.predicate, d.value, d.unit, d.kind, d.reason, d.quote, d.logical_path, d.actor_id, JSON.stringify(d.scope ?? ""), JSON.stringify(d.locator ?? "")].join(" "));
}

export interface Visible { files: Set<string>; rules: Set<string>; hits: Set<string> }
/** What a search/state filter leaves visible: every match plus the rows above it, so the way down to a result is always shown. */
export function filterTree(t: Tree, query: string, state: string): Visible | null {
  const q = query.trim().toLowerCase();
  if (!q && !state) return null;
  const files = new Set<string>(), rules = new Set<string>(), hits = new Set<string>();
  const ok = (n: GNode) => (!state || n.state === state) && (!q || nodeText(n).includes(q));
  for (const f of t.files) {
    const fileHit = !state && ok(f.node);
    if (fileHit) hits.add(f.id);
    for (const r of f.rules) {
      const kids = [...r.evidence, ...r.issues, ...r.decisions].filter((x) => !state && q && nodeText(x).includes(q));
      const hit = ok(r.node);
      if (hit) hits.add(r.id);
      for (const k of kids) hits.add(k.id);
      if (hit || kids.length || fileHit) { rules.add(r.id); files.add(f.id); }
    }
    if (fileHit) files.add(f.id);
  }
  return { files, rules, hits };
}
