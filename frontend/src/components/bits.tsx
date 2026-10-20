// Small shared building blocks: stage card, state pill, technical field with copy, labelled inputs.
import * as React from "react";
import { Check, Copy } from "lucide-react";
import { useSession } from "@/session";
import { cn } from "@/lib/cn";
import { ruleLine, ruleTitle, valueText } from "@/lib/fmt";
import { plain } from "@/lib/reasons";
import { useNotify } from "@/lib/notify";
import { Button } from "@/components/ui/button";
import { PILL_LABEL_MAX, stateTone, TONE_CLASS } from "@/lib/stateDisplay";

export function Card({ title, testid, children, className, aside, data }: { title?: React.ReactNode; testid?: string; children: React.ReactNode; className?: string; aside?: React.ReactNode; data?: Record<string, string> }) {
  const extra: Record<string, string> = {};
  for (const [k, v] of Object.entries(data ?? {})) extra[`data-${k}`] = v;
  return (
    <section data-testid={testid} {...extra} className={cn("rounded-lg border border-border bg-surface p-4 md:p-5 grid gap-3 min-w-0", className)}>
      {(title || aside) && (
        <header className="flex flex-wrap items-center gap-2">
          {title && <h2 className="text-lg font-semibold flex-1 min-w-0">{title}</h2>}
          {aside}
        </header>
      )}
      {children}
    </section>
  );
}

export const Muted = ({ children, testid, className, id }: { children: React.ReactNode; testid?: string; className?: string; id?: string }) => (
  <p id={id} data-testid={testid} className={cn("text-sm text-muted", className)}>{children}</p>
);

export function Callout({ tone = "info", children, testid, role }: { tone?: "info" | "warn" | "danger" | "wait"; children: React.ReactNode; testid?: string; role?: string }) {
  const border = { info: "border-border", warn: "border-warning", danger: "border-danger", wait: "border-info" }[tone];
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

const inputCls = "border border-border rounded-sm bg-bg px-3 py-2 text-base font-normal w-full min-h-10";

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

const selectCls = "border border-border rounded-sm bg-bg px-3 py-2 min-h-10 max-w-full min-w-0";
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
