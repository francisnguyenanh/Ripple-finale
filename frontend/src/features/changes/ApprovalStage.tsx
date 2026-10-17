import { stageNo, stageTitle } from "./registry";
import { StageCard } from "./StageCard";
import * as React from "react";
import { Check, X } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Fingerprint, Muted, StatePill, Tech, TextArea, usePlain } from "@/components/bits";
import { humanize, newId } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { ExplanationCard } from "@/features/explain/ExplanationCard";
import { fmtForm } from "./RequestCard";
import { DecisionBasis, decisionBasis } from "./DecisionBasis";
import { ReviewNotes, useReviewNotes } from "./ReviewNotes";
import { useFlow } from "./flow";
import { JobLine, useCreateStage } from "./StageBits";
import type { FlowData, Rec } from "./types";
import { isDone, isTerminal, jobState } from "./types";

function approvalStale(d: FlowData): boolean {
  if (!d.approval || jobState(d.approval) !== "COMPLETED") return false;
  const snap = (d.approval.execution_result?.snapshot ?? {}) as Rec;
  return snap.source_revision_id !== d.head.source_revision_id || snap.decision_epoch !== d.head.decision_epoch;
}

function limitations(d: FlowData): string[] {
  const out: string[] = [];
  const pr = (d.proposal?.execution_result ?? {}) as Rec;
  const rr = (d.review?.execution_result ?? {}) as Rec;
  const sr = (d.simulation?.execution_result ?? {}) as Rec;
  if (pr.edit_kind === "xlsx_typed_edit") out.push("Workbook edit: cached formula values are not recalculated. Open the exported workbook in a spreadsheet application to recalculate before relying on formula results.");
  if (sr.reason) out.push(`Simulation limit: ${sr.reason}${sr.status ? ` (mode ${sr.status})` : ""}`);
  for (const [name, chk] of Object.entries((rr.checks ?? {}) as Rec))
    if (chk.status === "UNAVAILABLE" || chk.status === "NOT_RUN") out.push(`Check "${name}" is ${chk.status}${chk.detail ? `: ${chk.detail}` : ""}`);
  return out;
}

export function ApprovalStage() {
  const { ws, ids, data, setIds, refresh } = useFlow();
  const p = usePlain();
  const s = useSession();
  const notify = useNotify();
  const { create, pending } = useCreateStage();
  const { run, pending: deciding } = useAction();
  const [reason, setReason] = React.useState("");
  const staleShown = React.useRef(false);
  const d = data;
  const notesHook = useReviewNotes(ids.proposal && d && isDone(d.proposal) ? ids.proposal : undefined);
  const stale = !!d && approvalStale(d);
  React.useEffect(() => {
    if (stale && !staleShown.current) {
      staleShown.current = true;
      notify.stale("This approval bundle is out of date (the source or decision epoch changed after it was computed). Nothing was changed. Reload to see the current state.");
    }
    if (!stale) staleShown.current = false;
  }, [stale, notify]);
  if (!d) return null;
  const ar = isDone(d.approval) ? (d.approval!.execution_result ?? {}) : null;
  const last = d.decisions.length ? d.decisions[d.decisions.length - 1] : null;
  const decState = last ? ({ approve: "APPROVED", reject: "REJECTED", request_changes: "CHANGES_REQUESTED" } as Rec)[last.action] ?? "NONE" : "NONE";
  const pr = (d.proposal?.execution_result ?? {}) as Rec;
  const basis = decisionBasis(d, notesHook.st, s.can("evidence.read"));

  const body = (() => {
    if (!ar) return null;
    const sim = d.simulation && isDone(d.simulation) ? (d.simulation.execution_result ?? {}) : null;
    const hb = sim?.harbor && sim.status === "EXECUTED" ? (sim.harbor as Rec) : null;
    const manual = ((pr.verification_plan ?? []) as Rec[]).filter((x) => x.action === "human_authoring_required");
    const blockers = (ar.blocked_reasons ?? []) as Rec[];
    const lims = limitations(d);
    const f = d.intent ? fmtForm(d.intent) : null;
    const digest = ar.approval_digest as string;
    return (
      <>
        <p className="text-sm break-all">Approval fingerprint (the exact bundle you decide on): <Fingerprint value={digest} testid="approval-digest" /></p>
        <Muted>Readiness: {humanize(String(ar.readiness)).toLowerCase()} - next step: {p(ar.required_action)}</Muted>
        {!ids.apply && manual.map((st, i) => <Callout key={i} tone="warn" testid="manual-work-item">Manual work required: {p(st.reason || "a person must author this edit")}</Callout>)}
        <DecisionBasis basis={basis} />
        <Callout testid="decision-brief">
          <strong>What you are deciding</strong>
          <span>
            {f ? `${f.title}: ${f.from} → ${f.to}. ` : ""}Approving records your decision with your name and reason; it does not change any file by itself. Only the exact edit in the diff above can be applied afterwards.{" "}
            {hb ? `In the simulation, ${hb.changed_count} of ${hb.transactions} sample cases would change their outcome. ` : "No behaviour simulation backs this change. "}
            {manual.length ? `${manual.length} related item(s) are left for people and stay unresolved.` : ""}
          </span>
        </Callout>
        <ExplanationCard ws={ws} subject="approval" subjectId={d.approval!.job_id} title="AI explanation of what you are deciding" />
        {lims.length > 0 && <h3 className="font-semibold">Limitations</h3>}
        {lims.map((l, i) => <Callout key={i} tone="warn" testid="limitation-item">{p(l)}</Callout>)}
        <h3 className="font-semibold">{blockers.length ? `Blockers (${blockers.length}) - shown before you decide` : "No blockers reported"}</h3>
        {blockers.map((b, i) => <Callout key={i} tone="danger" testid="blocker-item">{humanize(String(b.name ?? b.kind ?? "blocker"))}: {p(b.reason ?? b.detail ?? "")}</Callout>)}
        {blockers.length > 0 && <Muted>The server records an approval together with its blockers; approving does not clear them.</Muted>}
        <p className="text-sm flex flex-wrap items-center gap-2">
          Decision: <StatePill state={decState} testid="state-approval-decision" label={({ NONE: "Waiting for the approver to decide", APPROVED: "Approved", REJECTED: "Rejected", CHANGES_REQUESTED: "Changes requested" } as Rec)[decState] ?? decState} />
          {last && <span className="text-muted">by {last.actor_id}{last.reason ? ` - ${last.reason}` : ""}</span>}
        </p>
        {stale && <Callout tone="danger">This approval bundle was computed before the workspace changed. It cannot be approved as shown; prepare a new bundle.</Callout>}
        {s.can("change.approve") && decState === "NONE" && !(d.approval?.requested_by && d.approval.requested_by === s.account) && (
          <div className="grid gap-2">
            <Field label="Decision reason"><TextArea data-testid="field-decision-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
            <div className="flex flex-wrap gap-2">
              {([["approve", "Approve", "action-approve", true, Check], ["reject", "Reject", "action-reject", false, X]] as const).map(([action, label, testid, primary, Icon]) => (
                <Button key={action} variant={primary ? "primary" : "secondary"} data-testid={testid} disabled={stale || deciding || (action === "approve" && basis.missing.length > 0)}
                  title={action === "approve" && basis.missing.length > 0 ? `Not available yet: ${basis.missing.join("; ")}` : undefined}
                  onClick={async () => {
                    const text = reason.trim();
                    if (!text) { notify.status("A reason is required for this decision."); return; }
                    // The digest is exactly what is on screen; it is never re-read at click time.
                    const r = await run(() => api("POST", `/v2/jobs/${encodeURIComponent(d.approval!.job_id)}/decision`, { workspace_id: ws, action, reason: text, expected_approval_digest: digest, idempotency_key: newId("dec") }), { stale: true });
                    if (!r) return;
                    setReason("");
                    notify.status(`Your ${action} decision was recorded on this approval.`);
                    await refresh();
                  }}>
                  <Icon size={16} aria-hidden="true" />{label}
                </Button>
              ))}
            </div>
          </div>
        )}
        <Tech><Muted>Approval job {d.approval!.job_id}</Muted></Tech>
      </>
    );
  })();

  const blockerCount = ((ar?.blocked_reasons ?? []) as Rec[]).length;
  const apprDone = decState === "APPROVED" && !!d.apply;
  const apprSummary = last ? (
    <>{({ APPROVED: "Approved", REJECTED: "Rejected", CHANGES_REQUESTED: "Changes requested" } as Rec)[decState] ?? decState} by {last.actor_id}{last.reason ? `: "${last.reason}"` : ""}. {blockerCount} blocker(s) were shown before the decision.</>
  ) : ar ? <>Waiting for the approver to decide. {blockerCount} blocker(s) are shown before the decision.</> : undefined;
  return (
    <StageCard title={stageTitle("approval")} testid="flow-approval" done={apprDone} summary={apprSummary}>
      <JobLine name="approval" job={d.approval} />
      {ids.proposal && isDone(d.proposal) && <ReviewNotes proposalId={ids.proposal} proposalBy={d.proposal?.requested_by} st={notesHook.st} set={notesHook.set} />}
      {s.can("source.import") && (
        <div>
          <Button variant="primary" data-testid="action-prepare-approval" disabled={!isDone(d.review) || (!!d.approval && !isTerminal(d.approval)) || pending}
            title={!d.review ? `Run the review first (stage ${stageNo("review")}).` : !isDone(d.review) ? "The review is still running." : d.approval && !isTerminal(d.approval) ? "A bundle is already being prepared." : undefined}
            onClick={async () => {
              const id = newId("ui-approval");
              if (await create("approval", id, { proposal_job_id: ids.proposal, review_job_id: ids.review })) { setIds({ approval: id, apply: "" }); await refresh(); }
            }}>Prepare approval bundle</Button>
        </div>
      )}
      {body}
    </StageCard>
  );
}
