// Where this change is, for everyone who opens it: every step in order with its number, its state in words (never colour alone), who has to act,
// what they do next and why anything waits or is blocked. Read from server data only, so it is the same after a refresh, Back, or another sign-in.
import { CheckCircle2, CircleAlert, CircleDot, Clock, Circle, PlayCircle, RotateCcw } from "lucide-react";
import { useSession } from "@/session";
import { useFlow } from "./flow";
import { journey, type JState } from "./journey";

const ICON: Record<JState, typeof Circle> = { done: CheckCircle2, current: PlayCircle, waiting: Clock, blocked: CircleAlert, failed: CircleAlert, redo: RotateCcw, partial: CircleDot, todo: Circle };
const WORD: Record<JState, string> = { done: "Done", current: "Action needed", waiting: "Waiting", blocked: "Blocked", failed: "Failed", redo: "To redo", partial: "Done, partly", todo: "Not yet" };
const TONE: Record<JState, string> = { done: "text-success", current: "text-primary", waiting: "text-info", blocked: "text-danger", failed: "text-danger", redo: "text-warning", partial: "text-warning", todo: "text-muted" };

export function Journey() {
  const { data } = useFlow();
  const s = useSession();
  if (!data || (!data.intent && !(data.chat && !data.chat.unavailable))) return null;
  const steps = journey(data, { account: s.account, can: s.can, roles: s.roles });
  const open = steps.find((x) => x.state !== "done" && x.state !== "partial" && x.state !== "todo") ?? null;
  return (
    <section data-testid="journey" aria-label="Where this change is" className="rounded-lg border border-border bg-surface p-3 md:p-4 grid gap-2">
      <h2 className="text-base font-semibold">Where this change is</h2>
      <ol className="grid gap-1">
        {steps.map((x) => {
          const Icon = ICON[x.state];
          const active = open?.key === x.key;
          return (
            <li key={x.key} data-step={x.key} data-step-state={x.state} data-mine={String(x.mine)} aria-current={active ? "step" : undefined}
              className={`rounded-md px-2 py-1.5 ${active ? "border border-primary bg-surface-2" : ""}`}>
              <a href={`#${x.target}`} className="grid grid-cols-[auto_1fr] gap-x-2 text-sm rounded-sm"
                onClick={(e) => { e.preventDefault(); document.querySelector(`[data-testid=${x.target}]`)?.scrollIntoView({ block: "start" }); }}>
                <Icon size={16} aria-hidden="true" className={`mt-0.5 ${TONE[x.state]}`} />
                <span>
                  <span className={x.state === "todo" ? "text-muted" : "font-medium"}>{x.n}. {x.title}</span>
                  <span className={`ml-2 text-xs ${TONE[x.state]}`} data-testid="journey-state">{x.mine && x.state === "current" ? "Your action" : WORD[x.state]}</span>
                </span>
              </a>
              {(active || x.state === "redo" || x.state === "partial" || x.state === "failed" || x.state === "blocked") && (
                <p className="text-sm pl-6 text-muted" data-testid="journey-detail"><strong className="text-text">{x.who === "Nobody" ? "" : `${x.who}: `}</strong>{x.text}</p>
              )}
              {active && x.also && <p className="text-xs pl-6 text-muted" data-testid="journey-also">{x.also}</p>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
