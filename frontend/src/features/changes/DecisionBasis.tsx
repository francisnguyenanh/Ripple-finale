// What the approver decides ON, in one place, built only from what the server already returned to THIS role (no raw source quotes:
// those need evidence.read and stay hidden). Approve is offered only when the required basis is present; what is missing is said.
import { stageNo } from "./registry";
import { Check, Minus, X } from "lucide-react";
import { Callout, Muted, Tech } from "@/components/bits";
import { basename } from "@/lib/fmt";
import type { FlowData, Rec } from "./types";
import { isDone } from "./types";
import type { Notes } from "./ReviewNotes";

export interface Basis {
  rows: { key: string; ok: boolean | null; title: string; text: string; tech?: string }[];
  /** Why Approve is not offered (empty = the required basis is complete). Reject is never blocked by this. */
  missing: string[];
}

const show = (v: unknown): string => (v === null || v === undefined ? "?" : String(v));

export function decisionBasis(d: FlowData, notes: Notes | null, canReadEvidence: boolean): Basis {
  const pr = (d.proposal?.execution_result ?? {}) as Rec;
  const edits = ((pr.edits ?? []) as Rec[]);
  const lines = ((d.diff?.lines ?? []) as Rec[]).filter((l) => l.kind === "add" || l.kind === "remove");
  const noop = !!(d.diff?.is_no_op ?? pr.is_no_op);
  const file = d.diff?.logical_path ? basename(String(d.diff.logical_path)) : "the file";
  const ar = (d.approval?.execution_result ?? {}) as Rec;
  const snap = (ar.snapshot ?? {}) as Rec;
  const current = (!!snap.source_revision_id && snap.source_revision_id === d.head.source_revision_id && snap.decision_epoch === d.head.decision_epoch) || !!d.apply;     // applied: history, not out of date
  const rr = (d.review?.execution_result ?? {}) as Rec;
  const checks = Object.values((rr.checks ?? {}) as Rec) as Rec[];
  const passed = checks.filter((c) => c.status === "PASS").length;
  const blockers = ((ar.blocked_reasons ?? []) as Rec[]).length;
  const contracts = d.contracts ?? [];
  const sim = d.simulation && isDone(d.simulation) ? ((d.simulation.execution_result ?? {}) as Rec) : null;
  const hb = sim?.harbor && sim.status === "EXECUTED" ? (sim.harbor as Rec) : null;
  const open = notes?.open_change_requests ?? [];
  const nNotes = notes?.notes.length ?? 0;

  const rows: Basis["rows"] = [
    {
      key: "change", ok: !noop && (edits.length > 0 || lines.length > 0), title: "What changes",
      text: edits.length
        ? edits.map((e) => `${file}: ${show(e.path ?? e.cell)} ${show(e.before)} → ${show(e.after)}`).join("; ")
        : noop ? "This proposal changes nothing." : lines.length ? `${lines.length} changed line(s) in ${file} (see the exact diff in stage ${stageNo("proposal")}).` : "No exact before/after is available.",
    },
    {
      key: "source", ok: current, title: "Source and revision",
      text: current ? `Built on the current source revision of ${file}; nothing has changed since.` : "The workspace changed after this bundle was prepared: it is out of date and cannot be approved.",
      tech: `snapshot revision ${show(snap.source_revision_id)}; head ${show(d.head.source_revision_id)}`,
    },
    {
      key: "review", ok: isDone(d.review) && blockers === 0, title: "Review",
      text: isDone(d.review) ? `${passed} of ${checks.length} checks passed; ${blockers} blocker(s) reported.` : "The review has not finished.",
    },
    {
      key: "contract", ok: contracts.length > 0, title: "Check after the change (stated independently)",
      text: contracts.length ? contracts.map((c) => `${basename(c.logical_path)} must say ${show(c.expected)} (set by ${c.author_account_id})`).join("; ") : "Nobody has stated the value that must hold after the change.",
    },
    {
      key: "simulation", ok: hb ? true : null, title: "Simulation and its limits",
      text: hb ? `${hb.changed_count} of ${hb.transactions} sample cases would change their outcome. Limits are listed under the decision brief.` : "No simulation backs this change. That is not a failure, but the effect on cases is not measured.",
    },
    {
      key: "notes", ok: open.length === 0, title: "Reviewer notes",
      text: open.length ? `${open.join(", ")} asked for changes: not approvable until marked ready.` : nNotes ? `${nNotes} note(s), none asking for changes.` : "No reviewer wrote a note (a review is optional).",
    },
    {
      key: "quotes", ok: null, title: "Original source quotes",
      text: canReadEvidence ? "Your role may read the original quotes (Sources / Explorer)." : "Hidden from your role by design. You decide on the exact change, the review, the independent check and the notes above.",
    },
  ];
  const missing: string[] = [];
  if (!rows[0].ok) missing.push("the exact before/after is missing or the change is a no-op");
  if (!rows[1].ok) missing.push("the bundle is out of date");
  if (!rows[2].ok) missing.push(isDone(d.review) ? "the review reports blockers" : "the review has not finished");
  const reviewNamed = !!d.review?.payload?.verification_contract_id;
  if (!rows[3].ok) missing.push(`the independent check after the change has not been stated (state it in stage ${stageNo("check")} 'Check after the change', then the requester runs the review again and prepares a new approval bundle)`);
  else if (isDone(d.review) && !reviewNamed) missing.push("the review ran BEFORE the check was stated, so it tested nothing against it: the requester must run the review again and prepare a new approval bundle");
  if (open.length) missing.push("a reviewer asked for changes");
  return { rows, missing };
}

export function DecisionBasis({ basis }: { basis: Basis }) {
  return (
    <div data-testid="decision-basis" data-complete={basis.missing.length === 0 ? "true" : "false"} className="grid gap-2 rounded-md border border-border p-3">
      <h3 className="font-semibold">What you are deciding on</h3>
      <ul className="grid gap-1 text-sm">
        {basis.rows.map((r) => (
          <li key={r.key} data-testid={`basis-${r.key}`} data-ok={String(r.ok)} className="flex gap-2">
            {r.ok === true ? <Check size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-success" /> : r.ok === false ? <X size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-danger" /> : <Minus size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-muted" />}
            <span><strong>{r.title}.</strong> {r.text}{r.tech ? <Tech><span className="text-muted"> ({r.tech})</span></Tech> : null}</span>
          </li>
        ))}
      </ul>
      {basis.missing.length > 0
        ? <Callout tone="warn" testid="basis-missing"><strong>Approve is not available yet</strong><span>Missing: {basis.missing.join("; ")}. You can still reject, or wait for it to be completed.</span></Callout>
        : <Muted testid="basis-complete">The required basis is complete. Approving records your decision with your name and reason; it changes no file by itself.</Muted>}
    </div>
  );
}
