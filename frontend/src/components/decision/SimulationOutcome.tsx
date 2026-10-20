// UX-09: what a deterministic sample run measured: how many cases ran, how many changed outcome (and whether the edited values explain them), the outcome counts
// before / after and the changed cases (capped, with the full count stated). Engine values only; an AI reading of them is shown elsewhere and apart.
import * as React from "react";
import { ArrowRight } from "lucide-react";
import { Muted, Tech } from "@/components/bits";
import { humanize } from "@/lib/fmt";
import type { Outcome } from "./outcomeModel";

type Rec = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export function SimulationOutcome({ o, tech }: { o: Outcome; tech: boolean }) {
  return (
    <>
      <p data-testid="sim-summary" className="text-sm">
        {o.total as React.ReactNode} public {o.invoices ? "invoices" : "requisitions"} simulated. {o.changedCount} change their outcome{o.unexplained ? ` (${o.unexplained} NOT explained by the edited ${tech ? "keys" : "values"})` : ` (all explained by the edited ${tech ? "keys" : "values"})`}.
      </p>
      {o.routes.length > 0 && (
        <table className="text-sm border border-border w-full max-w-md">
          <caption className="sr-only">Outcomes before and after the edit</caption>
          <thead><tr className="bg-surface-2 text-left"><th className="p-2">Outcome</th><th className="p-2 tnum">Before</th><th className="p-2 tnum">After</th></tr></thead>
          <tbody>{o.routes.map((r) => <tr key={r.key} className="border-t border-border"><td className="p-2">{tech ? r.key : humanize(r.key)}</td><td className="p-2 tnum">{r.before}</td><td className="p-2 tnum">{r.after}</td></tr>)}</tbody>
        </table>
      )}
      {o.changedCount ? (
        <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Table (scrolls sideways on a small screen)">
          <table data-testid="sim-changed" className="text-sm border border-border w-full">
            <caption className="sr-only">Cases that change their outcome</caption>
            <thead><tr className="bg-surface-2 text-left">{[o.invoices ? "Invoice" : "Request", "Amount JPY", "Detail", "Before", "After", "Cause"].map((t) => <th key={t} className="p-2">{t}</th>)}</tr></thead>
            <tbody>
              {o.cases.map((c: Rec) => (
                <tr key={c.id} data-request={c.id} className="border-t border-border align-top">
                  <td className="p-2">{c.id}</td><td className="p-2 tnum">{c.amount_jpy}</td><td className="p-2">{c.label}</td>
                  <td className="p-2">{tech ? c.before.outcome : humanize(c.before.outcome)} <Tech>({c.before.reason}{c.before.due_date ? `, due ${c.before.due_date}` : ""})</Tech></td>
                  <td className="p-2"><ArrowRight size={12} aria-hidden="true" className="inline mr-1" />{tech ? c.after.outcome : humanize(c.after.outcome)} <Tech>({c.after.reason}{c.after.due_date ? `, due ${c.after.due_date}` : ""})</Tech></td>
                  <td className="p-2">{((c.cause ?? []) as Rec[]).length ? <><span>the edited value</span><Tech> {(c.cause as Rec[]).map((x) => `${x.config_key} ${x.from}->${x.to}${x.code_ref ? ` [${x.code_ref.file}:${x.code_ref.line}]` : ""}`).join("; ")}</Tech></> : "not explained"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <Muted>No requisition changes its outcome under this edit.</Muted>}
      {o.changedCount > o.cases.length && <Muted>Showing {o.cases.length} of {o.changedCount} changed cases.</Muted>}
    </>
  );
}
