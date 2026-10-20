// One look for a model's reading of a person's words before anything is confirmed (UX-08 / UX-07): AI origin label, a heading that says how far the reading got,
// the person's quoted words, the fields that were read (a missing one says so), the parts that could not be read, assumptions and limits. Presentation only:
// the caller passes the validated server interpretation and its own confirm control; this shell never confirms, trims an uncovered part or shows reasoning.
import * as React from "react";
import { Muted } from "@/components/bits";
import { OriginTag } from "./Origin";

export interface PreviewField { key: string; label: string; value: string | null; source?: string | null }
export interface PreviewIds { field?: (key: string) => string; source?: (key: string) => string; fields?: string; uncovered?: string; uncoveredPart?: string; assumptions?: string }

export function AIInterpretationPreview({ status, heading, quoted, fields, uncovered = [], assumptions = [], limitations = [], children, footer, testid, ids = {}, version }: {
  status: string; heading: React.ReactNode; quoted?: string | null; fields?: PreviewField[] | null; uncovered?: string[]; assumptions?: string[]; limitations?: React.ReactNode[];
  children?: React.ReactNode; footer?: React.ReactNode; testid?: string; ids?: PreviewIds; version?: string;
}) {
  return (
    <div data-testid={testid} data-state={status} data-version={version} className="rounded-md border border-dashed border-origin-ai bg-surface-2 p-3 grid gap-2 anim-in">
      <p className="flex flex-wrap items-center gap-2 text-base font-semibold"><OriginTag origin="ai" label="AI reading" />{heading}</p>
      {quoted ? <Muted>Your words: "{quoted}"</Muted> : null}
      {fields && fields.length > 0 && (
        <dl data-testid={ids.fields} className="grid grid-cols-[minmax(8rem,12rem)_1fr] gap-x-3 gap-y-1 text-sm">
          {fields.map((f) => (
            <React.Fragment key={f.key}>
              <dt className="text-muted">{f.label}</dt>
              <dd data-testid={ids.field?.(f.key)} data-missing={f.value === null ? "true" : "false"}>{f.value === null ? <span className="text-muted">not known yet</span> : f.value}</dd>
              {f.source ? (<><dt aria-hidden="true" /><dd data-testid={ids.source?.(f.key)} className="text-xs text-muted">{f.source}</dd></>) : null}
            </React.Fragment>
          ))}
        </dl>
      )}
      {children}
      {uncovered.length > 0 && (
        <div data-testid={ids.uncovered}>
          <p className="text-sm font-medium">Not read, so nothing is proposed from it:</p>
          <ul className="list-disc pl-5 text-sm">{uncovered.map((u, i) => <li key={i} data-testid={ids.uncoveredPart}>{u}</li>)}</ul>
        </div>
      )}
      {assumptions.length > 0 && <Muted testid={ids.assumptions}>Assumed, please check: {assumptions.join("; ")}</Muted>}
      {limitations.map((l, i) => <Muted key={i}>{l}</Muted>)}
      {footer}
    </div>
  );
}
