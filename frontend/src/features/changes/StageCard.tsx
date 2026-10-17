// A stage of the change flow. Only the stage that needs a person (and anything the person opens) is expanded; a finished stage is
// folded to ONE summary line that still carries what must never be hidden: the values before/after, who decided and why, what is not
// covered or unresolved, and PARTIAL states. Folded content stays in the page (hidden), so typed text and state are not lost.
import * as React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";

const Ctx = React.createContext<{ showAll: boolean; setShowAll: (v: boolean) => void }>({ showAll: false, setShowAll: () => undefined });
export const useStages = () => React.useContext(Ctx);

export function StageProvider({ children }: { children: React.ReactNode }) {
  const [showAll, setShowAll] = React.useState(false);
  const v = React.useMemo(() => ({ showAll, setShowAll }), [showAll]);
  return <Ctx.Provider value={v}>{children}</Ctx.Provider>;
}

export function StageCard({ title, testid, done, summary, children, className }: {
  title: React.ReactNode; testid: string; done: boolean; summary?: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  const { showAll } = useStages();
  const [user, setUser] = React.useState<boolean | null>(null);
  const open = showAll || (user ?? !done);
  const id = React.useId();
  return (
    <section data-testid={testid} data-open={open ? "true" : "false"} data-done={done ? "true" : "false"} className={cn("rounded-lg border border-border bg-surface p-4 md:p-5 grid gap-3 min-w-0", className)}>
      <h2 className="text-lg font-semibold">
        <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setUser(!open)} data-testid={`${testid}-toggle`}
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
