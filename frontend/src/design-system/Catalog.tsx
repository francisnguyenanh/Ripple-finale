// Synthetic examples of the shared components, in both themes and both modes. No live data, no requests, no-op callbacks.
// Add an example here when a new shared variant or component gets its first real consumer (docs/design-system/COMPONENT_GUIDELINES.md).
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Callout, Field, HowItWorks, Muted, StatePill, TextArea, TextInput } from "@/components/bits";
import { StatusBadge, type Status } from "@/components/StatusBadge";
import { ResultSummary } from "@/components/decision/ResultSummary";
import { NextActionPanel } from "@/components/decision/NextActionPanel";
import { TrustStateSummary } from "@/components/decision/TrustStateSummary";
import { OriginSection, OriginTag } from "@/components/decision/Origin";
import { useSession } from "@/session";
import { TaskFrame } from "@/features/request-assistant/TaskFrame";
import { AssistantMessage } from "@/features/request-assistant/RequestAssistant";

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

// Synthetic task views (contract ripple-request-task/1) and assistant messages: illustrative only, never read from a server.
const STEPS = (states: Record<string, string>, labels: [string, string][], par: string[] = []) => labels.map(([code, label]) => ({ code, label, state: states[code] ?? "future", ...(par.includes(code) ? { parallel_group: "after_knowledge_decision" } : {}) }));
const BC_STEPS: [string, string][] = [["understand", "Request understood and confirmed"], ["draft", "Draft of the knowledge change"], ["clarify", "Open questions answered"], ["knowledge_decision", "Knowledge decision"],
  ["commit", "Knowledge committed"], ["file_decision", "File edits decided"], ["source_apply", "Source edits applied"], ["readback", "Source read back"]];
const TASK_APPROVER = {
  title: "Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY.", as_of: "2026-10-20T10:00:00Z", request_ref: { family: "business_change", id: "chg-catalog-1" },
  stage: { code: "AWAITING_DECISION", label: "Waiting for the knowledge decision" }, blockers: [], features: { request_assistant: "off" },
  input_refs: [{ label: "Rule before / after and the proposed relation updates" }],
  obligations: [{ code: "decide_knowledge", label: "Decide the knowledge change (approve, reject or request changes)", required: true, holder_label: "an approver (not the person who asked)", eligible_me: true, obligation_me: true, state: "current", step: "knowledge_decision" },
    { code: "decide_file_writes", label: "Decide the proposed file edits", required: true, holder_label: "an approver", eligible_me: true, obligation_me: false, state: "future", step: "file_decision" }],
  waiting_on: [], expected_deliverables: [{ code: "knowledge_decision", label: "A recorded knowledge decision (approve, reject or request changes) with a reason; approving does not commit anything" },
    { code: "later", later: true, label: "Later, by other steps: the commit by a knowledge owner; the decision on the proposed file edits" }],
  stages: STEPS({ understand: "done", draft: "done", clarify: "done", knowledge_decision: "current" }, BC_STEPS, ["commit", "file_decision"]),
};
const TASK_JUDGE = {
  title: "Manager approval limit: 1,000,000 JPY to 500,000 JPY", as_of: "2026-10-20T10:00:00Z", request_ref: { family: "intent_flow", id: "intent-catalog-2" },
  stage: { code: "ASK_DRAFT", label: "Confirmed request, no AI draft yet" }, blockers: [], features: { request_assistant: "off" }, input_refs: [{ label: "The confirmed request" }],
  obligations: [{ code: "ask_draft", label: "Ask for the AI draft of the change", required: true, holder_label: "the requester", eligible_me: true, obligation_me: false, state: "current", step: "draft", reason: "the requester is asked; your role may also do it" }],
  waiting_on: [{ holder: "requester", holder_label: "the requester", code: "ask_draft", step: "draft" }],
  expected_deliverables: [{ code: "draft", label: "An AI draft of the change with checked citations" }],
  stages: STEPS({ conversation: "done", intent: "done", draft: "current" }, [["conversation", "Request stated in a conversation"], ["intent", "Request confirmed"], ["draft", "AI draft"], ["proposal", "Proposal prepared"]]),
};
const SAMPLE_MESSAGES: Record<string, unknown>[] = [
  { message_id: "m1", state: "answered", question: "What should I do next?", current: true,
    answer: { status: "answered", blocks: [{ kind: "authority", text: "You are asked to do this now: Decide the knowledge change (approve, reject or request changes).", fact_refs: ["F3"] }], checks_version: "request-assistant-checks/4",
      next_actions: [{ label: "Decide the knowledge change (approve, reject or request changes)" }], limitations: [], rejected: [] },
    citations: { F3: { kind: "obligation", text: "Decide the knowledge change (approve, reject or request changes) - required, held by you, state current (you may do it: yes).", display: "Decide the knowledge change (approve, reject or request changes): you are asked to do this now." } } },
  { message_id: "m2", state: "answered", question: "Can you approve it for me?", current: false, as_of: "2026-10-19T09:00:00Z",
    answer: { status: "answered", blocks: [{ kind: "interpretation", text: "A decision is made only with the request's own controls; nothing was approved here.", fact_refs: ["F7"] }], next_actions: [], limitations: [], rejected: [{ index: 1 }] },
    citations: { F7: { kind: "action", text: "Action 'Decide the knowledge change': allowed for you and it is the step you are asked to do now.", display: "Decide the knowledge change: your step now." } } },
  { message_id: "m3", state: "withheld", question: "Who else asked about this?", reason: "Withheld: your access changed since this was asked. Ask again to get an answer under your current access." },
];

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

      <Block id="cat-task" title="Request task frame (TaskFrame): asked vs may-but-not-asked, whole request, parallel steps">
        <Muted>Synthetic task views passed as `shared` (no request). The launcher is off here because the assistant panel reads the server; its messages are shown below.</Muted>
        <TaskFrame ws="ws-catalog" family="business_change" id="chg-catalog-1" shared={{ tv: TASK_APPROVER, err: null }} />
        <TaskFrame ws="ws-catalog" family="intent_flow" id="intent-catalog-2" shared={{ tv: TASK_JUDGE, err: null }} />
      </Block>

      <Block id="cat-assistant" title="Request assistant answers (AssistantMessage)">
        <ol className="grid gap-3 text-sm rounded-lg border border-border bg-surface p-4">
          {SAMPLE_MESSAGES.map((m) => <AssistantMessage key={String(m.message_id)} m={m} technical={s.mode === "technical"} />)}
        </ol>
      </Block>

      <Block id="cat-origin" title="Where content comes from (OriginTag, OriginSection): AI vs source vs engine; origin is not validation">
        <p className="flex flex-wrap items-center gap-2 text-sm"><OriginTag origin="ai" /> <OriginTag origin="source" /> <OriginTag origin="engine" /> <OriginTag origin="source" label="Task record" /> <OriginTag origin="something-else" /></p>
        <OriginSection origin="engine" title="Engine result (deterministic, seeded; not a forecast)" testid="catalog-origin-engine">
          <Muted>Baseline 12.0 days, scenario 9.5 days, change -2.5 days (synthetic).</Muted>
        </OriginSection>
        <OriginSection origin="ai" title="AI interpretation (every number checked against the engine result)" testid="catalog-origin-ai">
          <Muted>The shorter approval path removes about 2.5 days of waiting in this sample (synthetic).</Muted>
        </OriginSection>
        <p className="text-sm">A file missing from your upload counts as <strong>deleted</strong> from the new version. (A consequence: stays visible.)</p>
        <HowItWorks testid="catalog-how"><Muted>Routine walkthrough, collapsed by default (synthetic).</Muted></HowItWorks>
        <Callout tone="info">An AI label never means correct, and a source or engine label never means accepted or approved: checks and human confirmations have their own displays.</Callout>
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
