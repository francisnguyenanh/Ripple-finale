import { stageNo, stageTitle } from "./registry";
import * as React from "react";
import { Download } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Fingerprint, Muted, StatePill, Tech, TechField, TextArea, TextInput, usePlain } from "@/components/bits";
import { basename, humanize, newId } from "@/lib/fmt";
import { downloadArtifact } from "@/lib/download";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { ExplanationCard } from "@/features/explain/ExplanationCard";
import { CompletionFindings, gateWords, dispositionGroups, dispositionsReady, dispositionPayload, type Disp } from "./CompletionFindings";
import { useFlow } from "./flow";
import { ApproveJobButton, JobLine, useCreateStage } from "./StageBits";
import type { ChangeState, Rec } from "./types";
import { isDone, jobState } from "./types";
import { EquivalentStep } from "@/components/EquivalentStep";

export function ApplyStage() {
  const { ids, data, setIds, refresh } = useFlow();
  const s = useSession();
  const { create, pending } = useCreateStage();
  const whyId = React.useId();
  const d = data;
  if (!d || ids.apply) return d ? <ApplyCard><Muted>The approved change has been sent to apply. Its result is shown below.</Muted></ApplyCard> : null;
  const approved = d.decisions.length > 0 && d.decisions[d.decisions.length - 1].action === "approve";
  const ar = (d.approval?.execution_result ?? {}) as Rec;
  const lastDec = d.decisions.length ? d.decisions[d.decisions.length - 1] : null;
  // Why Apply is not available yet, in the order a person can act on it (null = available).
  const why: string | null = !d.approval ? `Prepare the approval bundle first (stage ${stageNo("approval")}).`
    : !isDone(d.approval) ? "The approval bundle is still being prepared."
    : !lastDec ? "Waiting for the approver to decide. Nothing can be applied before a recorded approval."
    : lastDec.action !== "approve" ? `The change was ${lastDec.action === "reject" ? "rejected" : "sent back for changes"} by ${lastDec.actor_id}. Prepare a new proposal.`
    : null;
  return (
    <ApplyCard>
      {s.can("change.apply") && (
        <div>
          <Button variant="primary" data-testid="action-apply" title={why ?? undefined} aria-describedby={whyId} disabled={!(approved && isDone(d.approval)) || pending}
            onClick={async () => {
              const id = newId("ui-apply");
              if (await create("apply", id, { approval_job_id: d.approval!.job_id, expected_approval_digest: ar.approval_digest })) { setIds({ apply: id }); await refresh(); }
            }}>Apply approved change</Button>
        </div>
      )}
      <Muted id={whyId} testid="apply-why">{approved ? "The approval is recorded. Applying publishes the change to the source." : (why ?? "Apply becomes available after a human approval is recorded.")}{!s.can("change.apply") ? " You do not hold the apply permission; the requester applies it." : ""}</Muted>
    </ApplyCard>
  );
}

function ApplyCard({ children }: { children: React.ReactNode }) {
  return <Card title={stageTitle("apply")} testid="flow-apply">{children}</Card>;
}

const OUTCOME = (dto: Rec): string => {
  const by = dto.completion?.decision ? ` by ${dto.completion.decision.actor_id}` : "";
  if (dto.state === "REANALYSING" && dto.reanalysis?.terminal) return "The AI re-analysis failed and has no provider attempts left, so it will not run again and this change cannot be completed. The change was applied and the published source is unchanged; the machine verification result is kept. Choose Request changes or Reject result.";
  return ({
    STAGED: "Queued: nothing is published yet.",
    APPROVED: "Nothing was published: this change carries manual work only, or it has not been applied.",
    SOURCE_PUBLISHED: "Published to the source. Verification and reanalysis are still pending.",
    REANALYSING: "Published. The AI reanalysis is still running; this is not a finished result.",
    MACHINE_VERIFIED: "Machine checks passed. The knowledge candidate still needs its owner and a human result decision.",
    AWAITING_ACCEPTANCE: "Waiting for the human result decision.",
    COMPLETED: dto.completion?.decision?.acceptance_scope === "NARROW" ? `Accepted${by} in a NARROW scope: the change itself only; some items were left unhandled, so the request is NOT completely done.` : `Completed${by} (a human result decision is on record).`,
    FAILED: "Failed: this change needs recovery.",
    RECOVERY_REQUIRED: "Rejected by a human: recovery is required.",
    STALE: "Stale: the source moved after this change. Do not rely on it.",
  } as Rec)[dto.state] ?? `State: ${dto.state}`;
};

export function ChangeResult({ cs }: { cs: ChangeState }) {
  const { ws, refresh } = useFlow();
  const p = usePlain();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const dto = cs.dto;
  const decided = !!dto.completion?.decision;
  const terminal = !!dto.reanalysis?.terminal;
  const words = (r: string) => (terminal && r === "reanalysis_incomplete" ? "the AI re-analysis of the changed files failed and has no provider attempts left" : gateWords(r));
  const canDecide = s.can(dto.subject === "RESTORE" ? "source.restore" : "change.accept_result") && !decided;
  const head = s.head;
  const [reason, setReason] = React.useState("");
  const [disp, setDisp] = React.useState<Disp>({});
  const groups = dispositionGroups((dto.findings ?? {}) as Rec);
  const ready = dispositionsReady(groups, disp);
  const [proofs, setProofs] = React.useState<Record<string, string>>({});
  const [refusal, setRefusal] = React.useState<string[]>([]);
  const lin = (dto.lineage ?? {}) as Rec;
  const reopened = ((dto.carry_forward?.reopened ?? []) as Rec[]);
  const findingIds = new Set<string>();
  for (const g of (dto.alignment?.residual ?? []) as Rec[]) if (g.kind === "critical_gap") findingIds.add(g.finding_id);
  for (const g of (dto.result_view?.conflicts?.residual ?? []) as (string | Rec)[]) findingIds.add(typeof g === "string" ? g : g.finding_id);
  for (const f of refusal) findingIds.add(f);
  const arts = ((cs.revision?.artifacts ?? []) as Rec[]).filter((a) => !((dto.result_view?.scope ?? []) as string[]).length || (dto.result_view.scope as string[]).includes(a.artifact_id));
  const digest = dto.verification?.report_digest as string | undefined;

  const decide = async (action: "accept" | "request_changes" | "reject") => {
    const r = await run(() => api("POST", `/v2/changes/${encodeURIComponent(dto.apply_job_id)}/result-decision`, {
      workspace_id: ws, action,
      reason: reason.trim() || ({ accept: "Accepted on the change page.", request_changes: "Changes requested on the change page.", reject: "Rejected on the change page." })[action],
      expected_report_digest: digest, idempotency_key: newId("res"),
      ...(action === "accept" && groups.length ? { item_dispositions: dispositionPayload(groups, disp), expected_decision_basis: dto.findings?.decision_basis } : {}),
    }));
    if (r) { setRefusal([]); notify.status(action === "accept" ? "Result decision recorded." : "Your decision was recorded."); await refresh(); }
  };

  return (
    <Card title={`${stageTitle("accept")}: what was applied and verified`} testid="flow-change">
      <p className="text-sm flex flex-wrap items-center gap-2">Apply job: <StatePill state={jobState(cs.applyJob)} label={humanize(jobState(cs.applyJob))} testid="state-apply" data={{ "job-id": dto.apply_job_id }} /><TechField label="apply job" value={dto.apply_job_id} /></p>
      <p className="text-sm">Change state: <StatePill state={dto.state} label={humanize(dto.state)} testid="state-change" /></p>
      <p className="text-sm">Machine verification: <StatePill state={dto.verification?.status ?? "NOT_STARTED"} label={humanize(dto.verification?.status ?? "NOT_STARTED")} testid="state-verification" /></p>
      <p className="text-sm">AI reanalysis: <StatePill state={dto.reanalysis.status} label={humanize(dto.reanalysis.status)} testid="state-reanalysis" />{dto.reanalysis.reason ? <span className="text-muted"> - {p(dto.reanalysis.reason)}</span> : null}</p>
      {terminal && (
        <Callout tone="danger" testid="reanalysis-terminal" role="alert">
          <strong>The AI re-analysis failed and cannot be retried.</strong>
          <span>It used all {dto.reanalysis.attempt_limit ?? "the approved"} provider attempts, so it will not run again and nothing more is charged. The change was applied: the published source is unchanged and the machine verification result is kept. This change cannot be completed or accepted. {canDecide ? "Next: choose Request changes or Reject result below." : "Next: a person who may decide the result has to choose Request changes or Reject result."}</span>
        </Callout>
      )}
      {cs.reJobs.map((job) => job && (
        <div key={job.job_id} data-testid="reanalysis-job" data-job-id={job.job_id} data-state={jobState(job)} className="text-sm flex flex-wrap items-center gap-2">
          <StatePill state={jobState(job)} label={humanize(jobState(job))} /> <span>{humanize(job.kind)}</span>
          <Tech><code>{job.job_id}</code></Tech>
          {jobState(job) === "FAILED" && job.execution_reason && <span className="text-muted">- {p(job.execution_reason)}</span>}
          {jobState(job) === "FAILED" && ((dto.reanalysis?.exhausted_job_ids ?? []) as string[]).includes(job.job_id) && <span data-testid="reanalysis-exhausted" className="text-muted">- no provider attempts left ({job.retry?.attempts_used ?? dto.reanalysis.attempt_limit} of {job.retry?.attempt_limit ?? dto.reanalysis.attempt_limit} used); it will not run again</span>}
          {jobState(job) === "FAILED" && s.can("source.import") && job.retry?.allowed !== false && !((dto.reanalysis?.exhausted_job_ids ?? []) as string[]).includes(job.job_id) && <Button data-testid="action-retry-reanalysis" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${encodeURIComponent(job.job_id)}/retry`, { workspace_id: ws }), { stale: true })) await refresh(); }}>Retry</Button>}
        </div>
      ))}
      <p className="text-sm">Knowledge: <StatePill state={dto.knowledge.status} label={humanize(dto.knowledge.status)} testid="state-knowledge" /><Tech><span className="text-muted"> retrieval index {dto.knowledge.retrieval_index_status}</span></Tech></p>
      <p className="text-sm">Completion: <StatePill state={dto.completion.status} label={humanize(dto.completion.status)} testid="state-completion" /></p>
      <p data-testid="state-outcome" className="text-sm font-medium">{OUTCOME(dto)}</p>
      <div className="grid gap-1 sm:grid-cols-2 text-sm">
        <p>Source before: <code data-testid="source-before">{lin.parent_source_revision_id || "none"}</code></p>
        <p>Source after: <code data-testid="source-after">{lin.source_revision_id || "not published"}</code></p>
        <p>Knowledge before: <code data-testid="km-prior">{lin.prior_knowledge_revision_id || "none"}</code></p>
        <p>Knowledge candidate: <code data-testid="km-candidate">{lin.knowledge_candidate_revision_id || "none"}</code></p>
      </div>
      <Muted>Overall alignment: {humanize(dto.alignment ? dto.alignment.global : "NOT_ASSESSED").toLowerCase()} (this page never claims the whole system is aligned).</Muted>
      {dto.verification && <p className="text-sm break-all">Verification report fingerprint: <Fingerprint value={dto.verification.report_digest} testid="report-digest" /></p>}
      <ExplanationCard ws={ws} subject="change_result" subjectId={dto.apply_job_id} title="AI explanation of where this change stands" />
      {reopened.length > 0 && <h3 className="font-semibold">Decisions the rebuild reopened</h3>}
      {reopened.map((item, i) => <Callout key={i} tone="warn" testid="reopened-decision-item">{p(item.reason || "A decision was reopened by the rebuild.")}<Tech> {item.decision_id ?? ""}</Tech></Callout>)}
      {((dto.manual_work ?? []) as Rec[]).map((m) => (
        <div key={m.index} data-testid="manual-work-item" data-index={m.index} className="rounded-md border border-border p-3 grid gap-2 text-sm">
          <p><strong>Manual work #{m.index}: </strong>{p(m.reason || m.action || "")}</p>
          {m.proof ? <Muted>Proof recorded by {m.proof.actor_id}: {m.proof.evidence_ref}</Muted> : canDecide && (
            <div className="flex flex-wrap gap-2 items-end">
              <Field label="Evidence of the manual work"><TextInput data-testid="field-work-proof" value={proofs[m.index] ?? ""} onChange={(e) => setProofs((p) => ({ ...p, [m.index]: e.target.value }))} /></Field>
              <Button data-testid="action-submit-work-proof" disabled={pending} onClick={async () => {
                const text = (proofs[m.index] ?? "").trim(); if (!text) { notify.status("Describe the evidence first."); return; }
                if (await run(() => api("POST", `/v2/changes/${encodeURIComponent(dto.apply_job_id)}/work-proof`, { workspace_id: ws, work_index: m.index, evidence_ref: text }), { stale: true })) { setProofs((p) => ({ ...p, [m.index]: "" })); await refresh(); }
              }}>Submit proof</Button>
            </div>
          )}
        </div>
      ))}
      {!decided && (
        <div>
          <h3 className="font-semibold">Why this is not complete yet</h3>
          <p data-testid="completion-reasons" className="text-sm">{(dto.completion.blocked_reasons ?? []).length ? <>{(dto.completion.blocked_reasons as string[]).map(words).join("; ")}<Tech> ({(dto.completion.blocked_reasons as string[]).join(", ")})</Tech></> : "no blocking reasons reported"}</p>
        </div>
      )}
      <CompletionFindings dto={dto} disp={disp} setDisp={setDisp} />
      {arts.length > 0 && s.can("evidence.read") && (
        <div className="grid gap-1">
          <h3 className="font-semibold">Exported files (published revision)</h3>
          {arts.map((a) => (
            <a key={a.artifact_id} role="link" tabIndex={0} data-testid="export-link" data-logical-path={a.logical_path} className="text-primary underline cursor-pointer inline-flex items-center gap-1 text-sm"
              onClick={() => void downloadArtifact(ws, cs.revision!.revision_id, a, notify.fail)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void downloadArtifact(ws, cs.revision!.revision_id, a, notify.fail); } }}>
              <Download size={14} aria-hidden="true" />Export {a.logical_path}
            </a>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {s.can("knowledge.confirm") && dto.knowledge.candidate_revision_id && !dto.knowledge.accepted && !decided && dto.state !== "STALE" && (
          <>
            <Button data-testid="action-accept-km" disabled={pending} onClick={async () => {
              const r = await run(() => api("POST", "/v2/knowledge/accept", { workspace_id: ws, knowledge_revision_id: dto.knowledge.candidate_revision_id, source_revision_id: head?.source_revision_id, decision_epoch: head?.decision_epoch, expected_version: head?.version, idempotency_key: newId("km") }), { stale: true });
              if (r) { notify.status("Knowledge candidate accepted."); await refresh(); }
            }}>Accept knowledge candidate</Button>
            <Muted>Accepting pins the candidate to the current source revision and decision epoch.</Muted>
          </>
        )}
      </div>
      {canDecide && (
        <div className="grid gap-2">
          <Field label="Result decision reason (optional)"><TextArea data-testid="field-decision-reason" rows={2} aria-label="Reason for the result decision" value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" data-testid="action-accept-result" title={!dto.completion.eligible ? `Not possible yet: ${((dto.completion.blocked_reasons ?? []) as string[]).map(words).join("; ") || "the completion conditions are not met"}` : undefined} disabled={!digest || !dto.completion.eligible || pending || (groups.length > 0 && !ready)} onClick={() => decide("accept")}>Accept result</Button>
            {dto.completion.eligible && groups.length > 0 && !ready && <Muted testid="accept-needs-ack">Choose for every group above (a reason of at least 10 characters for accepted limits and unrelated); then Accept result is possible.</Muted>}
            {!dto.completion.eligible && <Muted testid="accept-blocked-why">Accept result is not possible yet: {((dto.completion.blocked_reasons ?? []) as string[]).map(words).join("; ") || "the completion conditions are not met"}.</Muted>}
            <Button variant={terminal ? "primary" : undefined} data-testid="action-request-changes" disabled={!digest || pending} onClick={() => decide("request_changes")}>Request changes</Button>
            <Button data-testid="action-reject-result" disabled={!digest || pending} onClick={() => decide("reject")}>Reject result</Button>
          </div>
        </div>
      )}
    </Card>
  );
}

export function LoopCard() {
  const { data } = useFlow();
  const p = usePlain();
  const l = data?.loop;
  if (!l) return null;
  const res = (l.residual_counterevidence ?? []) as Rec[];
  const phrase = ({ PARTIAL: "Partly done - some sources still need work", COMPLETE: "Done", FAILED: "Failed" } as Rec)[l.status] ?? humanize(l.status);
  return (
    <Card title={`${stageTitle("refresh")}: where this change stands`} testid="flow-loop">
      <p className="text-sm" data-testid="loop-supported">Supported edit: <StatePill state={l.supported_edit_status === "VERIFIED" ? "PASSED" : "PENDING"} label={l.supported_edit_status === "VERIFIED" ? "applied and checked" : "not verified"} testid="loop-supported-state" /><span className="text-muted"> - this covers only the edit the system supports, not the whole request.</span></p>
      <p className="text-sm">Overall: <StatePill state={l.status} label={phrase} testid="loop-status" /><span className="text-muted"> {((l.reasons ?? []) as string[]).map(p).join(", ")}</span></p>
      <Muted>{p(l.note)}</Muted>
      <p data-testid="loop-stages" className="text-sm text-muted">Checks {humanize(String(l.stages.verification)).toLowerCase()} - knowledge rebuild {humanize(String(l.stages.knowledge_rebuild)).toLowerCase()} - files re-read {l.stages.reanalysis.completed} of {l.stages.reanalysis.total} - knowledge blockers {String(l.stages.knowledge_blockers)}</p>
      <h3 className="font-semibold">Sources still not at the new value ({res.length})</h3>
      {res.map((x, i) => <Callout key={i} tone="danger" testid="loop-residual"><span data-state={x.current_state}>{basename(x.logical_path)} - {humanize(x.draft_status)} - now: {humanize(x.current_state)}{x.carried_from ? (x.disposition === "outside_this_request" ? " [outside this request - NOT resolved, kept from the earlier draft]" : " [still unresolved - kept from the earlier draft]") : (x.disposition === "potentially_affected" ? " [potentially affected - same rule, a different value; NOT resolved]" : "")}</span></Callout>)}
    </Card>
  );
}

/** What people had confirmed that was given to the AI next to the source text: a person's statement, never source text, never a file change. */
function DecisionsGiven({ items }: { items: Rec[] }) {
  if (!items.length) return null;
  return (
    <div data-testid="query-decisions" className="rounded-md border border-border p-3 grid gap-1 text-sm">
      <h3 className="font-semibold">Decisions given to the AI (statements by people, not source text)</h3>
      <ul className="list-disc pl-5">
        {items.map((x) => (
          <li key={x.decision_id} data-kind={x.kind}>
            {x.kind === "knowledge" ? "A rule of " + basename(x.artifact_path) + " confirmed" : "An open point answered"} by <strong>{x.actor_id}</strong> (decision {x.epoch}, {x.status}):{" "}
            {x.statement_hidden ? <em>the passage is hidden for your role</em> : <span data-verbatim>"{x.statement}"</span>}
          </li>
        ))}
      </ul>
      <Muted>A decision does not change any file. The AI was told to show a disagreement between a decision and a source instead of choosing; whether it used a decision is not checked.</Muted>
    </div>
  );
}

export function QueryStage() {
  const { ids, data, setIds, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { create, pending } = useCreateStage();
  const [q, setQ] = React.useState("");
  const [picked, setPicked] = React.useState<string[] | null>(null);        // null = every current file (the default)
  const d = data;
  const subject = String(((d?.intent?.structured_form ?? {}) as Rec).subject ?? "").trim();
  // A natural question is proposed from the confirmed request; nothing about files needs to be typed or ticked.
  React.useEffect(() => { if (!q && subject) setQ(`What is the ${subject} now?`); }, [subject]);       // eslint-disable-line react-hooks/exhaustive-deps
  if (!d || !ids.apply || !d.apply || !isDone(d.apply)) return null;
  const all = (d.arts ?? []).map((a) => a.artifact_id as string);
  const scope = picked ?? all;
  const narrowed = scope.length < all.length;
  const r = isDone(d.query) ? (d.query!.execution_result ?? {}) : null;
  return (
    <Card title={stageTitle("query")} testid="flow-query">
      <Muted>Ask again on the new revision. The answer must cite the new source text; a claim the new revision does not support is withheld.</Muted>
      <JobLine name="query" job={d.query} usePhase />
      {s.can("source.import") && (
        <div className="grid gap-3">
          <Field label="Your question"><TextInput id="fld-query-q" data-testid="field-query" value={q} onChange={(e) => setQ(e.target.value)} /></Field>
          <p data-testid="query-scope-plan" data-narrowed={String(narrowed)} className="text-sm">
            Ripple will search <strong>{narrowed ? `${scope.length} of the ${all.length} current files you chose` : `all ${all.length} current files`}</strong>.{" "}
            {narrowed ? <strong>A file left out is not searched, so a file that still shows the old value will not be noticed.</strong> : "Searching every file lets Ripple notice a file that still says the old value."}
          </p>
          <details data-testid="query-advanced" className="rounded-md border border-border px-3 py-2">
            <summary className="cursor-pointer text-sm font-medium">Advanced: choose the files to search</summary>
            <div className="grid gap-2 mt-3">
              <Field label="Sources to search (hold Ctrl to pick several)">
                <select multiple id="fld-query-art" data-testid="field-query-artifact" className="border border-border rounded-sm bg-bg px-3 py-2 min-h-24" value={scope} onChange={(e) => setPicked(Array.from(e.target.selectedOptions).map((o) => o.value))}>
                  {(d.arts ?? []).map((a) => <option key={a.artifact_id} value={a.artifact_id}>{a.logical_path}</option>)}
                </select>
              </Field>
              {narrowed && <div><Button data-testid="query-all-files" onClick={() => setPicked(null)}>Search all files again</Button></div>}
            </div>
          </details>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" data-testid="action-run-query" title={aiWhy(s, "gap.answer")} disabled={pending || !s.aiKind("gap.answer")} onClick={async () => {
              if (!q.trim() || !scope.length) { notify.status("Enter a question; at least one file must be searched."); return; }
              const id = newId("ui-query");
              if (await create("gap.answer", id, { question: q.trim(), question_id: id, artifact_ids: scope, intent_id: ids.intent || undefined })) { setIds({ query: id }); await refresh(); }
            }}>Ask on the new revision</Button><EquivalentStep kind="gap.answer" to="ask" />
            <ApproveJobButton job={d.query} label="Approve this model call" testid="action-approve-query" />
          </div>
        </div>
      )}
      {r && (
        <div className="grid gap-2">
          <p className="text-sm">Result: <StatePill state={r.status} label={humanize(r.status)} testid="query-status" /> <strong data-testid="query-answer" data-ai-text>{r.answer ?? ""}</strong></p>
          {String(r.status).toLowerCase() === "abstained" && <Muted testid="query-abstain-note">Abstained is a valid answer, not an error: the sources disagree or do not say, so the system declines to pick one. The question below is what a person has to settle; the citations show where the sources differ.</Muted>}
          {r.question_for_human && <Callout tone="warn">{r.question_for_human}</Callout>}
          <Muted testid="query-scope">This answer only covers: {((r.scope_examined ?? []) as string[]).map((x) => basename(x)).join(", ") || "the files you picked"}{all.length ? ` (${((r.scope_examined ?? []) as string[]).length} of ${all.length} current files)` : ""}. Other files were not searched, so it does not say whether they still show the old value{(d.loop?.residual_counterevidence ?? []).length ? ` (${(d.loop!.residual_counterevidence as unknown[]).length} source(s) are known to still show it - see "Where this change stands")` : ""}. Answered on the current source revision<Tech>: <code>{d.head.source_revision_id}</code></Tech>.</Muted>
          {((r.evidence_spans ?? []) as Rec[]).map((sp, i) => (
            <blockquote key={i} data-testid="query-citation" className="border-l-2 border-border pl-3 text-sm text-muted">{sp.quotes_redacted ? <em data-testid="citation-hidden">Cited source text hidden for your role.</em> : sp.quote}<span className="block text-xs">{basename(sp.artifact_path)}<Tech> @ {sp.source_revision_id}</Tech></span></blockquote>
          ))}
          <DecisionsGiven items={(r.decisions_given ?? []) as Rec[]} />
        </div>
      )}
    </Card>
  );
}
