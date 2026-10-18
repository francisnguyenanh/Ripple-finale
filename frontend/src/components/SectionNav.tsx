// Local navigator of a long page: shows ONE section or all of them. Sections stay mounted (the page hides the others with `hidden`), so
// switching never discards typed text, starts a job or repeats a read. Which section is selected lives in the URL (`ui_section`).
import { Button } from "@/components/ui/button";

export interface NavSection { id: string; label: string; group?: string; badge?: string }

export function SectionNav({ ariaLabel, testid, prefix, sections, selected, onSelect, groupLabels, allLabel = "All sections" }: {
  ariaLabel: string; testid: string; prefix: string; sections: NavSection[]; selected: string | null; onSelect: (id: string) => void;
  groupLabels?: Record<string, string>; allLabel?: string;
}) {
  const groups = groupLabels ? Object.keys(groupLabels) : [""];
  return (
    <nav aria-label={ariaLabel} data-testid={testid} className="mb-6 grid gap-3 rounded-lg border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={selected === null ? "primary" : "secondary"} aria-current={selected === null ? "true" : undefined} data-testid={`${prefix}-all`} onClick={() => onSelect("")}>{allLabel}</Button>
        {!groupLabels && sections.map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={onSelect} />)}
      </div>
      {groupLabels && groups.map((g) => (
        <div key={g} className="grid gap-1">
          <p className="text-sm font-medium">{groupLabels[g]}</p>
          <div className="flex flex-wrap gap-2">{sections.filter((x) => x.group === g).map((x) => <Item key={x.id} x={x} prefix={prefix} selected={selected} onSelect={onSelect} />)}</div>
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
