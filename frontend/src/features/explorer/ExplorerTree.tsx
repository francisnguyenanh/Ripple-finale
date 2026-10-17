// Explorer as a tree you open and close: file -> rules read from it -> inline explanation (what it means, where it came from, why it needs
// confirmation, related items, history, next action). The header of every row is ONE button (chevron, icon, name, summary, state) that
// opens and closes it; actions sit beside the header and never toggle it. Everything comes from the same graph response as the graph view.
import * as React from "react";
import { ChevronDown, ChevronRight, FileCode2, FileSpreadsheet, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Callout, Muted, StatePill, Tech, TechField } from "@/components/bits";
import { basename, humanize, nodePhrase, ruleTitle, scopeText, valueText } from "@/lib/fmt";
import { plain } from "@/lib/reasons";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";
import { bizLabel, locText, nodeHelp, nodeHistoryLines, nodeStatus, STATE_HELP, sym, TYPE_LABEL } from "./explorerText";

export { locText };
import type { GNode } from "./layout";
import { ConfirmPanel, ResolvePanel, Timeline, useTimeline } from "./RuleParts";
import type { RuleRow, Tree, Visible } from "./tree";

const PAGE = 50;

/** The location of a quote. Code is named by its function or method: that name is the code's own text, so it is marked verbatim (not translated, not humanized). */
function LocView({ raw, tech, fallback }: { raw: unknown; tech: boolean; fallback: string }) {
  let o: unknown = raw;
  if (typeof raw === "string" && raw.trim().startsWith("{")) { try { o = JSON.parse(raw); } catch { o = raw; } }
  const r = o as Rec | null;
  if (!tech && r && typeof r === "object" && r.type === "code_span" && typeof r.qualname === "string" && r.qualname !== "<module>") {
    const kind = r.qualname.includes(".") ? "method" : "function";
    const part = r.parts ? `, part ${String(r.part)} of ${String(r.parts)}` : "";
    return <>{kind} <span data-verbatim>{r.qualname}</span>, lines {String(r.line_start)}-{String(r.line_end)}{part}</>;
  }
  return <>{fallback}</>;
}
const excerpt = (q: unknown, n = 140) => { const t = String(q ?? "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "..." : t; };

const FileIcon = ({ mt }: { mt: string }) => (mt.includes("spreadsheet") ? <FileSpreadsheet size={18} aria-hidden="true" /> : mt.includes("python") ? <FileCode2 size={18} aria-hidden="true" /> : <FileText size={18} aria-hidden="true" />);

export interface TreeProps {
  tree: Tree; vis: Visible | null; query: string;
  expanded: Set<string>; closedInSearch: Set<string>; toggle: (id: string, wasOpen: boolean) => void;
  reveal: (id: string) => void; sel: string | null; select: (id: string) => void; reload: () => Promise<void>; focusId: string | null;
}

function ruleCounts(r: RuleRow): string {
  const bits = [`${r.evidence.length} evidence`, `${r.issues.length} issue${r.issues.length === 1 ? "" : "s"}`, `${r.decisions.length} decision${r.decisions.length === 1 ? "" : "s"}`];
  return bits.join(", ");
}

function RulePanel({ r, tree, reload, reveal, fileName }: { r: RuleRow; tree: Tree; reload: () => Promise<void>; reveal: (id: string) => void; fileName: string }) {
  const s = useSession();
  const ws = s.route.ws!;
  const tech = s.mode === "technical";
  const n = r.node;
  const d = (n.detail ?? {}) as Rec;
  const tl = useTimeline(ws, n.id, true, tree);
  const redacted = !!d.evidence_redacted;       // the server leaves quotes out for a role without evidence.read: that is not "no evidence"
  const canConfirm = s.can("knowledge.confirm");
  return (
    <div data-testid="node-detail" data-detail-for={n.id} className="grid gap-4 border-l border-border pl-3 sm:pl-4 py-2 min-w-0">
      <section aria-label="What this means" className="grid gap-1">
        <h4 className="font-semibold">What this means</h4>
        <dl data-testid="node-facts" className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted">Rule</dt><dd>{ruleTitle(d.subject, d.predicate)}</dd>
          <dt className="text-muted">Value</dt><dd>{valueText(d.value, d.unit, d.unit)}</dd>
          <dt className="text-muted">Applies to</dt><dd>{scopeText(d.scope)}</dd>
          <dt className="text-muted">Taken from</dt><dd>{fileName}</dd>
          <dt className="text-muted">Status</dt><dd><StatePill state={n.state} label={`${sym(n.state)} ${nodeStatus(n)}`} testid="node-state" /></dd>
        </dl>
        {nodeHelp(n) && <p className="text-sm text-muted">{nodeHelp(n)}</p>}
        {nodeHistoryLines(n).map((x, i) => <p key={i} data-testid="node-history" className="text-sm text-muted">{x}</p>)}
        <Tech><p className="text-xs text-muted">{n.label} - {STATE_HELP[n.state] ?? ""}</p></Tech>
      </section>

      <section aria-label="Where it came from" className="grid gap-2">
        <h4 className="font-semibold">Where it came from</h4>
        {!redacted && r.evidence.length === 0 && <Callout tone="warn" testid="no-evidence">No quoted evidence was stored for this rule. Treat it with care until a person checks the file.</Callout>}
        {redacted && <Muted testid="evidence-hidden">The original quotes and where they sit are hidden for your role, which cannot read source evidence. {d.has_evidence ? "Evidence was stored for this rule." : "No evidence was stored for this rule."} The file it was read from is shown above.</Muted>}
        {r.evidence.map((e) => {
          const q = ((e.detail ?? {}) as Rec).quote;
          const loc = locText(((e.detail ?? {}) as Rec).locator, tech);
          return (
            <div key={e.id} data-node={e.id} data-type="evidence" data-testid="evidence-item" className="rounded-md border border-border p-2 text-sm grid gap-1 min-w-0">
              <span className="font-medium break-words">{fileName} - <LocView raw={((e.detail ?? {}) as Rec).locator} tech={tech} fallback={loc || "location not recorded"} /></span>
              {q ? <>
                <span data-verbatim className="break-words">{excerpt(q)}</span>
                {String(q).replace(/\s+/g, " ").trim().length > 140 && <details><summary className="cursor-pointer text-primary underline text-xs">Show the full quote</summary><blockquote data-verbatim className="mt-1 border-l-2 border-border pl-3 whitespace-pre-wrap break-words">{String(q)}</blockquote></details>}
                {/[^\x00-\x7F]/.test(String(q)) && <Muted>The quote is shown exactly as written in the file. No English interpretation was stored for it.</Muted>}
              </> : null}
            </div>
          );
        })}
      </section>

      <section aria-label="Why it needs confirmation" className="grid gap-1">
        <h4 className="font-semibold">{n.state === "confirmed" ? "What was confirmed" : "Why it needs confirmation"}</h4>
        {r.decisions.length === 0 && <p className="text-sm">{n.state === "confirmed" ? "It is marked as confirmed." : "The AI read this rule from the file. No person has confirmed it yet, so it is not treated as knowledge."}</p>}
        {r.decisions.map((x) => { const dd = (x.detail ?? {}) as Rec; return <p key={x.id} data-node={x.id} data-type="decision" className="text-sm">{dd.action === "resolve" ? "Authority chosen" : "Confirmed"} by <strong>{String(dd.actor_id ?? "a person")}</strong> (decision {String(dd.epoch ?? "")}). The source file was not changed.</p>; })}
      </section>

      <section aria-label="Related items" className="grid gap-2">
        <h4 className="font-semibold">Related items</h4>
        {!r.issues.length && !r.related.length && <Muted>No issues or related rules were found for this rule.</Muted>}
        {r.issues.map((f) => {
          const fd = (f.detail ?? {}) as Rec;
          const involved = [...tree.files.flatMap((x) => x.rules)].filter((x) => x.issues.some((i) => i.id === f.id));
          const cands = involved.map((x) => x.node);
          return (
            <div key={f.id} data-node={f.id} data-type="finding" data-state={f.state} className="rounded-md border border-border p-2 grid gap-1 text-sm">
              <span><strong>{TYPE_LABEL.finding}: {humanize(fd.kind)}</strong> <StatePill state={f.state} label={`${sym(f.state)} ${nodePhrase(f.state)}`} /></span>
              {fd.reason ? <span className="break-words">{plain(String(fd.reason), tech)}</span> : null}
              {involved.length > 1 && <span className="text-muted">Also involves: {involved.filter((x) => x.id !== r.id).map((x) => <button key={x.id} type="button" className="underline text-primary cursor-pointer mr-2" onClick={() => reveal(x.id)}>{bizLabel(x.node)}</button>)}</span>}
              {f.state === "authority-decided" && <Muted>An authority decision exists; the opposing source file still says what it said.</Muted>}
              {f.state !== "authority-decided" && s.can("conflict.resolve") && fd.kind === "CONFLICT" && <ResolvePanel id={f.id} cands={cands} reload={reload} />}
            </div>
          );
        })}
        {r.related.length > 0 && (
          <ul className="grid gap-1 text-sm" aria-label="Related rules">
            {r.related.map(({ node, edge }) => (
              <li key={edge.id + node.id}>
                <button type="button" data-testid="related-rule" className="underline text-primary cursor-pointer text-left" onClick={() => reveal(node.id)}>{bizLabel(node)}</button>
                <span className="text-muted"> - {edge.projection ? "looks like the same rule (a guess from similar wording, not a dependency)" : "related"}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Before, after and history" className="grid gap-1">
        <h4 className="font-semibold">Before, after and history</h4>
        <Timeline tl={tl} />
      </section>

      <section aria-label="Next action" className="grid gap-2">
        <h4 className="font-semibold">Next action</h4>
        {n.state === "stale" ? <p className="text-sm">This rule was read from an older version of the file, and the file has changed since. Read the current rule instead.</p>
          : n.state === "confirmed" ? <p className="text-sm">Nothing to confirm. Confirming never edits a file: to change the value in the source, ask for a change.</p>
          : canConfirm ? <p className="text-sm">You can confirm this rule as knowledge. It records your decision and does not change the source file.</p>
          : <p className="text-sm">A person with the Knowledge owner role, or an Administrator, confirms rules. You can read it and ask for a change.</p>}
        {s.can("intent.create") && n.state !== "stale" && <div><Button data-testid="request-change" onClick={() => { location.hash = hashFor(ws, "changes", undefined, { rule: n.id }); }}>Request a change to this rule</Button></div>}
        {canConfirm && n.state !== "confirmed" && n.state !== "stale" && <ConfirmPanel id={n.id} reload={reload} />}
      </section>

      <Tech>
        <p className="text-xs text-muted">Provenance: {n.provenance}{n.source_revision_id ? ` - source revision ${n.source_revision_id}` : ""}{n.stale ? " - STALE" : ""}</p>
        {n.file_unchanged_since_read ? <p className="text-xs text-muted">Read at an earlier revision. The file has not changed since, so this rule is still valid.</p> : null}
        <TechField label="rule id" value={n.id} />
        <dl className="grid grid-cols-[8rem_1fr] gap-x-3 text-xs break-all">
          {Object.entries(d).filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [<dt key={k + "t"} className="text-muted">{k}</dt>, <dd key={k + "d"}>{typeof v === "string" ? v : JSON.stringify(v)}</dd>])}
        </dl>
      </Tech>
    </div>
  );
}

function RuleItem({ r, p, fileName }: { r: RuleRow; p: TreeProps; fileName: string }) {
  const s = useSession();
  const tech = s.mode === "technical";
  const searching = !!p.vis;
  const childHit = searching && [...r.evidence, ...r.issues, ...r.decisions].some((x) => p.vis!.hits.has(x.id));
  const open = (p.expanded.has(r.id) || childHit) && !p.closedInSearch.has(r.id);
  const n = r.node;
  const d = (n.detail ?? {}) as Rec;
  const title = tech ? n.label : `${ruleTitle(d.subject, d.predicate)} = ${valueText(d.value, d.unit, d.unit)}`;
  const hdr = `hdr-${r.id}`;
  const sel = p.sel === r.id;
  return (
    <li className="min-w-0">
      <div data-node={r.id} data-type="candidate" data-state={n.state} className={`flex flex-wrap items-start gap-2 rounded-md px-2 py-1.5 ${sel ? "bg-surface-2" : ""}`}>
        <h3 className="min-w-0 basis-full sm:basis-0 sm:flex-1">
          <button id={hdr} type="button" aria-expanded={open} aria-controls={`panel-${r.id}`} data-testid="rule-toggle" onClick={() => p.toggle(r.id, open)}
            className="flex w-full items-start gap-2 text-left cursor-pointer rounded-sm">
            {open ? <ChevronDown size={18} aria-hidden="true" className="mt-0.5 shrink-0" /> : <ChevronRight size={18} aria-hidden="true" className="mt-0.5 shrink-0" />}
            <span aria-hidden="true" className="w-4 shrink-0 text-center mt-0.5">{sym(n.state)}</span>
            <span className="min-w-0 flex-1">
              <span className="block font-medium break-words">{title}</span>
              <span className="block text-sm text-muted break-words">{scopeText(d.scope)} - {nodeStatus(n)} - {ruleCounts(r)}</span>
            </span>
          </button>
        </h3>
        <span className="hidden sm:inline-flex"><StatePill state={n.state} label={nodeStatus(n)} /></span>
      </div>
      <div id={`panel-${r.id}`} hidden={!open} className="ml-2 sm:ml-6">
        {open && <RulePanel r={r} tree={p.tree} reload={p.reload} reveal={p.reveal} fileName={fileName} />}
      </div>
    </li>
  );
}

function FileItem({ f, p }: { f: Tree["files"][number]; p: TreeProps }) {
  const s = useSession();
  const tech = s.mode === "technical";
  const searching = !!p.vis;
  const open = (p.expanded.has(f.id) || (searching && p.vis!.files.has(f.id))) && !p.closedInSearch.has(f.id);
  const [shown, setShown] = React.useState(PAGE);
  const d = (f.node.detail ?? {}) as Rec;
  const analysed = d.extraction === "PENDING_HUMAN";
  const rules = searching ? f.rules.filter((r) => p.vis!.rules.has(r.id)) : f.rules;
  const confirmed = f.rules.filter((r) => r.node.state === "confirmed").length;
  const pending = f.rules.filter((r) => r.node.state === "pending-confirmation").length;
  const name = tech ? f.node.label : basename(f.node.label);
  const summary = !analysed && !f.rules.length ? "Not analysed - the rules in this file are unknown, which is not the same as having none"
    : !f.rules.length ? "Analysed - no rules were found in this file"
    : `${f.rules.length} rule${f.rules.length === 1 ? "" : "s"}: ${confirmed} confirmed, ${pending} waiting for a person${f.rules.length - confirmed - pending > 0 ? `, ${f.rules.length - confirmed - pending} other` : ""}`;
  return (
    <li className="min-w-0 rounded-lg border border-border bg-surface">
      <div data-node={f.id} data-type="artifact" data-state={f.node.state} className="flex flex-wrap items-start gap-2 p-3">
        <h2 className="min-w-0 basis-full sm:basis-0 sm:flex-1">
          <button id={`hdr-${f.id}`} type="button" aria-expanded={open} aria-controls={`panel-${f.id}`} data-testid="file-toggle" onClick={() => p.toggle(f.id, open)}
            className="flex w-full items-start gap-2 text-left cursor-pointer rounded-sm">
            {open ? <ChevronDown size={18} aria-hidden="true" className="mt-0.5 shrink-0" /> : <ChevronRight size={18} aria-hidden="true" className="mt-0.5 shrink-0" />}
            <span className="mt-0.5 shrink-0"><FileIcon mt={String(d.media_type ?? "")} /></span>
            <span className="min-w-0 flex-1">
              <span className="block font-semibold break-all">{name}</span>
              <span className="block text-sm text-muted break-words">{sym(f.node.state)} {summary}</span>
            </span>
          </button>
        </h2>
        <a className="text-primary underline text-sm self-center" href={hashFor(s.route.ws!, "sources")} data-testid="file-open-sources">Open in Sources</a>
      </div>
      <div id={`panel-${f.id}`} hidden={!open} className="px-3 pb-3">
        {open && (rules.length ? (
          <ul className="grid gap-1" aria-label={`Rules read from ${name}`}>
            {rules.slice(0, shown).map((r) => <RuleItem key={r.id} r={r} p={p} fileName={basename(f.node.label)} />)}
            {rules.length > shown && <li><Button data-testid="show-more-rules" onClick={() => setShown((x) => x + PAGE)}>Show {Math.min(PAGE, rules.length - shown)} more of {rules.length - shown} remaining</Button></li>}
          </ul>
        ) : <Muted>{searching ? "No rule here matches the search." : analysed ? "The analysis found no rules in this file." : "No rules are shown because this file has not been analysed. Open Sources to analyse it."}</Muted>)}
      </div>
    </li>
  );
}

export function ExplorerTree(p: TreeProps) {
  const { tree, vis } = p;
  const files = vis ? tree.files.filter((f) => vis.files.has(f.id)) : tree.files;
  // move keyboard focus to a row that a deep link or a "related rule" link opened
  React.useEffect(() => {
    if (!p.focusId) return;
    const t = window.setTimeout(() => { const el = document.getElementById(`hdr-${p.focusId}`); el?.scrollIntoView({ block: "center" }); el?.focus(); }, 60);
    return () => window.clearTimeout(t);
  }, [p.focusId]);
  if (!files.length) return <div data-testid="state-empty" className="rounded-lg border border-border bg-surface p-6">{vis ? "Nothing matches the search or filter. Clear it to see everything again." : "There are no source files in this workspace yet."}</div>;
  return (
    <ul data-testid="explorer-tree" aria-label="Source files and the rules read from them" className="grid gap-3">
      {files.map((f) => <FileItem key={f.id} f={f} p={p} />)}
      {!vis && tree.looseIssues.length > 0 && (
        <li className="rounded-lg border border-border bg-surface p-3 grid gap-1" data-testid="loose-issues">
          <h2 className="font-semibold">Issues not tied to a rule</h2>
          {tree.looseIssues.map((f) => <p key={f.id} data-node={f.id} data-type="finding" data-state={f.state} className="text-sm">{humanize((f.detail as Rec | undefined)?.kind)}: {String((f.detail as Rec | undefined)?.reason ?? "")} <StatePill state={f.state} label={nodePhrase(f.state)} /></p>)}
        </li>
      )}
    </ul>
  );
}

export type { GNode };
