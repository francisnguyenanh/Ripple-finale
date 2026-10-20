// Loaded findings in labelled groups with exact counts and filter pills (UX-07). Presentation only: the caller supplies the groups, the counts and the card for
// each item; a filter hides loaded cards and never decides, dismisses or answers anything. Hidden cards stay mounted, so what a person typed in one is kept.
import * as React from "react";
import { Button } from "@/components/ui/button";

export interface TriageGroupView<T> { key: string; label: string; items: T[]; critical: number }
export interface TriagePill { key: string; label: string; count: number }

export function FindingsTriage<T>({ groups, pills, filter, onFilter, visible, itemKey, render, noun = "question", testid = "findings-triage" }: {
  groups: TriageGroupView<T>[]; pills: TriagePill[]; filter: string; onFilter: (key: string) => void; visible: (item: T) => boolean; itemKey: (item: T) => string; render: (item: T) => React.ReactNode;
  noun?: string; testid?: string;
}) {
  return (
    <div className="grid gap-4" data-testid={testid}>
      <div className="flex flex-wrap items-center gap-1 text-sm" role="group" aria-label={`Filter the ${noun}s`} data-testid={`${testid}-pills`}>
        {pills.map((p) => (
          <Button key={p.key} size="sm" variant={filter === p.key ? "primary" : "ghost"} aria-pressed={filter === p.key} data-testid={`${testid}-pill`} data-key={p.key}
            disabled={p.count === 0 && filter !== p.key} onClick={() => onFilter(p.key)}>{p.label} ({p.count})</Button>
        ))}
      </div>
      {groups.map((g) => (
        <section key={g.key} className="grid gap-3" data-testid={`${testid}-group`} data-key={g.key} data-count={g.items.length} aria-label={g.label} hidden={!g.items.some(visible)}>
          <h3 className="text-sm font-medium">{g.label} <span className="text-muted">({g.items.length} {g.items.length === 1 ? noun : `${noun}s`}{g.critical ? `, ${g.critical} need an answer` : ""})</span></h3>
          {g.items.map((it) => <div key={itemKey(it)} hidden={!visible(it)}>{render(it)}</div>)}
        </section>
      ))}
    </div>
  );
}
