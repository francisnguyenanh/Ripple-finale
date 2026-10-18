// A stage of the change flow. Only the stage that needs a person (and anything the person opens) is expanded; a finished stage is
// folded to ONE summary line that still carries what must never be hidden: the values before/after, who decided and why, what is not
// covered or unresolved, and PARTIAL states. Folded content stays in the page (hidden), so typed text and state are not lost.
import * as React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";

interface StageCtx { showAll: boolean; setShowAll: (v: boolean) => void; requested: Record<string, number>; request: (testid: string) => void }
const Ctx = React.createContext<StageCtx>({ showAll: false, setShowAll: () => undefined, requested: {}, request: () => undefined });
export const useStages = () => React.useContext(Ctx);

export function StageProvider({ children }: { children: React.ReactNode }) {
  const [showAll, setShowAll] = React.useState(false);
  // "Open this stage and move focus to it" requests from the journey navigation: one nonce per stage test id.
  const [requested, setRequested] = React.useState<Record<string, number>>({});
  const request = React.useCallback((testid: string) => setRequested((r) => ({ ...r, [testid]: (r[testid] ?? 0) + 1 })), []);
  const v = React.useMemo(() => ({ showAll, setShowAll, requested, request }), [showAll, requested, request]);
  return <Ctx.Provider value={v}>{children}</Ctx.Provider>;
}

export function StageCard({ title, testid, done, summary, children, className }: {
  title: React.ReactNode; testid: string; done: boolean; summary?: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  const { showAll, requested } = useStages();
  const [user, setUser] = React.useState<boolean | null>(null);
  const open = showAll || (user ?? !done);
  const id = React.useId();
  const root = React.useRef<HTMLElement>(null);
  const toggle = React.useRef<HTMLButtonElement>(null);
  const nonce = requested[testid] ?? 0;
  // A journey link opens a folded stage BEFORE moving focus to it, so focus never lands inside hidden content.
  React.useEffect(() => {
    if (!nonce) return;
    setUser(true);
    const f = window.requestAnimationFrame(() => { root.current?.scrollIntoView({ block: "start" }); toggle.current?.focus(); });
    return () => window.cancelAnimationFrame(f);
  }, [nonce]);
  return (
    <section ref={root} data-testid={testid} data-open={open ? "true" : "false"} data-done={done ? "true" : "false"} className={cn("rounded-lg border border-border bg-surface p-4 md:p-5 grid gap-3 min-w-0", className)}>
      <h2 className="text-lg font-semibold">
        <button ref={toggle} type="button" aria-expanded={open} aria-controls={id} onClick={() => setUser(!open)} data-testid={`${testid}-toggle`}
          className="flex w-full items-center gap-2 text-left cursor-pointer rounded-sm">
          {open ? <ChevronDown size={18} aria-hidden="true" /> : <ChevronRight size={18} aria-hidden="true" />}
          <span className="flex-1 min-w-0">{title}</span>
          {done && <span className="text-xs font-normal rounded-full border border-border px-2 py-0.5 text-success">done</span>}
        </button>
      </h2>
      {!open && summary && <div data-testid={`${testid}-summary`} className="text-sm pl-6">{summary}</div>}
      <div id={id} hidden={!open} className="grid gap-3 min-w-0">{children}</div>
    </section>
  );
}
