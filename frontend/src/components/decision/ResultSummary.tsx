// A titled outcome block: what this is about, how far its facts reach, warnings that must be read first, then the content and action.
// Presentation only: the owning feature passes already-authorised facts and the action element; nothing is fetched or mutated here.
import * as React from "react";
import { Callout } from "@/components/bits";
import { cn } from "@/lib/cn";

export interface Notice { key: string; tone: "info" | "warn" | "danger" | "wait"; text: React.ReactNode; blocking?: boolean }
export type Availability = "loading" | "ready" | "empty" | "unavailable" | "denied";

export function ResultSummary({ title, scopeLabel, availability = "ready", warnings = [], action, meta, children, testid, className }: {
  title: React.ReactNode; scopeLabel?: React.ReactNode; availability?: Availability; warnings?: Notice[]; action?: React.ReactNode; meta?: React.ReactNode;
  children?: React.ReactNode; testid?: string; className?: string;
}) {
  return (
    <section data-testid={testid} data-availability={availability} className={cn("grid gap-3 min-w-0", className)}>
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold">{title}</h2>
        {scopeLabel && <span className="text-sm text-muted">{scopeLabel}</span>}
        {meta}
      </header>
      {/* Warnings come before the content and the action, even when the content is collapsed or unavailable. */}
      {warnings.map((w) => <Callout key={w.key} tone={w.tone} role={w.blocking ? "alert" : "status"}>{w.text}</Callout>)}
      {availability === "unavailable" && warnings.length === 0 && <Callout tone="warn" role="status">This could not be read, so nothing is shown instead of a guess.</Callout>}
      {children}
      {action}
    </section>
  );
}
