// Visual provenance (EUI-01): AI-written content and source / engine content look different in both themes and without colour. AI = dashed border + sparkle icon +
// "AI"; source = solid border + document icon; engine = solid border + gauge icon. Origin never says the content is correct, checked or approved; a check result or a
// human confirmation is shown by its own component. Presentation only.
import * as React from "react";
import { FileText, Gauge, CircleHelp, Sparkles } from "lucide-react";
import { originDisplay } from "./originDisplay";

const ICON = { ai: Sparkles, source: FileText, engine: Gauge } as const;

function look(origin: unknown) {
  const d = originDisplay(origin);
  const Icon = d.known ? ICON[origin as keyof typeof ICON] : CircleHelp;
  const tone = d.family === "ai" ? "text-origin-ai border-origin-ai border-dashed" : d.family === "evidence" ? "text-origin-evidence border-origin-evidence" : "text-muted border-border border-dashed";
  const surface = d.family === "ai" ? "bg-origin-ai-surface border-origin-ai border-dashed" : d.family === "evidence" ? "bg-origin-evidence-surface border-origin-evidence" : "bg-surface border-border border-dashed";
  return { d, Icon, tone, surface };
}

/** Inline origin label: icon + short text; `label` replaces the short text (e.g. "Task record"), the full note stays available to assistive technology. */
export function OriginTag({ origin, label }: { origin: unknown; label?: string }) {
  const { d, Icon, tone } = look(origin);
  return (
    <span data-testid="origin-tag" data-origin={d.known ? String(origin) : "unknown"} title={d.note}
      className={`inline-flex items-center gap-1 rounded-sm border px-1.5 text-xs font-medium align-middle ${tone}`}>
      <Icon aria-hidden="true" className="size-3" />{label ?? d.label}<span className="sr-only">: {d.note}</span>
    </span>
  );
}

/** A section whose whole content has one origin: left rule + tinted surface + origin label before the title. Keeps the caller's heading level and test id. */
export function OriginSection({ origin, title, label, children, testid, ariaLabel, status }:
  { origin: unknown; title: React.ReactNode; label?: string; children: React.ReactNode; testid?: string; ariaLabel?: string; status?: string }) {
  const { d, surface } = look(origin);
  return (
    <section aria-label={ariaLabel} data-testid={testid} data-status={status} data-origin={d.known ? String(origin) : "unknown"}
      className={`grid gap-1 rounded-md border-l-4 px-3 py-2 text-sm ${surface}`}>
      <h3 className="flex flex-wrap items-center gap-2 font-semibold"><OriginTag origin={origin} label={label} />{title}</h3>
      {children}
    </section>
  );
}
