import { stageTitle } from "./registry";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/bits";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { ApplyStage, ChangeResult, LoopCard, QueryStage } from "./ApplyStage";
import { AnalysisStage } from "./AnalysisStage";
import { ApprovalStage } from "./ApprovalStage";
import { DraftStage, PreviewStage } from "./DraftStage";
import { FlowProvider, useFlow } from "./flow";
import { IntentStage } from "./IntentStage";
import { ProposalStage } from "./ProposalStage";
import { Journey } from "./JourneyBar";
import { NextCard, RequestCard, SupersededCard } from "./RequestCard";
import { CheckStage } from "./CheckStage";
import { ReviewStage } from "./ReviewStage";
import { StageProvider, useStages } from "./StageCard";
import { WhatIfStage } from "./WhatIfStage";
import { WorkInbox } from "@/features/inbox/WorkInbox";

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
      <SupersededCard />
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
