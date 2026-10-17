import { stageNo, stageTitle } from "./registry";
import { StageCard } from "./StageCard";
import { Button } from "@/components/ui/button";
import { Name, StatePill, usePlain } from "@/components/bits";
import { humanize, newId } from "@/lib/fmt";
import { useSession } from "@/session";
import { useFlow } from "./flow";
import { JobLine, useCreateStage } from "./StageBits";
import type { Rec } from "./types";
import { isDone, isTerminal } from "./types";

export function ReviewStage() {
  const { ids, data, setIds, refresh } = useFlow();
  const p = usePlain();
  const s = useSession();
  const { create, pending } = useCreateStage();
  const d = data;
  if (!d) return null;
  const rr = isDone(d.review) ? (d.review!.execution_result ?? {}) : null;
  const named = d.contracts ?? [];
  const checks = Object.values(((rr?.checks ?? {}) as Rec)) as Rec[];
  const unavailable = Object.entries(((rr?.checks ?? {}) as Rec)).filter(([, c]) => (c as Rec).status === "UNAVAILABLE" || (c as Rec).status === "NOT_RUN").map(([k]) => humanize(k).toLowerCase());
  const revDone = !!rr && !!d.approval;
  const revSummary = rr ? (
    <>{checks.filter((c) => c.status === "PASS").length} of {checks.length} checks passed{unavailable.length ? `; not available or not run: ${unavailable.join(", ")}` : ""}. The review roles are checked separately.</>
  ) : undefined;
  return (
    <StageCard title={stageTitle("review")} testid="flow-review" done={revDone} summary={revSummary}>
      <JobLine name="review" job={d.review} />
      {s.can("source.import") && (
        <div>
          <Button variant="primary" data-testid="action-run-review" title={!isDone(d.proposal) ? `Prepare the proposal first (stage ${stageNo("proposal")}).` : named.length === 0 ? "Waiting for an approver to state the check after the change (the card above). A review run before it has nothing to test and is blocked." : d.review && !isTerminal(d.review) ? "A review is already running." : undefined} disabled={!isDone(d.proposal) || named.length === 0 || (!!d.review && !isTerminal(d.review)) || pending}
            onClick={async () => {
              const id = newId("ui-review");
              const payload: Rec = { proposal_job_id: ids.proposal };
              if (named.length === 1) payload.verification_contract_id = named[0].contract_id;
              if (await create("review", id, payload)) { setIds({ review: id, approval: "", apply: "" }); await refresh(); }
            }}>Run review</Button>
        </div>
      )}
      {rr && (
        <>
          <h3 className="font-semibold">Review roles (checked separately)</h3>
          <div className="flex flex-wrap gap-2">
            {Object.entries((rr.roles ?? {}) as Rec).map(([name, role]) => (
              <span key={name} data-testid="review-role" data-role={name} data-state={role.status} className="rounded-md border border-border px-2 py-1 text-sm"><Name raw={name} />: <StatePill state={role.status} label={humanize(role.status)} /></span>
            ))}
          </div>
          <h3 className="font-semibold">Tool checks</h3>
          <ul className="grid gap-1 text-sm">
            {Object.entries((rr.checks ?? {}) as Rec).map(([name, chk]) => (
              <li key={name} data-testid="tool-check" data-check={name} data-state={chk.status}><Name raw={name} />: <StatePill state={chk.status} label={humanize(chk.status)} />{chk.detail ? <span className="text-muted"> - {p(chk.detail)}</span> : null}</li>
            ))}
          </ul>
          <p data-testid="review-disclosure" className="text-sm">{p(rr.model_correlation_disclosure || "No disclosure about model independence was reported.")}</p>
        </>
      )}
    </StageCard>
  );
}
