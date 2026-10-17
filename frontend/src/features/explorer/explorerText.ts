// Words the Explorer uses for states, kinds and links. Business wording is deterministic formatting of the stored fields.
import { basename, humanize, nodePhrase, ruleLine, scopeText, valueText } from "@/lib/fmt";
import type { Rec } from "@/features/changes/types";
import type { GNode } from "./layout";

export const SYMBOL: Record<string, string> = {
  observed: "●", "not-analysed": "○", "pending-confirmation": "?", confirmed: "✓", conflict: "✕", "scope-mismatch": "≠", gap: "∅", ambiguity: "~",
  "authority-decided": "⚖", stale: "⌛", proposed: "◇", updated: "↻",
};
export const STATE_HELP: Record<string, string> = {
  observed: "observed from the file", "not-analysed": "not analysed (no completed extraction)", "pending-confirmation": "pending confirmation",
  confirmed: "confirmed by a person (source file unchanged)", conflict: "conflict finding open", "scope-mismatch": "scope mismatch - needs review, not automatically a conflict",
  gap: "gap - expected coverage missing", ambiguity: "ambiguity / insufficient context", "authority-decided": "authority chosen; opposing source still exists",
  stale: "stale - from an older source revision", proposed: "proposed (projection, not a confirmed relation)", updated: "updated - read again and differs from the earlier reading (see history); earlier decisions are not carried over",
};
export const STATE_BIZ_HELP: Record<string, string> = {
  observed: "Read directly from the file by the parser.",
  "not-analysed": "The AI has not finished reading this file, so any rules in it are unknown. This is not the same as having none.",
  "pending-confirmation": "The AI read this rule from the file. No person has confirmed it yet.",
  confirmed: "A person confirmed this rule. The source file itself was not changed.",
  conflict: "Two sources disagree and nobody has decided which one is right.",
  "scope-mismatch": "The sources talk about different cases. Review it; it is not automatically a conflict.",
  gap: "Something that should be covered is missing.",
  ambiguity: "There is not enough context to be sure what this means.",
  "authority-decided": "A person chose which source is authoritative. The other source still says what it said.",
  stale: "This comes from an older version of the source and is no longer current.",
  proposed: "A guess from similar wording. It is not proof that one thing depends on the other.",
  updated: "The rule was read again and differs from the earlier reading. Earlier decisions about the old reading are NOT carried over: a person has to decide again.",
};
export const TYPE_LABEL: Record<string, string> = { artifact: "File", evidence: "Evidence", candidate: "Rule", finding: "Finding", decision: "Decision" };
export const EDGE_BIZ: Record<string, string> = {
  "source-of": "comes from the file", "evidence-of": "is backed by this quoted text", "involved-in": "is part of this finding",
  "decided-by": "was decided by a person", relation: "proposed link to another rule (not proven)",
};
export const EVENT_BIZ: Record<string, string> = {
  extracted: "Read from the file by the AI", value_changed_in_source: "The rule reads differently than in the earlier reading",
  "decision:confirm": "Confirmed by a person (the file was not changed)", source_changed: "The source file was changed", knowledge_revision: "Knowledge accepted", answer: "Comment or answer",
};
export const sym = (s: string) => SYMBOL[s] ?? "•";

/** What the stored provenance says about a rule that was read again. Never inferred from time or from a revision name:
 *  `history` is written by the server from the apply job result and the extraction job's own payload. */
type Hist = { update_kind?: string | null; source_edited_by_apply?: string | null; read_again_after_apply?: string | null; file_unchanged_since_read?: boolean };
const hist = (n: GNode): Hist => ((n.detail ?? {}) as { history?: Hist }).history ?? {};
export function nodeStatus(n: GNode): string {
  const h = hist(n);
  if (n.state !== "updated") return nodePhrase(n.state);
  if (h.update_kind === "value_changed") return h.source_edited_by_apply ? "Value changed by an applied change" : "Value read differently than in the earlier reading";
  if (h.update_kind === "scope_only") return "Read again with a different scope (value unchanged)";
  return nodePhrase(n.state);
}
export function nodeHistoryLines(n: GNode): string[] {
  const h = hist(n), out: string[] = [];
  if (h.source_edited_by_apply) out.push("The source file was edited by an applied change.");
  if (h.read_again_after_apply) out.push("This rule was read again because of an applied change.");
  if (h.file_unchanged_since_read) out.push("Read at an earlier revision; the file has not changed since, so it is still valid.");
  if (n.state === "updated" && h.update_kind === "scope_only") out.push("Only the scope differs from the earlier reading; the value is the same and the file was not edited by an applied change.");
  if (n.state === "updated" && h.update_kind === "value_changed" && !h.source_edited_by_apply) out.push("No applied change edited this file; the AI read a different value this time.");
  return out;
}
export function nodeHelp(n: GNode): string {
  if (n.state !== "updated") return STATE_BIZ_HELP[n.state] ?? "";
  return "The rule was read again and differs from the earlier reading. Earlier decisions about the old reading are NOT carried over: a person has to decide again.";
}

/** A stored rule value or file state in words (Business view of the timeline). */
export function describeState(v: Rec): string {
  if (v && typeof v === "object" && "value" in v) {
    const scope = v.scope && Object.keys(v.scope).length ? ` (applies to ${scopeText(v.scope)})` : "";
    return `${valueText(v.value, v.unit, v.unit)}${scope}`;
  }
  if (v && typeof v === "object" && "blob_hash" in v) return "the file content";
  return "recorded";
}

export function whenWho(e: Rec, technical: boolean): string {
  const when = e.ts ? new Date(e.ts).toLocaleString() : "";
  const who = technical ? e.actor : String(e.actor ?? "").startsWith("model") || String(e.actor ?? "").startsWith("service") ? "the AI" : e.actor;
  const bits = [when, who];
  if (technical) bits.push(e.source_revision_id && `source ${e.source_revision_id}`, e.epoch !== undefined && `epoch ${e.epoch}`);
  return bits.filter(Boolean).join(" - ");
}

/** Business wording of a node: deterministic formatting of the extracted fields. */
export function bizLabel(n: GNode): string {
  const d = (n.detail ?? {}) as Rec;
  if (n.type === "candidate") return ruleLine(d.subject, d.predicate, valueText(d.value, d.unit, d.unit));
  if (n.type === "artifact") return basename(n.label);
  if (n.type === "finding") return humanize(d.kind) + (d.reason ? `: ${String(d.reason).slice(0, 60)}` : "");
  if (n.type === "evidence") return "Quoted text";
  return n.label;
}

/** Where in the file a quote sits, in words (Business) or exactly as stored (Technical). The server sends the locator as an object or as JSON text. */
export function locText(loc: unknown, technical = false): string {
  let o: unknown = loc;
  if (typeof loc === "string" && loc.trim().startsWith("{")) { try { o = JSON.parse(loc); } catch { o = loc; } }
  if (technical) return typeof o === "string" ? o : JSON.stringify(o);
  if (o && typeof o === "object") {
    const r = o as Rec;
    if (r.type === "code_span" && typeof r.qualname === "string") {
      const where = r.qualname === "<module>" ? "module-level code" : r.qualname.includes(".") ? `method ${r.qualname}` : `function ${r.qualname}`;
      const part = r.parts ? `, part ${String(r.part)} of ${String(r.parts)}` : "";
      return `${where}, lines ${String(r.line_start)}-${String(r.line_end)}${part}`;
    }
    return Object.entries(r).filter(([k]) => k !== "type").map(([k, v]) => {
      const t = typeof v === "object" ? JSON.stringify(v) : String(v);
      if (k === "key_path") return `setting "${t.split(".").map((x) => x.replace(/_/g, " ")).join(" > ")}"`;
      if (k === "line" || k === "row" || k === "page" || k === "cell" || k === "sheet" || k === "column") return `${k} ${t}`;
      return `${humanize(k).toLowerCase()} ${t.replace(/_/g, " ")}`;
    }).join(", ");
  }
  return String(o ?? "");
}
