import { stageTitle } from "./registry";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/bits";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { AI_KIND_WORDS } from "@/lib/aiKinds";
import { ApplyStage, ChangeResult, LoopCard, QueryStage } from "./ApplyStage";
import { AnalysisStage } from "./AnalysisStage";
import { ApprovalStage } from "./ApprovalStage";
import { DraftStage, PreviewStage } from "./DraftStage";
import { FlowProvider, useFlow } from "./flow";
import { IntentStage } from "./IntentStage";
import { ProposalStage } from "./ProposalStage";
import { Journey } from "./JourneyBar";
import { fmtForm, NextCard, RequestCard, SupersededCard } from "./RequestCard";
import { CheckStage } from "./CheckStage";
import { ReviewStage } from "./ReviewStage";
import { StageProvider, useStages } from "./StageCard";
import { WhatIfStage } from "./WhatIfStage";
import { WorkInbox } from "@/features/inbox/WorkInbox";
import { TaskFrame } from "@/features/request-assistant/TaskFrame";
import { TaskViewContext, useTaskView } from "@/features/request-assistant/taskView";

function Unavailable() {
  const { ws } = useFlow();
  return (
    <div data-testid="link-unavailable" className="rounded-lg border border-border bg-surface p-4 grid gap-2">
      <h2 className="text-lg font-semibold">This page is not available to your account</h2>
      <p className="text-sm">The link may be incomplete, may belong to another workspace, or your account has no access to it. Nothing was changed and nothing from that workspace is shown.</p>
      <div><Button asChild><a href={`#/ws/${encodeURIComponent(ws)}/changes`}>Go to Changes in this workspace</a></Button></div>
    </div>
  );
}

function ExpandAll() {
  const { showAll, setShowAll } = useStages();
  return <div><Button type="button" variant="ghost" data-testid="stages-expand-all" aria-pressed={showAll} onClick={() => setShowAll(!showAll)}>{showAll ? "Collapse finished stages" : "Expand all stages"}</Button></div>;
}

function Flow() {
  const { data, failed, loadError, refresh, ids, ws } = useFlow();
  const reqId = ids.intent || ids.chat ? String(ids.intent ?? ids.chat) : null;
  const task = useTaskView(ws, "intent_flow", reqId, data);                // one read, shared by the task frame, the journey and the next step
  if (loadError) {
    if (loadError.status === 403 || loadError.status === 404) return <Unavailable />;
    return (
      <div data-testid="state-error" className="rounded-lg border border-danger bg-surface p-4 grid gap-2">
        <p>{loadError.network ? "The server could not be reached." : `The server could not complete the request (HTTP ${loadError.status}).`}</p>
        <div><Button onClick={() => void refresh()}>Try again</Button></div>
      </div>
    );
  }
  if (!data) return <p data-testid="state-loading" role="status" className="text-muted">Loading the change flow...</p>;
  const hasIntent = !!data.intent;
  const noRequest = !Object.values(ids).some(Boolean);
  return (
    <TaskViewContext.Provider value={reqId ? task : null}>
    <div className="grid gap-4" data-testid="change-flow">
      {failed && (
        <Callout tone="warn" testid="job-refresh-failed">
          <span>The latest status could not be read. What is shown may be out of date.</span>
          <div><Button onClick={() => void refresh()}>Refresh status</Button></div>
        </Callout>
      )}
      {noRequest && (
        <div data-testid="changes-chooser" className="grid gap-2">
          <Callout testid="changes-none-selected"><strong>No change is selected.</strong><span>Pick one of your changes below (the list is built by the server for your account), or start a new request in the first stage. Nothing is opened for you that your role may not see.</span></Callout>
          <WorkInbox ws={ws} />
        </div>
      )}
      <LegacyAiNote />
      <SupersededCard />
      {reqId && <TaskFrame key={`${ws}|${reqId}`} ws={ws} family="intent_flow" id={reqId} refreshKey={data} shared={task} detail={task.tv ? <NextCard inline /> : undefined}
        fallbackTitle={data.intent ? (() => { const f = fmtForm(data.intent); return `${f.title}: ${f.from} → ${f.to}`; })() : undefined} />}
      <Journey />
      <NextCard />
      <ExpandAll />
      {(hasIntent || (data.chat && !data.chat.unavailable)) && <RequestCard />}
      <IntentStage />
      <DraftStage />
      <PreviewStage />
      <AnalysisStage />
      <ProposalStage />
      <CheckStage />
      <ReviewStage />
      <ApprovalStage />
      <ApplyStage />
      <LoopCard />
      {data.change ? <ChangeResult cs={data.change} /> : ids.apply ? (
        <section data-testid="flow-change" className="rounded-lg border border-border bg-surface p-4 grid gap-2">
          <h2 className="text-lg font-semibold">{stageTitle("accept")}</h2>
          <p data-testid="state-outcome" className="text-sm">The state of this change has not been read from the server yet. Nothing is shown as finished.</p>
        </section>
      ) : null}
      <QueryStage />
      <WhatIfStage />
    </div>
    </TaskViewContext.Provider>
  );
}

export function ChangesPage() {
  const s = useSession();
  if (!s.route.ws) return null;
  return (
    <>
      <PageHeader id="changes" />
      <FlowProvider ws={s.route.ws} key={s.route.ws}>
        <StageProvider><Flow /></StageProvider>
      </FlowProvider>
    </>
  );
}


/** The AI steps of this flow that the server does not offer (role allowlist or provider switch). The deterministic steps keep working; each button says why. */
const FLOW_AI_KINDS = ["intent.interpret", "change.draft", "gap.answer", "impact.judge", "explain", "sim.analysis"];
function LegacyAiNote() {
  const s = useSession();
  if (s.aiOff) return null;                                       // the shell already says that AI is switched off
  const off = FLOW_AI_KINDS.filter((k) => !s.aiKind(k));
  if (!off.length) return null;
  return (
    <Callout testid="flow-ai-not-enabled" role="status">
      <strong>Some AI steps of this page are not enabled on this server.</strong>
      <span>Not available here: {off.map((k) => AI_KIND_WORDS[k] ?? k).join(", ")}. The steps that need no AI still work, and nothing is sent to the AI for the others.
        The business change path on the Questions page uses its own AI steps.</span>
    </Callout>
  );
}

