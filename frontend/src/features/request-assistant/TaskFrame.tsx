// Task frame of one request (RUX-02 / RUX-03, contract ripple-request-task/1): who holds the next step, what this person is asked to do, what they hand over, and what blocks
// it, read from GET /v2/requests/{family}/{id}/task-view (computed by the server under this person's rights; nothing is decided here). "My task" shows Input -> Action ->
// Handoff; "Whole request" lists every canonical step with its state, who holds each obligation at that step, who is waited on, and the steps that run in parallel.
// Blockers stay visible next to the action. Permission, assignment and obligation stay apart: a person who may act but is not asked sees why; nobody gets a call to action
// that is not theirs. The view and the opened step are in the URL (`task=map`, `step=<code>`), so a link, a reload and Back keep them; a linked step opens and takes focus.
// The assistant launcher opens the private request assistant (no model call on open).
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Callout, Muted } from "@/components/bits";
import { StatusBadge, type Status } from "@/components/StatusBadge";
import { useSession } from "@/session";
import { useParam } from "@/lib/param";
import { RequestAssistant } from "./RequestAssistant";
import { useTaskView } from "./taskView";

type Rec = Record<string, unknown>;
const STEP: Record<string, { status: Status; label: string }> = {
  done: { status: "success", label: "Done" }, current: { status: "info", label: "Now" }, waiting: { status: "waiting", label: "Waiting" }, blocked: { status: "partial", label: "Blocked" },
  failed: { status: "failed", label: "Failed" }, future: { status: "empty", label: "Later" }, not_applicable: { status: "empty", label: "Not needed" },
};
// The server's step phase (contract: stages[].phase, derived from the record): the spinning "in progress" badge only when something is actually running; a current step
// nobody has started is "Not started"; an unknown phase stays neutral (design rules section 5: workflow position and job execution are different dimensions).
const PHASE: Record<string, { status: Status; label: string }> = {
  running: { status: "running", label: "In progress" }, not_started: { status: "empty", label: "Not started" },
  awaiting_decision: { status: "waiting", label: "Waiting for a decision" }, awaiting_action: { status: "waiting", label: "Waiting for an action" },
  awaiting_other_step: { status: "waiting", label: "Waiting for another step" }, unknown: { status: "info", label: "Status not recorded" },
};

/** `shared` = the page already read the projection (Changes shares it with its journey and next-step text); otherwise the frame reads it itself. `detail` = the
 *  owning page's how-to / waiting controls for the current step, shown inside "What to do" so the page has one responsibility summary. */
export function TaskFrame({ ws, family, id, refreshKey, shared, detail }: { ws: string; family: "intent_flow" | "business_change"; id: string; refreshKey?: unknown;
  shared?: { tv: Rec | null; err: string | null }; detail?: React.ReactNode }) {
  const own = useTaskView(ws, family, shared ? null : id, refreshKey);
  const { tv, err } = shared ?? own;
  const [view, setView] = useParam("task");
  const [step, setStep] = useParam("step");
  const mode: "task" | "map" = view === "map" ? "map" : "task";
  const setMode = (m: "task" | "map") => setView(m === "map" ? "map" : "");
  const [askOpen, setAskOpen] = React.useState(false);
  const s = useSession();

  React.useEffect(() => {                                                          // a linked step takes focus once the map is shown (no focus theft on refresh)
    if (mode === "map" && step && tv) document.getElementById(`task-step-${step}`)?.focus();
  }, [mode, step, tv !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  if (err) return <Callout tone="warn" testid="task-frame-error">The task summary could not be read ({err}). The steps below are still shown.</Callout>;
  if (!tv) return null;
  const obligations = (tv.obligations ?? []) as Rec[];
  const mine = obligations.filter((o) => o.obligation_me && o.state !== "done");
  const others = obligations.filter((o) => !o.obligation_me && o.state !== "done");
  const blockers = (tv.blockers ?? []) as Rec[];
  const waiting = (tv.waiting_on ?? []) as Rec[];
  const stage = (tv.stage ?? {}) as Rec;
  const ref = ((tv.request_ref as Rec)?.canonical_ref ?? tv.request_ref) as Rec;
  const railOn = askOpen && ((tv.features as Rec | undefined)?.request_assistant ?? "on") !== "off";   // EUI-01/02: the assistant beside the task on wide screens, below it otherwise
  return (
    <section data-testid="task-frame" data-family={family} data-stage={String(stage.code)} data-mode={mode} aria-label="Your task on this request"
      className="rounded-lg border border-border bg-surface p-4 grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold mr-2">{String(tv.title ?? "This request")}</h2>
        <StatusBadge status={blockers.length ? "partial" : "info"} label={String(stage.label ?? "")} />
        <span className="text-xs text-muted">as of {new Date(String(tv.as_of)).toLocaleString()}</span>
        <div className="ml-auto flex gap-2" role="group" aria-label="View">
          <Button size="sm" variant={mode === "task" ? "primary" : "ghost"} aria-pressed={mode === "task"} data-testid="task-mode-task" onClick={() => setMode("task")}>My task</Button>
          <Button size="sm" variant={mode === "map" ? "primary" : "ghost"} aria-pressed={mode === "map"} data-testid="task-mode-map" onClick={() => setMode("map")}>Whole request</Button>
          {((tv.features as Rec | undefined)?.request_assistant ?? "on") !== "off" && (
            <Button size="sm" variant="secondary" data-testid="assistant-launch" aria-expanded={askOpen} onClick={() => setAskOpen(!askOpen)}>Ask about this request</Button>)}
        </div>
      </div>
      {blockers.length > 0 && (
        <Callout tone="warn" testid="task-blockers">
          <strong>What blocks this request</strong>
          <ul className="list-disc pl-5">{blockers.map((b, i) => <li key={i}>{String(b.text)}</li>)}</ul>
        </Callout>
      )}
      <div className={railOn ? "grid gap-3 items-start xl:grid-cols-[minmax(0,1fr)_minmax(20rem,26rem)]" : "grid gap-3"} data-testid="task-workspace" data-rail={String(railOn)}>
      <div className="grid gap-3 min-w-0">
      {mode === "task" ? (
        <div className={railOn ? "grid gap-3 lg:grid-cols-3 xl:grid-cols-2" : "grid gap-3 lg:grid-cols-3"} data-testid="task-blocks">
          <div className="grid gap-1 content-start" data-testid="task-input">
            <h3 className="font-semibold text-sm">Input</h3>
            {((tv.input_refs ?? []) as Rec[]).length ? <ul className="list-disc pl-5 text-sm">{((tv.input_refs ?? []) as Rec[]).map((r, i) => <li key={i}>{String(r.label)}</li>)}</ul>
              : <Muted>Nothing to read yet.</Muted>}
          </div>
          <div className="grid gap-1 content-start" data-testid="task-action">
            <h3 className="font-semibold text-sm">What to do</h3>
            {mine.length ? (
              <ul className="grid gap-1 text-sm">{mine.map((o, i) => (
                <li key={i} data-testid="task-my-obligation" data-code={String(o.code)} data-required={String(o.required)}>
                  {o.state === "future" ? <span>{String(o.label)} <span className="text-muted">(later, after the step before it)</span></span> : <strong>{String(o.label)}</strong>}
                  {o.required ? "" : " (optional)"}{o.state === "blocked" ? " - blocked, see above" : ""}
                </li>))}
              </ul>
            ) : <p className="text-sm" data-testid="task-not-yours">Not waiting for you{waiting.length ? `: waiting on ${waiting.map((w) => String(w.holder_label)).join(", ")}` : "."}</p>}
            {others.filter((o) => o.eligible_me && o.state === "current").map((o, i) => <Muted key={i} testid="task-eligible-not-asked">{String(o.label)}: {String(o.reason ?? "you may do this, but you are not asked to")}</Muted>)}
            {detail}
          </div>
          <div className="grid gap-1 content-start" data-testid="task-handoff">
            <h3 className="font-semibold text-sm">Result to hand over</h3>
            <ul className="list-disc pl-5 text-sm" data-testid="task-deliverables">{((tv.expected_deliverables ?? []) as Rec[]).filter((d) => !d.later).map((d, i) => <li key={i} data-code={String(d.code)}>{String(d.label)}</li>)}</ul>
            {((tv.expected_deliverables ?? []) as Rec[]).filter((d) => d.later).map((d, i) => <Muted key={i} testid="task-deliverable-later">{String(d.label)}</Muted>)}
            {!((tv.expected_deliverables ?? []) as Rec[]).some((d) => d.later) && others.filter((o) => o.state === "future").length > 0 && <Muted testid="task-then">Then: {others.filter((o) => o.state === "future").map((o) => `${String(o.label)} (${String(o.holder_label)})`).join("; ")}.</Muted>}
          </div>
        </div>
      ) : (
        <>
        <MapStepper stages={(tv.stages ?? []) as Rec[]} obligations={obligations} />
        <ol className="grid gap-1 text-sm" data-testid="task-map" aria-label="All steps of this request">
          {((tv.stages ?? []) as Rec[]).map((s0) => {
            const code = String(s0.code);
            const st = PHASE[String(s0.phase)] ?? STEP[String(s0.state)] ?? { status: "info" as Status, label: String(s0.state) };
            const here = obligations.filter((o) => o.step === code);
            const waits = waiting.filter((w) => w.step === code);
            const yours = here.some((o) => o.obligation_me && o.state !== "done");
            const group = s0.parallel_group ? ((tv.stages ?? []) as Rec[]).filter((x) => x.parallel_group === s0.parallel_group && x.code !== code).map((x) => String(x.label)) : [];
            const open = step === code;
            return (
              <li key={code} data-testid="task-map-step" data-step={code} data-state={String(s0.state)} data-phase={s0.phase ? String(s0.phase) : undefined} data-parallel={s0.parallel_group ? String(s0.parallel_group) : undefined}>
                <button type="button" id={`task-step-${code}`} aria-expanded={open} aria-controls={`task-step-detail-${code}`}
                  className="flex w-full items-center gap-2 rounded text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
                  onClick={() => setStep(open ? "" : code)}>
                  <StatusBadge status={st.status} label={st.label} /> <span>{String(s0.label)}</span>
                  {yours && <span className="text-xs font-semibold" data-testid="task-map-yours">(your step)</span>}
                  {group.length > 0 && <span className="text-xs text-muted">(in parallel with {group.join(", ")})</span>}
                </button>
                {open && (
                  <div id={`task-step-detail-${code}`} data-testid="task-map-detail" className="ml-6 mt-1 grid gap-1">
                    {here.length === 0 && waits.length === 0 && <Muted>No obligation of this step is visible to you.</Muted>}
                    {here.map((o, i) => (
                      <p key={i} data-testid="task-map-obligation" data-code={String(o.code)} data-state={String(o.state)}>
                        {String(o.label)}{o.required ? "" : " (optional)"} - held by {o.obligation_me ? "you" : String(o.holder_label)}, {(STEP[String(o.state)] ?? { label: String(o.state) }).label.toLowerCase()}
                        {o.reason ? `; ${String(o.reason)}` : ""}
                      </p>
                    ))}
                    {waits.map((w, i) => <p key={`w${i}`} data-testid="task-map-waiting">Waiting on {String(w.holder_label)}.</p>)}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
        </>
      )}
      </div>
      {railOn && <RequestAssistant key={`${s.account}|${ws}|${String(ref.family)}|${String(ref.id)}`} ws={ws} family={String(ref.family) as "intent_flow" | "business_change"} id={String(ref.id)} tv={tv} />}
      </div>
    </section>
  );
}

/** EUI-04 compact overview of the whole request: steps in order, and steps the server marks as one `parallel_group` stacked in one column (never inferred from
 *  labels). Decorative for assistive technology: the step list below is the full text equivalent with states, holders and details. */
function MapStepper({ stages, obligations }: { stages: Rec[]; obligations: Rec[] }) {
  const cols: Rec[][] = [];
  for (const st of stages) {
    const last = cols[cols.length - 1];
    if (st.parallel_group && last && last[0].parallel_group === st.parallel_group) last.push(st); else cols.push([st]);
  }
  if (!cols.length) return null;
  const tone = (state: string) => state === "current" ? "border-primary font-semibold" : state === "blocked" ? "border-warning" : state === "done" ? "border-border text-muted"
    : "border-border border-dashed text-muted";
  return (
    <ol aria-hidden="true" data-testid="task-map-visual" className="flex flex-wrap items-center gap-1 text-xs">
      {cols.map((col, i) => (
        <li key={i} className="flex items-center gap-1" data-parallel={col.length > 1 ? String(col[0].parallel_group) : undefined}>
          {i > 0 && <span className="text-muted">→</span>}
          <span className={col.length > 1 ? "grid gap-1 rounded-md border border-dashed border-border p-1" : "grid"}>
            {col.length > 1 && <span className="text-muted">in parallel</span>}
            {col.map((st) => {
              const yours = obligations.some((o) => o.step === st.code && o.obligation_me && o.state !== "done");
              return <span key={String(st.code)} data-step={String(st.code)} data-state={String(st.state)} className={`rounded-md border px-2 py-0.5 ${tone(String(st.state))}`}>
                {st.state === "done" ? "✓ " : ""}{String(st.label)}{yours ? " (yours)" : ""}</span>;
            })}
          </span>
        </li>
      ))}
    </ol>
  );
}
