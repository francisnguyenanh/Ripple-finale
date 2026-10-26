// Where this change is, for everyone who opens it: every step in order with its number, its state in words (never colour alone), who has to act,
// what they do next and why anything waits or is blocked. Read from server data only, so it is the same after a refresh, Back, or another sign-in.
import { CheckCircle2, CircleAlert, CircleDot, Clock, Circle, PlayCircle, RotateCcw } from "lucide-react";
import { useSession } from "@/session";
import * as React from "react";
import { useFlow } from "./flow";
import { TaskViewContext, responsibility } from "@/features/request-assistant/taskView";
import { useStages } from "./StageCard";
import { journey, type JState } from "./journey";

const ICON: Record<JState, typeof Circle> = { done: CheckCircle2, current: PlayCircle, waiting: Clock, blocked: CircleAlert, failed: CircleAlert, redo: RotateCcw, partial: CircleDot, todo: Circle };
const WORD: Record<JState, string> = { done: "Done", current: "Action needed", waiting: "Waiting", blocked: "Blocked", failed: "Failed", redo: "To redo", partial: "Done, partly", todo: "Not yet" };
const TONE: Record<JState, string> = { done: "text-success", current: "text-primary", waiting: "text-info", blocked: "text-danger", failed: "text-danger", redo: "text-warning", partial: "text-warning", todo: "text-muted" };

// D1 (compact stepper): a step that needs nobody's attention is a numbered chip (icon + number; its title and state are the accessible name and the
// tooltip); the current step and every blocked, failed, to-redo or partly done step stay open with who acts and why. "Show all steps" opens every row.
const ATTENTION: JState[] = ["current", "waiting", "blocked", "failed", "redo", "partial"];

export function Journey() {
  const { data } = useFlow();
  const s = useSession();
  const { request } = useStages();
  const shared = React.useContext(TaskViewContext);
  const [missing, setMissing] = React.useState<string | null>(null);
  const [all, setAll] = React.useState(false);
  if (!data || (!data.intent && !(data.chat && !data.chat.unavailable))) return null;
  const steps = journey(data, { account: s.account, can: s.can, roles: s.roles }, responsibility(shared?.tv));
  const open = steps.find((x) => x.state !== "done" && x.state !== "partial" && x.state !== "todo") ?? null;
  const counts = (Object.keys(WORD) as JState[]).map((k) => [k, steps.filter((x) => x.state === k).length] as const).filter(([, n]) => n > 0);
  const go = (x: (typeof steps)[number]) => (e: React.MouseEvent) => {
    e.preventDefault();                                                 // the route hash stays as it is
    const el = document.querySelector(`[data-testid="${x.target}"]`);
    if (!el) { setMissing(x.title); return; }                           // never scroll to an unrelated place
    setMissing(null);
    if (el.hasAttribute("data-open")) request(x.target); else el.scrollIntoView({ block: "start" });
  };
  return (
    <section data-testid="journey" data-compact={String(!all)} aria-label="Where this change is" className="rounded-lg border border-border bg-surface p-3 md:p-4 grid gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">Where this change is</h2>
        <p className="text-xs text-muted" data-testid="journey-counts">{counts.map(([k, n]) => `${WORD[k]}: ${n}`).join(" · ")}</p>
        <button type="button" className="ml-auto text-sm text-primary underline" aria-expanded={all} data-testid="journey-all-toggle" onClick={() => setAll(!all)}>
          {all ? "Show only what needs attention" : `Show all ${steps.length} steps`}
        </button>
      </div>
      <ol className="flex flex-wrap gap-1.5">
        {steps.map((x) => {
          const Icon = ICON[x.state];
          const active = open?.key === x.key;
          const state = x.mine && x.state === "current" ? "Your action" : WORD[x.state];
          const row = all || active || ATTENTION.includes(x.state);
          if (!row)
            return (
              <li key={x.key} data-step={x.key} data-step-state={x.state} data-mine={String(x.mine)} data-shape="chip">
                <a href={`#${x.target}`} onClick={go(x)} title={`${x.n}. ${x.title}: ${state}`} aria-label={`${x.n}. ${x.title}: ${state}`}
                  className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs hover:bg-surface-2">
                  <Icon size={14} aria-hidden="true" className={TONE[x.state]} /><span className={x.state === "todo" ? "text-muted" : ""}>{x.n}</span>
                  <span className="sr-only" data-testid="journey-state">{state}</span>
                </a>
              </li>
            );
          return (
            <li key={x.key} data-step={x.key} data-step-state={x.state} data-mine={String(x.mine)} data-shape="row" aria-current={active ? "step" : undefined}
              className={`basis-full rounded-md px-2 py-1.5 ${active ? "border border-primary bg-surface-2" : ""}`}>
              <a href={`#${x.target}`} className="grid grid-cols-[auto_1fr] gap-x-2 text-sm rounded-sm" onClick={go(x)}>
                <Icon size={16} aria-hidden="true" className={`mt-0.5 ${TONE[x.state]}`} />
                <span>
                  <span className={x.state === "todo" ? "text-muted" : "font-medium"}>{x.n}. {x.title}</span>
                  <span className={`ml-2 text-xs ${TONE[x.state]}`} data-testid="journey-state">{state}</span>
                </span>
              </a>
              {(active || x.state === "redo" || x.state === "partial" || x.state === "failed" || x.state === "blocked") && (
                // SCN-02: the reader's own current step is explained once, in the task frame's "What to do"; the journey points there instead of repeating it
                shared?.tv && x.mine && x.state === "current"
                  ? <p className="text-sm pl-6 text-muted" data-testid="journey-detail" data-points-to="task-action">Your step: see “What to do” in the task above.</p>
                  : <p className="text-sm pl-6 text-muted" data-testid="journey-detail"><strong className="text-text">{x.who === "Nobody" ? "" : `${x.who}: `}</strong>{x.text}</p>
              )}
              {active && x.also && <p className="text-xs pl-6 text-muted" data-testid="journey-also">{x.also}</p>}
            </li>
          );
        })}
      </ol>
      {missing && <p role="status" data-testid="journey-missing" className="text-sm text-muted">"{missing}" has nothing to show yet: it appears once the step before it is done.</p>}
    </section>
  );
}
