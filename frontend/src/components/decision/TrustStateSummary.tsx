// Several independent completion axes shown side by side. They are never merged into one badge, percentage or green "verified":
// every axis keeps its own wording and a missing axis says "Unavailable". Presentation only; the caller supplies the facts.
import * as React from "react";
import { useSession } from "@/session";
import { axisDisplay } from "./trustAxes";

export interface Axis { key: string; label: string; raw: unknown }

export function TrustStateSummary({ axes, limitations = [], testid = "trust-axes" }: { axes: Axis[]; limitations?: React.ReactNode[]; testid?: string }) {
  const { mode } = useSession();
  return (
    <div data-testid={testid}>
      <ul className="flex flex-wrap gap-x-4 gap-y-2 text-sm">
        {axes.map((a) => {
          const d = axisDisplay(a.raw);
          return (
            <li key={a.key} data-axis={a.key} data-state={d.known ? String(a.raw) : "unavailable"} className="flex items-center gap-1.5">
              <span>{a.label}:</span>{" "}
              <span className={d.known ? "rounded-md border border-border px-2 py-0.5 text-xs font-medium" : "rounded-md border border-dashed border-border px-2 py-0.5 text-xs text-muted"}>{d.text}</span>
              {mode === "technical" && d.known && <code className="text-muted">{String(a.raw)}</code>}
            </li>
          );
        })}
      </ul>
      {limitations.map((l, i) => <p key={i} className="mt-1 text-sm text-muted">{l}</p>)}
    </div>
  );
}
