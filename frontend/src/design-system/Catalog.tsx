// Synthetic examples of the shared components, in both themes and both modes. No live data, no requests, no-op callbacks.
// Add an example here when a new shared variant or component gets its first real consumer (docs/design-system/COMPONENT_GUIDELINES.md).
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, StatePill, TextArea, TextInput } from "@/components/bits";
import { StatusBadge, type Status } from "@/components/StatusBadge";
import { ResultSummary } from "@/components/decision/ResultSummary";
import { NextActionPanel } from "@/components/decision/NextActionPanel";
import { TrustStateSummary } from "@/components/decision/TrustStateSummary";
import { useSession } from "@/session";

const STATUSES: Status[] = ["loading", "empty", "queued", "running", "waiting", "stale", "partial", "failed", "success", "info", "simulated"];
const STATES = ["COMPLETED", "CONFIRMED", "PENDING_APPROVAL", "PARTIAL", "RUNNING", "FAILED", "STALE", "NOT_STARTED", "SOME_NEW_STATE"];
// Static class names so Tailwind sees them; no inline style (the production CSP forbids it, and the catalog follows the same rules).
const TOKENS: [string, string][] = [
  ["bg", "bg-bg"], ["surface", "bg-surface"], ["surface-2", "bg-surface-2"], ["border", "bg-border"], ["text", "bg-text"], ["text-muted", "bg-muted"],
  ["primary", "bg-primary"], ["on-primary", "bg-on-primary"], ["success", "bg-success"], ["warning", "bg-warning"], ["danger", "bg-danger"],
  ["on-danger", "bg-on-danger"], ["info", "bg-info"], ["sim", "bg-sim"], ["focus", "bg-[var(--focus)]"],
];
const noop = () => undefined;

function Block({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return <section aria-labelledby={id} data-testid={`catalog-${id}`} className="grid gap-3"><h2 id={id} className="text-lg font-semibold">{title}</h2>{children}</section>;
}

export function Catalog() {
  const s = useSession();
  const [theme, setTheme] = React.useState<"light" | "dark">("light");
  React.useEffect(() => { document.documentElement.setAttribute("data-theme", theme); }, [theme]);
  return (
    <main className="mx-auto max-w-[1200px] p-4 md:p-8 grid gap-8" data-testid="catalog">
      <header className="grid gap-2">
        <h1 className="text-2xl font-semibold">Component catalog</h1>
        <Muted>Development only. Synthetic data; every action is a no-op.</Muted>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Catalog view options">
          <Button size="sm" variant={theme === "light" ? "primary" : "secondary"} aria-pressed={theme === "light"} onClick={() => setTheme("light")}>Light</Button>
          <Button size="sm" variant={theme === "dark" ? "primary" : "secondary"} aria-pressed={theme === "dark"} onClick={() => setTheme("dark")}>Dark</Button>
          <Button size="sm" variant={s.mode === "business" ? "primary" : "secondary"} aria-pressed={s.mode === "business"} onClick={() => s.setMode("business")}>Business</Button>
          <Button size="sm" variant={s.mode === "technical" ? "primary" : "secondary"} aria-pressed={s.mode === "technical"} onClick={() => s.setMode("technical")}>Technical</Button>
        </div>
      </header>

      <Block id="cat-tokens" title="Colour tokens">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-5">
          {TOKENS.map(([t, cls]) => (
            <div key={t} className="rounded-md border border-border p-2 text-sm grid gap-1">
              <span className={`h-8 rounded-sm border border-border ${cls}`} aria-hidden="true" />
              <code>--{t}</code>
            </div>
          ))}
        </div>
      </Block>

      <Block id="cat-buttons" title="Buttons">
        <div className="flex flex-wrap items-center gap-2">
          {(["primary", "secondary", "ghost", "danger"] as const).map((v) => <Button key={v} variant={v} onClick={noop}>{v}</Button>)}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {(["sm", "md", "lg"] as const).map((z) => <Button key={z} size={z} variant="primary" onClick={noop}>size {z}</Button>)}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" disabled aria-describedby="cat-why">Approve</Button>
          <span id="cat-why" className="text-sm text-muted" data-testid="catalog-disabled-reason">Unavailable: the approver must be a different person than the requester.</span>
        </div>
      </Block>

      <Block id="cat-states" title="Domain states (StatePill)">
        <div className="flex flex-wrap items-center gap-2">
          {STATES.map((st) => <StatePill key={st} state={st} label={st.replace(/_/g, " ").toLowerCase()} />)}
        </div>
        <StatePill state="NOT_STARTED" label="Not started. The AI draft is done: compute the relation coverage first, then run the impact check on the confirmed change" detail="(a long instruction wraps; it is not clipped into a pill)" />
      </Block>

      <Block id="cat-status" title="Generic status (StatusBadge)">
        <div className="flex flex-wrap items-center gap-2">{STATUSES.map((st) => <StatusBadge key={st} status={st} />)}</div>
      </Block>

      <Block id="cat-callout" title="Callouts">
        <Callout tone="info">Information</Callout>
        <Callout tone="warn">Warning that must be read</Callout>
        <Callout tone="danger" role="alert">Failure with a consequence</Callout>
        <Callout tone="wait">Waiting for another person</Callout>
      </Block>

      <Block id="cat-forms" title="Form controls">
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Reason" hint="Recorded with your name."><TextInput placeholder="Why, in your own words" /></Field>
          <Field label="Details"><TextArea rows={2} /></Field>
        </div>
      </Block>

      <Block id="cat-result" title="ResultSummary and NextActionPanel">
        <ResultSummary title="Current state" scopeLabel="Figures read from this workspace" availability="unavailable"
          warnings={[{ key: "x", tone: "warn", blocking: true, text: "Some figures could not be read (questions). They are shown as Unavailable, not as zero." }]}>
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-lg border border-border bg-surface p-4"><span className="text-2xl font-semibold tnum">12</span><p className="text-sm text-muted">files</p></div>
            <div className="rounded-lg border border-border bg-surface p-4"><span className="text-lg font-semibold text-muted">Unavailable</span><p className="text-sm text-muted">questions</p></div>
          </div>
        </ResultSummary>
        <NextActionPanel reason="Review the open question, then confirm or answer it." actorLabel="You" action={<Button variant="primary" onClick={noop}>Open</Button>} testid="catalog-next" />
        <NextActionPanel reason="A second approver is required; you requested this change." blocked testid="catalog-next-blocked" />
      </Block>

      <Block id="cat-axes" title="Completion axes (TrustStateSummary)">
        <TrustStateSummary axes={[
          { key: "knowledge", label: "Knowledge", raw: "committed" }, { key: "source", label: "Source files", raw: "pending" },
          { key: "code", label: "Code", raw: "applied_pending_readback" }, { key: "behaviour", label: "Behaviour", raw: undefined },
        ]} limitations={["Each axis is separate. A committed knowledge change does not mean files or code changed."]} testid="catalog-axes" />
      </Block>
    </main>
  );
}
