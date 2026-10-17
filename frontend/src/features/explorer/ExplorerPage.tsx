// Explorer: what Ripple read, where it came from, what needs a decision and what changed. The DEFAULT view is a tree you open and close
// (file -> rules -> explanation); the graph is an optional second view of the same response. Read-only: opening this page never calls a
// model. State is always icon + text. Links between rules are proposed identity guesses, not proven dependencies; "not analysed",
// "stale" and "no evidence" are shown, never hidden, and unknown is never shown as zero or as unaffected.
import * as React from "react";
import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { Network, TreePine } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Denied, Callout, Card, Field, Muted, StatePill, Tech, TextInput } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { basename, nodePhrase, ruleTitle, scopeText, valueText } from "@/lib/fmt";
import { replaceHash, hashFor } from "@/lib/route";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { bizLabel, EDGE_BIZ, nodeHelp, nodeHistoryLines, nodeStatus, STATE_HELP, SYMBOL, sym, TYPE_LABEL } from "./explorerText";
import { ExplorerTree, locText } from "./ExplorerTree";
import { layout, matches, type GEdge, type GNode } from "./layout";
import { ConfirmPanel, ResolvePanel, Timeline, useTimeline } from "./RuleParts";
import { buildTree, filterTree, openPath, type Tree } from "./tree";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const AUTO_OPEN_FILES_UP_TO = 60;      // a workspace with more rules than this opens collapsed
const OPEN_RULES_LIMIT = 25;           // "Open some rules" is bounded on purpose

interface Graph { nodes: GNode[]; edges: GEdge[]; coverage: Rec; head: Rec }

/** Label of a node in the graph: evidence is named by its file, location and a short excerpt, never just "Quoted text". */
function graphLabel(n: GNode, tree: Tree, technical = false): string {
  if (n.type !== "evidence") return bizLabel(n);
  const h = tree.home.get(n.id);
  const file = h ? basename(tree.byId.get(h.file)?.label) : "";
  const d = (n.detail ?? {}) as Rec;
  const quote = String(d.quote ?? "").replace(/\s+/g, " ").trim();
  return [file, locText(d.locator, technical), quote ? `"${quote.slice(0, 40)}${quote.length > 40 ? "..." : ""}"` : ""].filter(Boolean).join(" - ") || "Evidence";
}

interface KData extends Record<string, unknown> { n: GNode; label: string; selected: boolean; near: boolean; onSelect: (id: string) => void }

function KNode({ data }: NodeProps<Node<KData>>) {
  const { n, label, selected, near, onSelect } = data;
  return (
    <div data-node={n.id} data-type={n.type} data-state={n.state} className={`w-[230px] rounded-md border bg-surface text-sm shadow-card ${selected ? "border-primary ring-2 ring-primary" : near ? "border-primary" : n.state === "stale" || n.state === "not-analysed" ? "border-dashed border-border" : "border-border"}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <button type="button" aria-pressed={selected} onClick={() => onSelect(n.id)}
        className="w-full text-left px-2 py-1.5 cursor-pointer flex gap-2 items-start">
        <span aria-hidden="true" className="shrink-0 w-4 text-center">{sym(n.state)}</span>
        <span className="min-w-0"><span className="block text-[11px] uppercase tracking-wide text-muted">{TYPE_LABEL[n.type]}</span><span className="block line-clamp-2 break-words" title={label}>{label}</span><span className="sr-only">{nodePhrase(n.state)}</span></span>
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}
const NODE_TYPES = { k: KNode };

/** The graph's detail panel (the tree shows the same content inline, under each rule). */
function Detail({ g, tree, id, reload }: { g: Graph; tree: Tree; id: string | null; reload: () => Promise<void> }) {
  const s = useSession();
  const ws = s.route.ws!;
  const n = id ? g.nodes.find((x) => x.id === id) : undefined;
  const isCand = n?.type === "candidate";
  const tl = useTimeline(ws, n?.id, !!isCand, g);
  if (!n) return <Muted>Select a box to see its source, evidence, state and connections.</Muted>;
  const d = (n.detail ?? {}) as Rec;
  const near = g.edges.filter((e) => e.from === n.id || e.to === n.id);
  const byId = (nid: string) => g.nodes.find((x) => x.id === nid);
  const rawLabel = (nid: string) => byId(nid)?.label ?? nid;
  const bl = (nid: string) => { const x = byId(nid); return x ? graphLabel(x, tree) : nid; };
  const cands = n.type === "finding" ? g.edges.filter((e) => e.type === "involved-in" && e.to === n.id).map((e) => byId(e.from)).filter((x): x is GNode => !!x) : [];
  return (
    <section data-testid="node-detail" data-detail-for={n.id} className="grid gap-3">
      <h2 className="font-semibold text-lg">{TYPE_LABEL[n.type]}: {graphLabel(n, tree)}<Tech><span className="block text-xs text-muted font-normal">{n.label}</span></Tech></h2>
      <p className="text-sm"><StatePill state={n.state} label={`${sym(n.state)} ${nodeStatus(n)}`} testid="node-state" /><Tech><span className="text-muted"> {STATE_HELP[n.state] ?? ""}</span></Tech></p>
      {nodeHelp(n) && <p className="text-sm text-muted">{nodeHelp(n)}</p>}
      {nodeHistoryLines(n).map((x, i) => <p key={i} data-testid="node-history" className="text-sm text-muted">{x}</p>)}
      {isCand && (
        <dl data-testid="node-facts" className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted">Rule</dt><dd>{ruleTitle(d.subject, d.predicate)}</dd>
          <dt className="text-muted">Value</dt><dd>{valueText(d.value, d.unit, d.unit)}</dd>
          <dt className="text-muted">Applies to</dt><dd>{scopeText(d.scope)}</dd>
          <dt className="text-muted">Taken from</dt><dd>{basename(d.logical_path)}</dd>
          <dt className="text-muted">Status</dt><dd>{nodeStatus(n)}{n.provenance === "model" && n.state === "pending-confirmation" ? " (read by the AI from the file)" : ""}</dd>
        </dl>
      )}
      <Tech>
        <p className="text-xs text-muted">Provenance: {n.provenance}{n.source_revision_id ? ` - source revision ${n.source_revision_id}` : ""}{n.stale ? " - STALE" : ""}</p>
        <dl className="grid grid-cols-[8rem_1fr] gap-x-3 text-xs break-all">
          {Object.entries(d).filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [<dt key={k + "t"} className="text-muted">{k}</dt>, <dd key={k + "d"}>{typeof v === "string" ? v : JSON.stringify(v)}</dd>])}
        </dl>
      </Tech>
      <h3 className="font-semibold">Connections ({near.length})</h3>
      <ul className="text-sm grid gap-1 list-disc pl-5">
        {near.map((e) => (
          <li key={e.id}>
            <span>{e.from === n.id ? `this ${EDGE_BIZ[e.type] ?? e.type}: ${bl(e.to)}` : `${bl(e.from)} - ${EDGE_BIZ[e.type] ?? e.type} (to this item)`}{e.projection ? " - a guess from similar wording, not a dependency" : ""}</span>
            <Tech><span className="text-muted"> [{e.type}{e.relation_type ? `:${e.relation_type}` : ""} {e.state}{e.projection ? ", projection" : ""}] {e.from === n.id ? "→" : "←"} {rawLabel(e.from === n.id ? e.to : e.from)}{e.reason ? ` - ${e.reason}` : ""}</span></Tech>
          </li>
        ))}
      </ul>
      {isCand && <Muted>Confirming knowledge records a decision; it does not change the source file. Value changes go through Changes.</Muted>}
      {isCand && s.can("intent.create") && n.state !== "stale" && (
        <div><Button data-testid="request-change" onClick={() => { location.hash = hashFor(ws, "changes", undefined, { rule: n.id }); }}>Request a change to this rule</Button></div>
      )}
      {isCand && s.can("knowledge.confirm") && n.state !== "confirmed" && n.state !== "stale" && <ConfirmPanel id={n.id} reload={reload} />}
      {n.type === "finding" && n.state === "authority-decided" && <Muted>An authority decision exists; the opposing source file still says what it said.</Muted>}
      {n.type === "finding" && n.state !== "authority-decided" && s.can("conflict.resolve") && d.kind === "CONFLICT" && <ResolvePanel id={n.id} cands={cands} reload={reload} />}
      {isCand && <div className="grid gap-2"><h3 className="font-semibold">History of this rule</h3><Timeline tl={tl} /></div>}
    </section>
  );
}

export function ExplorerPage() {
  const s = useSession();
  const ws = s.route.ws!;
  const [g, setG] = React.useState<Graph | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [sel, setSel] = React.useState<string | null>(s.route.params.node ?? null);
  const [query, setQuery] = React.useState("");
  const [state, setState] = React.useState("");
  const [view, setView] = React.useState<"tree" | "graph">("tree");
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set());
  const [closedInSearch, setClosedInSearch] = React.useState<Set<string>>(new Set());
  const [focusId, setFocusId] = React.useState<string | null>(null);
  const initialised = React.useRef(false);
  const sessionRefresh = s.refresh;
  const load = React.useCallback(async () => {
    try { setG(await api<Graph>("GET", `/v2/knowledge/graph?workspace_id=${q(ws)}`)); setErr(null); void sessionRefresh(); }
    catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws, sessionRefresh]);
  React.useEffect(() => { void load(); }, [load]);
  const tree = React.useMemo(() => (g ? buildTree(g.nodes, g.edges) : null), [g]);

  // Once, when the first data arrives: files open (when the workspace is small) and a deep link opens its ancestors and takes focus.
  React.useEffect(() => {
    if (!tree || initialised.current) return;
    initialised.current = true;
    const open = new Set<string>();
    if (tree.totals.rules <= AUTO_OPEN_FILES_UP_TO) tree.files.forEach((f) => open.add(f.id));
    const want = s.route.params.node;
    if (want && tree.home.has(want)) { openPath(tree, want).forEach((x) => open.add(x)); setFocusId(tree.home.get(want)!.rule ?? want); }
    setExpanded(open);
  }, [tree, s.route.params.node]);
  // a new search starts without collapsed leftovers; clearing it restores exactly what the person had open
  React.useEffect(() => { setClosedInSearch(new Set()); }, [query, state]);

  const select = React.useCallback((id: string) => {
    setSel(id);
    const r = s.route;
    replaceHash(hashFor(ws, "explorer", r.sub, { ...r.params, node: id }));
  }, [s.route, ws]);
  const toggle = React.useCallback((id: string, wasOpen: boolean) => {
    const searching = !!query.trim() || !!state;
    if (searching && wasOpen) { setClosedInSearch((c) => new Set(c).add(id)); return; }
    setClosedInSearch((c) => { if (!c.has(id)) return c; const n = new Set(c); n.delete(id); return n; });
    setExpanded((e) => { const n = new Set(e); if (wasOpen) n.delete(id); else n.add(id); return n; });
    if (!wasOpen) select(id);
  }, [query, state, select]);
  const reveal = React.useCallback((id: string) => {
    if (!tree) return;
    setView("tree"); setQuery(""); setState("");
    setExpanded((e) => { const n = new Set(e); openPath(tree, id).forEach((x) => n.add(x)); return n; });
    select(id);
    setFocusId(tree.home.get(id)?.rule ?? id);
  }, [tree, select]);

  const vis = React.useMemo(() => (tree ? filterTree(tree, query, state) : null), [tree, query, state]);
  const visible = React.useMemo(() => (g ? g.nodes.filter((n) => matches(n, query, state)) : []), [g, query, state]);
  const flow = React.useMemo(() => {
    if (!g || !tree) return { nodes: [] as Node<KData>[], edges: [] as Edge[] };
    const pos = layout(visible);
    const ids = new Set(visible.map((n) => n.id));
    const nearIds = new Set<string>();
    if (sel) g.edges.forEach((e) => { if (e.from === sel) nearIds.add(e.to); if (e.to === sel) nearIds.add(e.from); });
    const nodes: Node<KData>[] = visible.map((n) => ({
      id: n.id, type: "k", position: pos[n.id], draggable: false, connectable: false,
      data: { n, label: s.mode === "technical" ? n.label : graphLabel(n, tree, false), selected: n.id === sel, near: nearIds.has(n.id), onSelect: select },
    }));
    const edges: Edge[] = g.edges.filter((e) => ids.has(e.from) && ids.has(e.to)).map((e) => ({
      id: e.id, source: e.from, target: e.to, type: "smoothstep", className: `${e.projection ? "rp-edge-projection" : "rp-edge"}${sel && (e.from === sel || e.to === sel) ? " rp-edge-active" : ""}`, selectable: false,
      ariaLabel: `${e.type}${e.projection ? " (proposed link, not proof)" : ""}`,
    }));
    return { nodes, edges };
  }, [g, tree, visible, sel, select, s.mode]);

  if (err && (err.status === 403 || err.status === 404)) return (<><PageHeader id="explorer" /><Denied err={err} /></>);
  if (err) return (<><PageHeader id="explorer" /><Callout tone="danger" testid={err.status === 403 || err.status === 404 ? "link-unavailable" : "state-error"}>{err.status === 403 || err.status === 404 ? "This page is not available to your account. Nothing was changed and nothing from that workspace is shown." : "The server could not complete the request."} <Button onClick={() => void load()}>Try again</Button></Callout></>);
  if (!g || !tree) return (<><PageHeader id="explorer" /><StatusBadge status="loading" /></>);
  const states = Array.from(new Set(g.nodes.filter((n) => n.type === "artifact" || n.type === "candidate" || n.type === "finding").map((n) => n.state))).sort();
  const cov = g.coverage;
  const upd = { edited: 0, reread: 0, scope: 0 };       // plain loop: this is after the early returns, so no hook here
  for (const n of g.nodes) {
    if (n.type !== "candidate" || n.state !== "updated") continue;
    const h = ((n.detail ?? {}) as { history?: { update_kind?: string; source_edited_by_apply?: string | null } }).history ?? {};
    if (h.update_kind === "scope_only") upd.scope++; else if (h.source_edited_by_apply) upd.edited++; else upd.reread++;
  }
  const T = tree.totals;
  const unanalysed = ((cov.artifacts ?? []) as Rec[]).filter((a) => a.extraction !== "PENDING_HUMAN");
  const shownRules = vis ? vis.rules.size : T.rules;
  return (
    <>
      <PageHeader id="explorer" next={T.rules ? "Open a file, then a rule, to see its evidence, history and what you can decide." : "Analyse a source file to see rules here."} />
      <div className="grid gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Search"><TextInput data-testid="explorer-search" value={query} onChange={(e) => setQuery(e.target.value)} /></Field>
          <Field label="State">
            <select data-testid="explorer-state" className="border border-border rounded-sm bg-bg px-3 py-2 min-h-10" value={state} onChange={(e) => setState(e.target.value)}>
              <option value="">All</option>{states.map((x) => <option key={x} value={x}>{nodePhrase(x)}</option>)}
            </select>
          </Field>
          <Button data-testid="explorer-mode" aria-pressed={view === "graph"} onClick={() => setView(view === "tree" ? "graph" : "tree")}>{view === "tree" ? <><Network size={16} aria-hidden="true" />Graph view</> : <><TreePine size={16} aria-hidden="true" />Back to the tree</>}</Button>
          {view === "tree" && (
            <div className="flex flex-wrap gap-2" role="group" aria-label="Open and close">
              <Button data-testid="expand-files" onClick={() => setExpanded((e) => { const n = new Set(e); tree.files.forEach((f) => n.add(f.id)); return n; })}>Open all files</Button>
              <Button data-testid="expand-rules" onClick={() => setExpanded((e) => { const n = new Set(e); let k = 0; for (const f of tree.files) { n.add(f.id); for (const r of f.rules) { if (k < OPEN_RULES_LIMIT) { n.add(r.id); k += 1; } } } return n; })}>Open the first {OPEN_RULES_LIMIT} rules</Button>
              <Button data-testid="collapse-all" onClick={() => { setExpanded(new Set()); setClosedInSearch(new Set()); }}>Close all</Button>
            </div>
          )}
        </div>
        <p data-testid="explorer-counts" className="text-sm text-muted">
          {s.mode === "technical"
            ? `${visible.length} of ${g.nodes.length} nodes - ${g.edges.length} edges - source revision ${g.head.source_revision_id || "none"} - knowledge revision ${g.head.knowledge_revision_id || "none"} - decision epoch ${g.head.decision_epoch}`
            : `${T.files} files, ${T.rules} rules read by the AI: ${T.confirmed} confirmed by a person, ${T.pending} not yet confirmed, ${upd.edited} changed by an applied change, ${upd.reread} read again with a different value, ${upd.scope} read again with a different scope only, ${g.nodes.filter((n) => n.state === "stale" && n.type === "candidate").length} out of date (their file changed after they were read)${cov.unchanged_since_read_candidates ? `, ${cov.unchanged_since_read_candidates} read at an earlier revision from a file that has not changed since (still valid)` : ""}. ${T.issues} issue${T.issues === 1 ? "" : "s"} and ${T.decisions} decision${T.decisions === 1 ? "" : "s"}, each counted once even when it touches several rules. ${vis ? `Showing ${shownRules} matching rule${shownRules === 1 ? "" : "s"}.` : "Showing everything."}`}
        </p>
        {view === "tree" ? (
          <div id="explorer-main" className="min-w-0">
            <ExplorerTree tree={tree} vis={vis} query={query} expanded={expanded} closedInSearch={closedInSearch} toggle={toggle} reveal={reveal} sel={sel} select={select} reload={load} focusId={focusId} />
          </div>
        ) : (
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
            <div id="explorer-main" className="min-w-0">
              {!visible.length ? <div data-testid="state-empty" className="rounded-lg border border-border bg-surface p-6">No item matches the filter.</div> : (
                <div data-testid="graph-svg" role="group" aria-label="Knowledge graph. Use the tree view for the same information as text." className="h-[600px] rounded-lg border border-border bg-surface">
                  <ReactFlow nodes={flow.nodes} edges={flow.edges} nodeTypes={NODE_TYPES} fitView minZoom={0.2} maxZoom={1.6} nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} proOptions={{ hideAttribution: false }}>
                    <Background /><Controls showInteractive={false} />
                  </ReactFlow>
                </div>
              )}
            </div>
            <aside id="explorer-detail" className="rounded-lg border border-border bg-surface p-4 min-w-0 lg:max-h-[620px] lg:overflow-auto"><Detail g={g} tree={tree} id={sel} reload={load} /></aside>
          </div>
        )}
        <Card title="Coverage and limits" testid="explorer-coverage">
          <ul className="list-disc pl-5 text-sm grid gap-1">
            {unanalysed.map((a) => <li key={a.logical_path}>{s.mode === "technical" ? `${a.logical_path}: ${a.extraction} - not part of the rules above` : `${basename(a.logical_path)}: not analysed - the rules in it are unknown, not absent`}</li>)}
            <li>Out-of-date rules (the file changed after they were read): {cov.stale_candidates}; rules read at an earlier revision from a file that has not changed since, so still valid: {cov.unchanged_since_read_candidates ?? 0}; rules without evidence: {cov.candidates_without_evidence}</li>
            <li>Comparisons between rules that were left out: {cov.relation_omitted_comparisons} (of which identity pairs: {cov.relation_omitted_identity_comparisons}); compared and found not to be the same rule: {cov.relation_compared_not_same_rule}. A rule with no link shown was not necessarily compared with every other.</li>
            {cov.truncated && <li>The graph was cut at its size limit, so some items are not shown.</li>}
            {cov.evidence_redacted && <li>Evidence quotes are hidden for your role.</li>}
          </ul>
          <Muted>{cov.note}</Muted>
        </Card>
        <Card title="Legend (icon + text)">
          <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">{Object.keys(SYMBOL).map((k) => <li key={k}>{SYMBOL[k]} {s.mode === "technical" ? `${k} - ${STATE_HELP[k]}` : nodePhrase(k)}</li>)}</ul>
          <Muted>Solid line = where something comes from (file, quoted text, person decision). Dashed amber line = a proposed link between two rules because they look alike: it is NOT a proof that one depends on the other. "Not analysed" or "unknown" never means "not affected". The tree is one way of showing these records: the same rule or issue can be reached from several places, and is still one record.</Muted>
        </Card>
      </div>
    </>
  );
}
