// Small shared building blocks: stage card, state pill, technical field with copy, labelled inputs.
import * as React from "react";
import { Check, CircleDashed, Copy, Lock, SearchX, TriangleAlert } from "lucide-react";
import { useSession } from "@/session";
import { cn } from "@/lib/cn";
import { ruleLine, ruleTitle, valueText } from "@/lib/fmt";
import { plain } from "@/lib/reasons";
import { useNotify } from "@/lib/notify";
import { Button } from "@/components/ui/button";
import { PILL_LABEL_MAX, stateTone, TONE_CLASS } from "@/lib/stateDisplay";

/** Semantic left accent (SCN-01): a selective 4px marker on a neutral surface; always paired with text that says the same (colour is never the only carrier). */
export type Accent = "primary" | "warning" | "danger" | "success" | "info";
export const ACCENT_CLASS: Record<Accent, string> = {
  primary: "border-l-4 border-l-primary", warning: "border-l-4 border-l-warning", danger: "border-l-4 border-l-danger", success: "border-l-4 border-l-success", info: "border-l-4 border-l-info",
};

/** A short kicker above a title, only when it adds information the title does not carry (12px floor; never the only label of a fact). */
export const Eyebrow = ({ children, testid }: { children: React.ReactNode; testid?: string }) => (
  <p data-testid={testid} className="text-xs font-semibold uppercase tracking-wider text-muted">{children}</p>
);

/** Panel (SCN-01, SCN-13). With a header (`title`, `aside`, `eyebrow`, `description` or `meta`, e.g. "read at ...") the panel is split: header strip
 *  (`px-4 py-3`, bottom rule) and body (`p-4`); without one it is a single padded body. `flush` drops the body padding so a list, table or canvas meets the panel
 *  edges (cells keep their own padding and focus ring; nothing is clipped); `accent` adds a semantic left marker; `level` picks the heading level so a panel inside a
 *  page section keeps the outline. The panel never clips its content (no overflow hidden), so focus rings at the edge stay visible. */
export function Card({ title, testid, children, className, aside, data, eyebrow, description, meta, flush, accent, level = 2, id }: {
  title?: React.ReactNode; testid?: string; children: React.ReactNode; className?: string; aside?: React.ReactNode; data?: Record<string, string>;
  eyebrow?: React.ReactNode; description?: React.ReactNode; meta?: React.ReactNode; flush?: boolean; accent?: Accent; level?: 2 | 3; id?: string;
}) {
  const extra: Record<string, string> = {};
  for (const [k, v] of Object.entries(data ?? {})) extra[`data-${k}`] = v;
  const H = level === 3 ? "h3" : "h2";
  const rich = !!(eyebrow || description || meta);
  const frame = cn("rounded-lg border border-border bg-surface shadow-card min-w-0", accent && ACCENT_CLASS[accent]);
  if (!(title || aside || rich))
    return <section data-testid={testid} {...extra} data-flush={flush ? "true" : undefined} className={cn(frame, flush ? "grid" : "grid gap-3 p-4", className)}>{children}</section>;
  return (
    <section data-testid={testid} {...extra} data-flush={flush ? "true" : undefined} className={cn(frame, "grid content-start", className)}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-4 py-3">
        {rich ? (
          <div className="grid gap-0.5 min-w-0 flex-1">
            {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
            {title && <H id={id} className={cn("font-semibold break-words", level === 3 ? "text-base" : "text-lg")}>{title}</H>}
            {description && <p className="text-sm text-muted">{description}</p>}
          </div>
        ) : title && <H id={id} className={cn("font-semibold flex-1 min-w-0", level === 3 ? "text-base" : "text-lg")}>{title}</H>}
        {meta && <span className="text-xs text-muted">{meta}</span>}
        {aside}
      </header>
      <div className={flush ? "min-w-0" : "grid gap-3 p-4 min-w-0"}>{children}</div>
    </section>
  );
}

/** SCN-11: a state with no rows, said for what it is. `kind`: none (nothing exists yet, as far as this read shows), no-match (a filter hides everything: offer to
 *  clear it), error (the read failed: never "nothing here"; offer a retry when the route allows), partial (some parts could not be read), denied (says nothing about
 *  whether the item exists). The reason must come from what is known; `action` is an allowed next step owned by the feature (no mutation or paid call here). */
export type EmptyKind = "none" | "no-match" | "error" | "partial" | "denied";
export function EmptyState({ kind, title, children, action, testid, compact }: { kind: EmptyKind; title: React.ReactNode; children?: React.ReactNode; action?: React.ReactNode; testid?: string; compact?: boolean }) {
  const Icon = { none: CircleDashed, "no-match": SearchX, error: TriangleAlert, partial: TriangleAlert, denied: Lock }[kind];
  const warn = kind === "error" || kind === "partial";
  return (
    <div data-testid={testid} data-empty={kind} role={warn ? "status" : undefined}
      className={cn("grid gap-2 rounded-lg border bg-surface", compact ? "p-3" : "p-5", warn ? "border-warning border-l-4" : "border-border")}>
      <p className="flex items-start gap-2 font-medium"><Icon size={18} aria-hidden="true" className={cn("mt-0.5 shrink-0", warn ? "text-warning" : "text-muted")} /><span>{title}</span></p>
      {children ? <div className="text-sm text-muted grid gap-1">{children}</div> : null}
      {action ? <div className="flex flex-wrap gap-2">{action}</div> : null}
    </div>
  );
}

/** One labelled fact, value first in weight (SCN-01 summary datum). A missing value reads `unknown` ("Not recorded" by default), never 0 or blank; the label is never
 *  dropped. Spans only, so it can sit inside a button or a list row. */
export function Datum({ label, value, hint, testid, unknown = "Not recorded", size = "md", className }: {
  label: React.ReactNode; value: React.ReactNode | null | undefined; hint?: React.ReactNode; testid?: string; unknown?: string; size?: "md" | "lg"; className?: string;
}) {
  const missing = value === null || value === undefined || value === "";
  return (
    <span data-testid={testid} data-state={missing ? "unknown" : "known"} className={cn("grid gap-0.5 min-w-0 content-start", className)}>
      <span className="text-xs font-semibold uppercase tracking-wider text-muted">{label}</span>
      <span className={cn("font-semibold break-words", size === "lg" ? "text-2xl tnum" : "text-base", missing && "text-muted font-medium")}>{missing ? unknown : value}</span>
      {hint ? <span className="text-sm text-muted">{hint}</span> : null}
    </span>
  );
}

export const Muted = ({ children, testid, className, id }: { children: React.ReactNode; testid?: string; className?: string; id?: string }) => (
  <p id={id} data-testid={testid} className={cn("text-sm text-muted", className)}>{children}</p>
);

export function Callout({ tone = "info", children, testid, role }: { tone?: "info" | "warn" | "danger" | "wait"; children: React.ReactNode; testid?: string; role?: string }) {
  // SCN-01: attention tones carry a 4px left marker on the neutral surface (the text still says what it is); info stays a plain 1px frame.
  const border = { info: "border-border", warn: "border-warning border-l-4", danger: "border-danger border-l-4", wait: "border-info border-l-4" }[tone];
  return <div role={role} data-testid={testid} className={cn("rounded-md border bg-surface-2 px-3 py-2 text-sm grid gap-1", border)}>{children}</div>;
}

/** Routine explanation (how a screen works, repeated definitions), collapsed by default and reachable by keyboard / touch (native details). Never put a blocker, a
 *  consequence of the next action, a paid-use note or a stale / partial warning in here: those stay visible beside the action (CONTENT_AND_STATES). */
export function HowItWorks({ children, testid, title = "How this works" }: { children: React.ReactNode; testid?: string; title?: string }) {
  return (
    <details data-testid={testid} className="text-sm rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer w-fit text-primary">{title}</summary>
      <div className="mt-2 grid gap-1">{children}</div>
    </details>
  );
}

/** A state: business wording in Business view, the raw state name in Technical view. data-state keeps the machine value. */
export function StatePill({ state, label, testid, data, detail }: { state: string; label: string; testid?: string; data?: Record<string, string>; detail?: React.ReactNode }) {
  const { mode } = useSession();
  const tone = TONE_CLASS[stateTone(state)];
  const extra: Record<string, string> = {};
  for (const [k, v] of Object.entries(data ?? {})) extra[`data-${k}`] = v;
  const text = mode === "technical" ? (label && label !== state ? `${state} - ${label}` : state) : label;
  // A long label is an instruction, not a status: it wraps in a rounded box instead of a clipped pill.
  const long = text.length > PILL_LABEL_MAX;
  return (
    <>
      <span data-testid={testid} data-state={state} {...extra} className={cn("inline-flex items-center border border-border px-2 py-0.5 text-xs font-medium", long ? "rounded-md max-w-full whitespace-normal" : "rounded-full", tone, state === "RUNNING" && "gap-1.5 border-warning")}>
        {/* RUNNING moves through a decorative dot only: the text never fades, so its contrast holds in every frame; no motion under reduced motion. */}
        {state === "RUNNING" && <span aria-hidden="true" data-running-indicator className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-warning motion-safe:animate-pulse" />}
        {text}
      </span>
      {detail ? <span className="text-sm text-muted"> {detail}</span> : null}
    </>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** A technical value with a human label and a copy action. Rendered only in the Technical view. */
export function TechField({ label, value }: { label: string; value: unknown }) {
  const { mode } = useSession();
  const [copied, setCopied] = React.useState<"" | "yes" | "no">("");
  if (mode !== "technical") return null;
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return (
    <div className="techfield flex flex-wrap items-center gap-2 text-sm" data-testid="tech-field">
      <span className="text-muted">{label}:</span>
      <code className="break-all">{text}</code>
      <Button variant="ghost" className="min-h-7 px-2 text-xs" aria-label={`Copy ${label}`} onClick={async () => setCopied((await copyText(text)) ? "yes" : "no")}>
        {copied === "yes" ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
        {copied === "yes" ? "Copied" : copied === "no" ? "Select and copy" : "Copy"}
      </Button>
    </div>
  );
}

export function Tech({ children }: { children: React.ReactNode }) {
  const { mode } = useSession();
  return mode === "technical" ? <>{children}</> : null;
}

const inputCls = "border border-border-strong rounded-sm bg-bg px-3 py-2 text-base font-normal w-full min-h-10";

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1 text-sm font-medium min-w-0">
      {label}
      {children}
      {hint && <span className="text-xs font-normal text-muted">{hint}</span>}
    </label>
  );
}

export const TextInput = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(function TextInput(p, ref) {
  return <input ref={ref} type="text" {...p} className={cn(inputCls, p.className)} />;
});
export const TextArea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(function TextArea(p, ref) {
  return <textarea ref={ref} {...p} className={cn(inputCls, "resize-y", p.className)} />;
});

const selectCls = "border border-border-strong rounded-sm bg-bg px-3 py-2 min-h-10 max-w-full min-w-0";
/** The shared native select: same look as the text controls; label it with `Field` or an `aria-label`. */
export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(function Select(p, ref) {
  return <select ref={ref} {...p} className={cn(selectCls, p.className)} />;
});

/** A name from the server: plain words in Business view, the exact machine name in Technical view. */
export function Name({ raw }: { raw: string }) {
  const { mode } = useSession();
  return <>{mode === "technical" ? raw : raw.replace(/[_.]+/g, " ").replace(/^./, (c) => c.toUpperCase()).toLowerCase().replace(/^./, (c) => c.toUpperCase())}</>;
}

/** A file: its name in Business view, the full stored path in Technical view. */
export function Path({ p }: { p: unknown }) {
  const { mode } = useSession();
  const full = String(p ?? "");
  return <>{mode === "technical" ? full : full.split("/").pop()}</>;
}

/** A rule, in words with its value and unit (Business) or exactly as stored (Technical). */
export function ClaimLabel({ c }: { c: Record<string, unknown> }) {
  const { mode } = useSession();
  if (mode === "technical")
    return <>{String(c.subject)} / {String(c.predicate)} = {String(c.value)}{c.unit ? ` ${c.unit}` : ""} ({String(c.logical_path || c.artifact_id || "unknown file")})</>;
  return <>{ruleLine(c.subject, c.predicate, valueText(c.value, c.unit, c.unit))}{c.logical_path ? ` (${String(c.logical_path).split("/").pop()})` : ""}</>;
}

/** Business-view wording of a server sentence (reason codes become words); Technical view keeps the exact text. */
export function usePlain(): (t: unknown) => string {
  const { mode } = useSession();
  return (t) => plain(t, mode === "technical");
}

/** A digest: a short fingerprint people can compare in Business view, the full value (copyable) in Technical view. */
export function Fingerprint({ value, testid }: { value: unknown; testid: string }) {
  const { mode } = useSession();
  const full = String(value ?? "");
  if (mode === "technical") return <code data-testid={testid} className="break-all">{full}</code>;
  return <code data-testid={`${testid}-short`} title="Short form of the exact fingerprint; the Technical view shows all of it">{full.replace(/^sha256:/, "").slice(0, 12)}{full.length > 12 ? "..." : ""}</code>;
}

/** A refused read (no access / not found): the plain panel, and the refusal is also announced in the error banner. */
export function Denied({ err, testid = "link-unavailable" }: { err: { status: number } & Error; testid?: string }) {
  const notify = useNotify();
  React.useEffect(() => { notify.fail(err); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div data-testid={testid} className="rounded-lg border border-border bg-surface p-4 grid gap-2">
      <h2 className="text-lg font-semibold">This page is not available to your account</h2>
      <p className="text-sm">The link may be incomplete, may belong to another workspace, or your account has no access to it. Nothing was changed and nothing from that workspace is shown.</p>
    </div>
  );
}

export function ClaimTitle({ c }: { c: Record<string, unknown> }) {
  const { mode } = useSession();
  return <>{mode === "technical" ? String(c.subject ?? c.predicate) : ruleTitle(c.subject, c.predicate)}</>;
}
