import { AlertTriangle, CheckCircle2, CircleDashed, Clock, Info, Loader2, XCircle } from "lucide-react";
import { cn } from "@/lib/cn";

// Every status = icon + text label; colour is secondary (design system section 4).
export type Status = "loading" | "empty" | "queued" | "running" | "waiting" | "stale" | "partial" | "failed" | "success" | "info" | "simulated";

const MAP: Record<Status, { icon: typeof Info; tone: string; label: string }> = {
  loading: { icon: Loader2, tone: "text-muted", label: "Loading" },
  empty: { icon: CircleDashed, tone: "text-muted", label: "Nothing yet" },
  queued: { icon: Clock, tone: "text-info", label: "Queued" },
  running: { icon: Loader2, tone: "text-info", label: "Running" },
  waiting: { icon: Clock, tone: "text-warning", label: "Waiting for approval" },
  stale: { icon: AlertTriangle, tone: "text-warning", label: "Out of date" },
  partial: { icon: AlertTriangle, tone: "text-warning", label: "Partial" },
  failed: { icon: XCircle, tone: "text-danger", label: "Failed" },
  success: { icon: CheckCircle2, tone: "text-success", label: "Done" },
  info: { icon: Info, tone: "text-info", label: "Note" },
  simulated: { icon: Info, tone: "text-sim", label: "SIMULATED" },
};

export function StatusBadge({ status, label, className }: { status: Status; label?: string; className?: string }) {
  const m = MAP[status];
  const Icon = m.icon;
  return (
    <span data-testid={status === "loading" ? "state-loading" : "status-badge"} data-state={status} className={cn("inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs font-medium", m.tone, className)}>
      <Icon size={14} aria-hidden="true" className={status === "running" || status === "loading" ? "motion-safe:animate-spin" : ""} />
      {label ?? m.label}
    </span>
  );
}
