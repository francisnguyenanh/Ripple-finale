// Shared DataTable (SCN-09): typed columns over rows the owning feature already read. Presentation only: no fetch, no mutation, no state the caller cannot see.
// - Loaded mode only: filtering, sorting and paging (when enabled) act on the rows passed in, and the table says so (`scope`); there is no server mode until an API
//   offers sort / cursor / coverage metadata, so no control pretends to page the server.
// - Data states are explicit (`state`): loading, ready, empty, error, partial; an error or partial read is never rendered as an empty table.
// - Sorting uses each column's typed `sortValue` (number / string / null; nulls last), never the formatted text, with the original order as the stable tie-break.
// - Essential columns are always rendered; under 768 px the table becomes labelled stacked rows (`.rp-stack`, index.css), so no column is dropped on a phone.
// - `flush` (SCN-15): the table sits edge to edge in a flush panel: header band and row rules reach the panel edges, the first and last cells line up with the
//   panel header's 16 px padding, and the loading / empty / error states keep their own padding. Cell padding, controls and focus rings are never removed.
import * as React from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { EmptyState } from "@/components/bits";

export type SortValue = number | string | null | undefined;
export interface Column<T> {
  id: string; header: string; cell: (row: T) => React.ReactNode; sortValue?: (row: T) => SortValue; essential?: boolean; align?: "start" | "end"; className?: string;
}
export type TableState = "loading" | "ready" | "empty" | "error" | "partial";

export function compareSort(a: SortValue, b: SortValue): number {
  const na = a === null || a === undefined || a === "", nb = b === null || b === undefined || b === "";
  if (na || nb) return na === nb ? 0 : na ? 1 : -1;                                   // missing values last in both directions
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/** Stable sort of loaded rows: the original index breaks ties, so equal keys keep the server's order. */
export function sortRows<T>(rows: T[], col: Column<T> | undefined, dir: "asc" | "desc"): T[] {
  if (!col?.sortValue) return rows;
  const get = col.sortValue;
  return rows.map((r, i) => [r, i] as const).sort(([ra, ia], [rb, ib]) => {
    const va = get(ra), vb = get(rb);
    const missing = (v: SortValue) => v === null || v === undefined || v === "";
    if (missing(va) || missing(vb)) return missing(va) === missing(vb) ? ia - ib : missing(va) ? 1 : -1;
    const c = compareSort(va, vb);
    return c !== 0 ? (dir === "asc" ? c : -c) : ia - ib;
  }).map(([r]) => r);
}

export function DataTable<T>({ columns, rows, rowKey, caption, captionHidden = false, state = "ready", scope, empty, error, rowProps, density = "comfortable", sortable = false, flush = false, testid }: {
  columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string; caption: React.ReactNode; captionHidden?: boolean; state?: TableState; scope?: React.ReactNode;
  empty?: React.ReactNode; error?: React.ReactNode; rowProps?: (row: T) => Record<string, string | undefined>; density?: "comfortable" | "compact"; sortable?: boolean; flush?: boolean; testid?: string;
}) {
  const [sort, setSort] = React.useState<{ id: string; dir: "asc" | "desc" } | null>(null);
  const shown = sortable && sort ? sortRows(rows, columns.find((c) => c.id === sort.id), sort.dir) : rows;
  const pad = cn(density === "compact" ? "px-3 py-1.5" : "px-3 py-2.5", flush && "max-md:px-4 md:first:pl-4 md:last:pr-4");
  const inset = (n: React.ReactNode) => (flush ? <div className="px-4 py-3">{n}</div> : n);
  if (state === "loading") return inset(<p role="status" data-testid={testid ? `${testid}-loading` : undefined} className={cn("text-sm text-muted", !flush && "px-3 py-2")}>Loading…</p>);
  if (state === "error") return inset(<EmptyState kind="error" compact testid={testid ? `${testid}-error` : undefined} title={error ?? "This list could not be read. It is not empty; try again."} />);
  if (rows.length === 0 && state !== "partial") return inset(typeof empty === "string" || empty === undefined
    ? <EmptyState kind="none" compact testid={testid ? `${testid}-empty` : undefined} title={empty ?? "Nothing to show."} />
    : <>{empty}</>);
  const toggle = (id: string) => setSort((s) => (s?.id !== id ? { id, dir: "asc" } : s.dir === "asc" ? { id, dir: "desc" } : null));
  return (
    <div className="min-w-0" data-testid={testid} data-state={state}>
      <table className="rp-stack w-full border-collapse text-sm">
        <caption className={cn("text-left", captionHidden ? "sr-only" : cn("pb-2 font-semibold text-base", flush ? "px-4" : "px-3"))}>
          {caption}{scope ? <span className="sr-only"> ({typeof scope === "string" ? scope : "loaded rows only"})</span> : null}
        </caption>
        <thead>
          <tr className="border-y border-t-border border-b-border-strong bg-surface-2 text-left">
            {columns.map((c) => (
              <th key={c.id} scope="col" aria-sort={sort?.id === c.id ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
                className={cn(pad, "text-xs font-semibold uppercase tracking-wider text-muted", c.align === "end" && "text-right")}>
                {sortable && c.sortValue ? (
                  <button type="button" className="inline-flex items-center gap-1 rounded-sm uppercase tracking-wider" onClick={() => toggle(c.id)} data-testid={testid ? `${testid}-sort-${c.id}` : undefined}>
                    {c.header}{sort?.id === c.id ? (sort.dir === "asc" ? <ArrowUp size={12} aria-hidden="true" /> : <ArrowDown size={12} aria-hidden="true" />) : <ArrowUpDown size={12} aria-hidden="true" />}
                  </button>
                ) : c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={rowKey(r)} {...(rowProps?.(r) ?? {})} className="border-b border-border last:border-b-0 align-top transition-colors hover:bg-surface-2/50">
              {columns.map((c) => <td key={c.id} data-label={c.header} className={cn(pad, "min-w-0 break-words", c.align === "end" && "text-right", c.className)}>{c.cell(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {scope ? <p className={cn("pt-2 text-xs text-muted", flush ? "border-t border-border px-4 pb-3" : "px-3")} data-testid={testid ? `${testid}-scope` : undefined}>{scope}</p> : null}
    </div>
  );
}
