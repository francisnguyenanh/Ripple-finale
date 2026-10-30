// The two AI steps of a confirmed business change, asked from its review (D2a equivalent flow, owner decision 2026-10-27): the model dependency check and the
// guidance on the server-built draft. Reading shows the server's state; a step starts only when a person asks (one paid model call each, stated next to the button).
// The server decides who may ask, which attempt is next and why guidance may not run yet (`ai_steps` of the review); the routes check permissions, the provider
// allowlist and the budget again. Guidance is text over the server's draft: it adds, drops or upgrades nothing, and nothing is applied.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, HowItWorks, Muted } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import type { Status } from "@/components/StatusBadge";
import { OriginSection } from "@/components/decision/Origin";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const STATE: Record<string, { status: Status; label: string }> = {
  not_requested: { status: "info", label: "Not run" }, pending: { status: "running", label: "Running" }, completed: { status: "success", label: "Done" },
  failed: { status: "failed", label: "Did not finish" }, stale: { status: "stale", label: "Out of date" }, tampered: { status: "failed", label: "Not used (integrity)" },
  not_applicable: { status: "info", label: "Not needed" },
};
const badge = (st: unknown) => STATE[String(st)] ?? { status: "info" as Status, label: `State: ${String(st)}` };

function refusal(e: unknown): string {
  if (e instanceof ApiError) {
    const b = (e.body ?? {}) as Rec;
    if (e.status === 403) return "Your role cannot ask for this AI step. Nothing was started.";
    if (b.next_action) return `${String(b.error ?? "Not started.")} ${String(b.next_action)}`;
    if (b.error) return `${String(b.error)} Nothing was started.`;
  }
  return errorText(e);
}

export function DraftAiSteps({ ws, cid, draft, steps, knowledgeMoved, onChanged }:
  { ws: string; cid: string; draft: Rec; steps: Rec | undefined; knowledgeMoved: boolean; onChanged: () => void }) {
  const [busy, setBusy] = React.useState<string | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const dep = (draft.dependency_state ?? {}) as Rec;
  const guide = (draft.guidance ?? {}) as Rec;
  const sd = (steps?.dependencies ?? {}) as Rec;
  const sg = (steps?.guidance ?? {}) as Rec;
  const gate = (sg.gate ?? null) as Rec | null;
  const moved = "The business knowledge changed after this request was confirmed; confirm it again first.";

  const ask = async (which: "deps" | "guidance") => {
    setBusy(which); setErr(null);
    const path = which === "deps" ? `/v2/business/changes/${q(cid)}/impact/dependencies` : `/v2/business/changes/${q(cid)}/draft/guidance`;
    const attempt = Number((which === "deps" ? sd : sg).next_attempt ?? 1);
    try { await api<Rec>("POST", path, { workspace_id: ws, attempt }); onChanged(); }
    catch (e) { setErr(refusal(e)); onChanged(); }
    finally { setBusy(null); }
  };

  const depReason = knowledgeMoved ? moved : !sd.kind_enabled ? "This AI step is not enabled on this server." : !sd.may_ask ? "Needs the change and AI permissions." : null;
  const guideReason = knowledgeMoved ? moved : !sg.kind_enabled ? "This AI step is not enabled on this server." : !sg.may_ask ? "Needs the prepare-change and AI permissions."
    : gate ? String(gate.next_action) : null;
  const files = ((draft.files ?? []) as Rec[]).filter((f) => f.model_instruction);
  const open = ((draft.unresolved ?? []) as Rec[]).filter((u) => u.model_instruction);
  const advice = (guide.advice_for_human ?? []) as Rec[];
  return (
    <Card title="AI help on this draft" testid="ai-steps">
      <HowItWorks testid="ai-steps-how">
        <p>First a model looks for rules that depend on this change and the stored relations do not show (proposals only). Then a model writes guidance over the
          server&apos;s draft: a summary, a manual step per item and questions. Neither changes a rule, a file or a decision.</p>
      </HowItWorks>
      {err && <Callout tone="danger" testid="ai-steps-error">{err}</Callout>}
      <div className="grid gap-2" data-testid="ai-deps" data-status={String(dep.status ?? "not_requested")}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">1. Dependency check</span><StatusBadge status={badge(dep.status ?? "not_requested").status} label={badge(dep.status ?? "not_requested").label} />
        </div>
        {dep.reason ? <Muted testid="ai-deps-reason">{String(dep.reason)}</Muted> : null}
        {dep.status === "completed" && <Muted testid="ai-deps-done">{dep.complete ? "It covered every rule. Its proposals are listed under “What it touches”." : "It did not cover every rule; dependencies beyond its proposals are unknown, not absent."}</Muted>}
        {sd.next_attempt != null && dep.status !== "pending" && (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant={dep.status === "not_requested" ? "primary" : "secondary"} data-testid="ai-deps-start" disabled={busy !== null || !!depReason}
              aria-describedby={depReason ? `ai-deps-why-${cid}` : undefined} onClick={() => void ask("deps")}>{busy === "deps" ? "Starting..." : dep.status === "not_requested" ? "Check dependencies with AI" : "Run the dependency check again"}</Button>
            <span id={`ai-deps-why-${cid}`} className="text-xs text-muted" data-testid="ai-deps-why">{depReason ?? "One paid model call within the budget."}</span>
          </div>
        )}
      </div>
      <div className="grid gap-2" data-testid="ai-guidance" data-status={String(guide.status ?? "not_requested")}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">2. Guidance on the draft</span><StatusBadge status={badge(guide.status ?? "not_requested").status} label={badge(guide.status ?? "not_requested").label} />
        </div>
        {guide.reason ? <Muted testid="ai-guidance-reason">{String(guide.reason)}</Muted> : null}
        {sg.next_attempt != null && guide.status !== "pending" && (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant={guide.status === "not_requested" ? "primary" : "secondary"} data-testid="ai-guidance-start" disabled={busy !== null || !!guideReason}
              aria-describedby={guideReason ? `ai-guidance-why-${cid}` : undefined} onClick={() => void ask("guidance")}>{busy === "guidance" ? "Starting..." : guide.status === "not_requested" ? "Ask AI for guidance" : "Ask for guidance again"}</Button>
            <span id={`ai-guidance-why-${cid}`} className="text-xs text-muted" data-testid="ai-guidance-why">{guideReason ?? "One paid model call within the budget."}</span>
          </div>
        )}
        {guide.status === "completed" && (
          <OriginSection origin="ai" ariaLabel="Guidance written by a model" title="Guidance (written by a model over the server's draft)" testid="ai-guidance-text">
            {guide.summary_withheld ? <Muted testid="ai-guidance-withheld">This guidance was written with evidence rights; it is shown only to readers who have them.</Muted>
              : <p data-testid="ai-guidance-summary">{String(guide.summary ?? "")}</p>}
            {[...files, ...open].length > 0 && (
              <ul className="list-disc pl-5 grid gap-1">{[...files, ...open].map((it) => <li key={String(it.ref)} data-testid="ai-guidance-item" data-ref={String(it.ref)}>{String(it.ref)}: {String(it.model_instruction)}</li>)}</ul>
            )}
            {((guide.effect_statements ?? []) as Rec[]).length > 0 && (       /* hardening H1: the model's own statement of each server-proven effect, checked by the server */
              <div className="grid gap-1" data-testid="ai-guidance-effects"><p className="font-medium">What the change does to cases, in the model&apos;s words</p>
                <ul className="list-disc pl-5">{((guide.effect_statements ?? []) as Rec[]).filter((x) => x.statement).map((x) => (
                  <li key={String(x.ref)} data-testid="ai-guidance-effect" data-status={String(x.status)}>{String(x.statement)}{x.status === "incomplete" ? " (incomplete: the server's statement above names the rest)" : ""}</li>
                ))}</ul></div>
            )}
            {(guide.effects_coverage as Rec | undefined)?.effects ? (
              <Muted testid="ai-guidance-effects-coverage">The model stated {String((guide.effects_coverage as Rec).kept)} of {String((guide.effects_coverage as Rec).effects)} proven effects fully; the server&apos;s own statements are shown above in any case.</Muted>
            ) : null}
            {((guide.removed ?? []) as Rec[]).length > 0 && (                   /* hardening H2: removed by the server, with the reason; nothing is rewritten in the model's name */
              <div className="grid gap-1" data-testid="ai-guidance-removed"><p className="font-medium">Removed by the server (contradicted a proven fact)</p>
                <ul className="list-disc pl-5">{((guide.removed ?? []) as Rec[]).map((x, i) => <li key={i} data-testid="ai-guidance-removed-item"><s>{String(x.text)}</s></li>)}</ul></div>
            )}
            {advice.length > 0 && (
              <div className="grid gap-1" data-testid="ai-guidance-advice"><p className="font-medium">Checks you may want to make (not blockers)</p>
                <ul className="list-disc pl-5">{advice.map((a, i) => <li key={i} data-testid="ai-guidance-advice-item">{String(a.text)}</li>)}</ul></div>
            )}
            <Muted>Questions that block the decision are listed under “Open questions”. {Number(guide.rejected_count) ? `${String(guide.rejected_count)} model item(s) were refused by the server.` : ""}</Muted>
          </OriginSection>
        )}
      </div>
    </Card>
  );
}
