// SCN-20: Before / After of one rule change as two clause lists (side by side from 768 px, stacked below). The clause texts are the server's, untouched; each
// box's text content stays "Before: <the server string>" (the " | " delimiters are kept as hidden, unread separators), so nothing is rendered twice. Marks are
// neutral (primary marker for what changed or was added, a struck-through clause with its tag for what was removed): a change is never coloured good or bad.
// The tag words ("changed", "added", "removed") come from CSS generated content (index.css `.rp-clause`), read by screen readers, not part of the text content.
import { cn } from "@/lib/cn";
import { CLAUSE_SEP, clauseDiff, type Clause } from "./clauseDiff";

const MARK_CLASS: Record<Clause["mark"], string> = {
  same: "text-muted",
  changed: "border-l-2 border-primary bg-surface pl-1.5 font-semibold text-text",
  added: "border-l-2 border-primary bg-surface pl-1.5 font-semibold text-text",
  removed: "border-l-2 border-border-strong bg-surface pl-1.5 text-text line-through decoration-1",
};

function Side({ title, clauses, testid, strong }: { title: "Before" | "After"; clauses: Clause[]; testid: string; strong: boolean }) {
  return (
    <div data-testid={testid} className={cn("grid content-start gap-1.5 rounded-sm border p-2.5 min-w-0", strong ? "border-border-strong bg-surface-2/70" : "border-border bg-surface-2/40")}>
      <p className="text-xs font-semibold uppercase tracking-wider text-muted">{title}:<span className="hidden"> </span></p>
      <ul className="grid gap-1 text-sm" aria-label={title}>
        {clauses.map((c, i) => (
          <li key={i} data-testid="rev-clause" data-mark={c.mark} className={cn("rp-clause break-words", MARK_CLASS[c.mark])}>
            {c.text}{i < clauses.length - 1 && <span aria-hidden="true" className="hidden">{CLAUSE_SEP}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ClauseCompare({ before, after }: { before: unknown; after: unknown }) {
  const d = clauseDiff(before, after);
  if (!d.structured)
    return (
      <div className="grid gap-1 md:grid-cols-2 md:gap-4" data-testid="rev-clauses" data-structured="false">
        <p><span className="text-muted">Before:</span> {String(before ?? "")}</p>
        <p><span className="text-muted">After:</span> {String(after ?? "")}</p>
      </div>
    );
  return (
    <div className="grid gap-2 md:grid-cols-2" data-testid="rev-clauses" data-structured="true" data-aligned={d.aligned ?? undefined}>
      <Side title="Before" clauses={d.before} testid="rev-before" strong={false} />
      <Side title="After" clauses={d.after} testid="rev-after" strong />
    </div>
  );
}
