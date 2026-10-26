// The one next step and who it is for. The reason is visible text (never tooltip-only); the action element comes from the
// owning feature and does nothing on mount. `blocked` means the step cannot be taken yet; the reason says why.
import * as React from "react";
import { cn } from "@/lib/cn";

// `compact` (SCN-16): a one-line reminder with a primary left marker, for when the owning page has PROVEN that the action repeats a row shown right below it;
// the action element and its consequence stay.
export function NextActionPanel({ reason, actorLabel, blocked = false, action, secondary, compact = false, testid = "next-step" }: {
  reason: React.ReactNode; actorLabel?: React.ReactNode; blocked?: boolean; action?: React.ReactNode; secondary?: React.ReactNode; compact?: boolean; testid?: string;
}) {
  return (
    <div data-testid={testid} data-blocked={blocked ? "yes" : "no"} data-compact={compact ? "yes" : "no"}
      className={cn("mb-6 rounded-md border bg-surface text-sm flex flex-wrap items-center gap-x-4 gap-y-2", blocked ? "border-warning" : "border-border",
        compact ? "border-l-4 border-l-primary px-4 py-2.5 justify-between" : "px-3 py-2")}>
      <p className="min-w-0 flex-1">
        <strong>{blocked ? "Blocked:" : "Next step:"}</strong>{" "}
        {actorLabel && <span className="text-muted">({actorLabel}) </span>}
        {reason}
      </p>
      {action}
      {secondary}
    </div>
  );
}
