// Local navigator of a long page: shows ONE section or all of them. Sections stay mounted (the page hides the others with `hidden`), so
// switching never discards typed text, starts a job or repeats a read. Which section is selected lives in the URL (`ui_section`).
import * as React from "react";
import { Button } from "@/components/ui/button";
import { useMedia } from "@/lib/useMedia";

export interface NavSection { id: string; label: string; group?: string; badge?: string }

export function SectionNav({ ariaLabel, testid, prefix, sections, selected, onSelect, groupLabels, allLabel = "All sections", compact = false }: {
  ariaLabel: string; testid: string; prefix: string; sections: NavSection[]; selected: string | null; onSelect: (id: string) => void;
  groupLabels?: Record<string, string>; allLabel?: string; compact?: boolean;
}) {
  const groups = groupLabels ? Object.keys(groupLabels) : [""];
  // SCN-05: on a narrow screen the navigator folds into one labelled disclosure that names the section shown, so the page's own content is not pushed below the
  // first screen; choosing closes it. SCN-17: on a wide screen it folds the same way only when the page says it is opened deep on one item (`compact`), with
  // "All sections" kept beside it. Same buttons, same URL and same mounted sections; one navigator at a time.
  const narrow = useMedia("(max-width: 767px)");
  const folded = narrow || compact;
  // Uncontrolled on purpose: a re-render (polling, a reload) must never re-apply a stale open state between the browser's own toggle and its event.
  const fold = React.useRef<HTMLDetailsElement>(null);
  const box = React.useRef<HTMLElement>(null);
  // A choice can switch the navigator between folded and full (the deep link no longer applies); the chosen button then takes focus in the new layout.
  const pending = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (pending.current === null) return;
    const id = pending.current;
    pending.current = null;
    const el = box.current?.querySelector<HTMLElement>(`[data-testid="${prefix}-${id || "all"}"]`);
    const shut = el?.closest("details");
    if (el && el.getClientRects().length && !(shut && !shut.open && !el.closest("summary"))) el.focus();
    else box.current?.querySelector<HTMLElement>(`[data-testid="${prefix}-toggle"]`)?.focus();     // the chosen button is inside the closed fold: its summary
  });
  const current = selected === null ? allLabel : sections.find((x) => x.id === selected)?.label ?? allLabel;
  const choose = (id: string) => { pending.current = id; onSelect(id); if (fold.current) fold.current.open = false; };
  if (folded)
    return (
      <nav ref={box} aria-label={ariaLabel} data-testid={testid} data-folded="true" data-compact={compact && !narrow ? "true" : undefined}
        className={compact && !narrow ? "mb-4 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2 rounded-lg border border-border bg-surface" : "mb-4 rounded-lg border border-border bg-surface"}>
        <details ref={fold} className="min-w-0">
          <summary className="cursor-pointer px-3 py-2 text-sm min-h-10 flex flex-wrap items-center gap-x-2" data-testid={`${prefix}-toggle`}>
            <span className="text-muted">Section:</span> <strong className="min-w-0 break-words">{current}</strong>
            {compact && !narrow && <span className="text-primary">(switch section)</span>}
          </summary>
          <div className="grid gap-3 border-t border-border p-3">
            {!(compact && !narrow) && (
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" variant={selected === null ? "primary" : "secondary"} aria-current={selected === null ? "true" : undefined} data-testid={`${prefix}-all`} onClick={() => choose("")}>{allLabel}</Button>
                {!groupLabels && sections.map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={choose} />)}
              </div>
            )}
            {compact && !narrow && !groupLabels && <div className="flex flex-wrap gap-2">{sections.map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={choose} />)}</div>}
            {groupLabels && groups.map((g) => (
              <div key={g} className="grid gap-1">
                <p className="text-sm font-medium">{groupLabels[g]}</p>
                <div className="flex flex-wrap gap-2">{sections.filter((x) => x.group === g).map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={choose} />)}</div>
              </div>
            ))}
          </div>
        </details>
        {compact && !narrow && (
          <div className="py-1 pr-2">
            <Button size="sm" variant="secondary" data-testid={`${prefix}-all`} onClick={() => choose("")}>{allLabel}</Button>
          </div>
        )}
      </nav>
    );
  return (
    <nav ref={box} aria-label={ariaLabel} data-testid={testid} className="mb-6 grid gap-3 rounded-lg border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={selected === null ? "primary" : "secondary"} aria-current={selected === null ? "true" : undefined} data-testid={`${prefix}-all`} onClick={() => choose("")}>{allLabel}</Button>
        {!groupLabels && sections.map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={choose} />)}
      </div>
      {groupLabels && groups.map((g) => (
        <div key={g} className="grid gap-1">
          <p className="text-sm font-medium">{groupLabels[g]}</p>
          <div className="flex flex-wrap gap-2">{sections.filter((x) => x.group === g).map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={choose} />)}</div>
        </div>
      ))}
    </nav>
  );
}

function Item({ x, prefix, selected, onSelect }: { x: NavSection; prefix: string; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <Button size="sm" variant={selected === x.id ? "primary" : "secondary"} aria-current={selected === x.id ? "true" : undefined} data-testid={`${prefix}-${x.id}`} onClick={() => onSelect(x.id)}>
      {x.label}{x.badge ? ` ${x.badge}` : ""}
    </Button>
  );
}
