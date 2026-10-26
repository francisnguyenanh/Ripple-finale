// Review of a CONFIRMED business change (CHG-04): what the rule means before and after, what the change touches, what is still pending for a person, open questions, and
// the separate decisions (accept the knowledge change / approve automatic file edits / clarify). Everything shown comes from the server's review read model; the decide
// calls send back the binding the server gave, so a revised draft or moved knowledge is refused by the server, never by this page alone. All text is rendered as plain text.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Select, StatePill, Tech, TextArea } from "@/components/bits";
import { TrustStateSummary } from "@/components/decision/TrustStateSummary";
import { useAction } from "@/lib/useAction";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";
import { CodeCandidate, CodeRecommendations } from "./CodePanel";
import { ChangeAiPanels } from "./ChangeAiPanels";
import { TaskFrame } from "@/features/request-assistant/TaskFrame";
import { useTaskView } from "@/features/request-assistant/taskView";
import { refusalWords } from "@/lib/refusal";
import { DiffView } from "@/features/changes/ProposalStage";
import { basename } from "@/lib/fmt";
import { ArrowDown } from "lucide-react";
import { jumpTo, pickJump, type JumpTarget } from "./decisionJump";
import { ClauseCompare } from "./ClauseCompare";

const q = encodeURIComponent;
const ACTION_WORDS: Record<string, string> = { approve: "Accepted", reject: "Rejected", request_changes: "Changes requested", clarify: "Clarified" };
const KIND_WORDS: Record<string, string> = { knowledge: "Knowledge change", file_writes: "Automatic file edits", clarification: "Clarification" };
// EUI-03: what a file action IS, never what happened: a candidate edit is not an applied change, and guidance is not an executable diff
const FILE_ACTION_WORDS: Record<string, string> = { AI_EDIT_SUPPORTED: "automatic edit possible (candidate, not applied)", MANUAL: "a person edits it (proposed guidance, no executable diff)",
  NO_OP: "already as requested", UNRESOLVED: "location unresolved" };
const STATUS_WORDS: Record<string, string> = { READY_FOR_REVIEW: "Ready for review", PARTIAL: "Partly resolved: a person still has work to do", INVALID: "Not valid" };
const DISPOSITIONS: [string, string][] = [["answered", "Answer the question"], ["outside_this_request", "Not part of this change"], ["manual_later", "A person will do it after the change"], ["resolved_by_person", "I know where or what it is"]];

function errText(e: unknown): string {
  if (e instanceof ApiError) {
    const body = (e as unknown as { body?: Rec }).body;
    const text = body && typeof body.error === "string" ? (body.error as string) : e.message;
    return text || "The decision could not be recorded.";
  }
  return "The decision could not be recorded.";
}

/** EUI-03: the candidate byte diff of the automatic file edits, read on request (GET, no model): computed by the server from the pinned bytes, nothing staged or applied. */
function CandidateFileDiff({ ws, cid }: { ws: string; cid: string }) {
  const [got, setGot] = React.useState<Rec | null>(null);
  const [state, setState] = React.useState<"idle" | "loading" | "failed">("idle");
  const [why, setWhy] = React.useState("");
  const load = async () => {
    setState("loading");
    try { setGot(await api<Rec>("GET", `/v2/business/changes/${encodeURIComponent(cid)}/file-diff?workspace_id=${encodeURIComponent(ws)}`)); setState("idle"); }
    catch (e) { setWhy(errText(e)); setState("failed"); }
  };
  if (!got) return (
    <div className="grid gap-1">
      <div><Button size="sm" data-testid="rev-file-diff-show" disabled={state === "loading"} onClick={() => void load()}>{state === "loading" ? "Reading the files..." : "Show the candidate file changes"}</Button></div>
      {state === "failed" && <Callout tone="warn" testid="rev-file-diff-unavailable">The candidate file changes could not be shown: {why}</Callout>}
    </div>);
  const cov = (got.coverage ?? {}) as Rec;
  return (
    <div data-testid="rev-file-diff" className="grid gap-2">
      <Muted>{String(got.statement)}</Muted>
      {((got.files ?? []) as Rec[]).map((f) => (
        <div key={String(f.artifact_id)} data-testid="rev-file-diff-file" data-state={String(f.state)} className="grid gap-1">
          <p className="font-medium">{basename(String(f.path))} <span className="text-muted">({f.state === "candidate" ? "candidate, not applied" : String(f.reason ?? f.state)})</span></p>
          {f.state === "candidate" && <DiffView diff={f} />}
        </div>
      ))}
      <Muted testid="rev-file-diff-coverage">Files with an automatic edit: {String(cov.auto_files)}; shown: {String(cov.files_shown)}{Number(cov.files_not_shown) ? `; not shown: ${String(cov.files_not_shown)}` : ""}. Items a person edits: {String(cov.manual_items)}; unresolved: {String(cov.unresolved_items)} (no byte diff for these).</Muted>
    </div>
  );
}

export function BusinessChangeReview({ ws, cid }: { ws: string; cid: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const mode = s.mode;
  const [data, setData] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [reason, setReason] = React.useState("");
  const [ack, setAck] = React.useState(false);
  const [note, setNote] = React.useState<string | null>(null);
  const [clarifyRef, setClarifyRef] = React.useState<string>("");
  const [disposition, setDisposition] = React.useState<string>("manual_later");
  const [answer, setAnswer] = React.useState("");
  const [tick, setTick] = React.useState(0);
  const [journey, setJourney] = React.useState<Rec | null>(null);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/changes/${q(cid)}/journey?workspace_id=${q(ws)}`)
      .then((r) => { if (live) setJourney(r); })
      .catch(() => { if (live) setJourney(null); });                               // the strip is a read model; the review stands without it
    return () => { live = false; };
  }, [cid, ws, tick]);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/changes/${q(cid)}/review?workspace_id=${q(ws)}&mode=${mode}`)
      .then((r) => { if (live) { setData(r); setErr(null); } })
      .catch((e) => { if (!live) return; if (e instanceof ApiError) setErr(e.status === 409 ? null : "The review could not be loaded."); else throw e; });
    return () => { live = false; };
  }, [cid, ws, mode, tick]);
  // a Harbor measurement runs as a job: while it is pending, re-read the review every two seconds (bounded by the server's job states)
  const simStatus = data ? (((data.impact as Rec)?.simulation_state as Rec | undefined)?.status as string | undefined) : undefined;
  React.useEffect(() => {
    if (simStatus !== "pending") return;
    const t = setTimeout(() => setTick((x) => x + 1), 2000);
    return () => clearTimeout(t);
  }, [simStatus, tick]);
  // The task projection is read once here and shared with the task frame (same request, same refresh as before): it says what this person is asked to do now.
  const task = useTaskView(ws, "business_change", data ? cid : null, data);
  if (err) return <Callout tone="danger" testid="rev-error">{err}</Callout>;
  if (!data) return null;
  const askedNow = ((task.tv?.obligations ?? []) as Rec[]).filter((o) => o.obligation_me && o.state === "current").map((o) => String(o.code));
  const draft = data.draft as Rec;
  const impact = data.impact as Rec;
  const can = data.can as Rec;
  const reasons = (can.reasons ?? {}) as Record<string, string>;
  const binding = data.binding as Rec;
  const decisions = (data.decisions ?? []) as Rec[];
  const ruleChanges = (draft.rule_changes ?? []) as Rec[];
  const ops = (draft.operations ?? []) as Rec[];
  const counts = (draft.file_action_counts ?? {}) as Record<string, number>;
  const unresolved = (draft.unresolved ?? []) as Rec[];
  const questions = (draft.questions ?? []) as Rec[];
  const files = (draft.files ?? []) as Rec[];
  const blockers = (can.blockers ?? []) as Rec[];
  const source = (data.source ?? null) as Rec | null;
  const sourceJob = (source?.job ?? null) as Rec | null;
  const openRefs: { ref: string; text: string }[] = [
    ...questions.filter((x) => !x.clarification).map((x) => ({ ref: x.ref as string, text: `Question: ${x.question as string}` })),
    ...unresolved.filter((x) => !x.clarification).map((x) => ({ ref: x.ref as string, text: `${x.ref as string}: ${x.instruction as string}` })),
    ...files.filter((x) => (x.action === "UNRESOLVED" || x.action === "MANUAL") && !x.clarification).map((x) => ({ ref: x.ref as string, text: `${x.ref as string}: ${(x.instruction as string) ?? `${x.action as string} (${x.kind as string})`}` })),
  ];
  const decide = async (kind: string, action: string) => {
    setNote(null);
    try {
      const r = await api<Rec>("POST", `/v2/business/changes/${q(cid)}/review/decide`, { workspace_id: ws, kind, action, reason: reason.trim(), binding, acknowledged_flags: ack });
      setNote(`${KIND_WORDS[kind]}: ${ACTION_WORDS[action]}. Nothing has been applied or committed.`);
      setReason(""); setTick((t) => t + 1);
      return r;
    } catch (e) { setNote(errText(e)); setTick((t) => t + 1); return null; }
  };
  const open = new Set(can.clarify ? openRefs.map((o) => o.ref) : []);
  // EUI-06: the clarification form opens directly under the item it is about (full text, no truncated list); same refs, same four dispositions, same handler
  const clarifyHere = (ref: string, text: string) => !open.has(ref) ? null : clarifyRef !== ref ? (
    <Button size="sm" variant="ghost" data-testid="rev-clarify-open" data-ref={ref} onClick={() => { setClarifyRef(ref); setAnswer(""); setDisposition("manual_later"); }}>Clarify this</Button>
  ) : (
    <div data-testid="rev-clarify" data-ref={ref} className="grid gap-2 rounded-md border border-border p-3 mt-1">
      <p className="text-sm"><span className="text-muted">Clarifying:</span> {text}</p>
      <Field label="What is true for this item" hint="Your answer is recorded with your name. It labels or answers the item; it changes no file and no rule.">
        <Select data-testid="rev-clarify-disposition" value={disposition} onChange={(e) => setDisposition(e.target.value)}>
          {DISPOSITIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
      </Field>
      <TextArea data-testid="rev-clarify-answer" aria-label="Your answer" rows={2} maxLength={1000} placeholder="Your answer (required for an answer or a statement)" value={answer} onChange={(e) => setAnswer(e.target.value)} />
      <div className="flex flex-wrap gap-2">
        <Button data-testid="rev-clarify-submit" disabled={pending || ((disposition === "answered" || disposition === "resolved_by_person") && !answer.trim())} onClick={() => void run(clarify)}>Record clarification</Button>
        <Button variant="ghost" data-testid="rev-clarify-cancel" disabled={pending} onClick={() => setClarifyRef("")}>Cancel</Button>
      </div>
    </div>
  );
  const clarify = async () => {
    setNote(null);
    try {
      await api<Rec>("POST", `/v2/business/changes/${q(cid)}/review/clarify`, { workspace_id: ws, ref: clarifyRef, disposition, answer: answer.trim() || null, reason: answer.trim() || "Clarified.", binding });
      setNote("Your clarification is recorded. The draft is revised: earlier decisions on it are out of date.");
      setAnswer(""); setClarifyRef(""); setTick((t) => t + 1);
    } catch (e) { setNote(errText(e)); setTick((t) => t + 1); }
  };
  const kd = can.knowledge_decision as Rec | null;
  const fd = can.file_writes_decision as Rec | null;
  const why = (k: string) => `rev-why-${k}-${cid}`;                              // SCN-02: a disabled decision names its reason beside it (aria-describedby)
  // SCN-17: the controls this page renders for each obligation, under the same conditions that render them below (so a link never points at nothing).
  const decideAt: JumpTarget = { id: `rev-reason-${cid}`, disabled: false, label: "Go to the decision controls" };
  const jumps: Record<string, JumpTarget | undefined> = {
    decide_knowledge: s.can("change.approve") ? decideAt : undefined,
    decide_file_writes: s.can("change.approve") ? decideAt : undefined,
    commit_knowledge: s.can("knowledge.confirm") && !data.commit
      ? { id: `rev-commit-${cid}`, disabled: !can.commit_knowledge, reasonId: !can.commit_knowledge && reasons.commit_knowledge ? why("commit") : undefined, label: "Go to the commit control" } : undefined,
    apply_source_edits: source && !!data.commit && !sourceJob
      ? { id: `rev-apply-${cid}`, disabled: !can.apply_source_edits, reasonId: !can.apply_source_edits && reasons.apply_source_edits ? why("apply") : undefined, label: "Go to the apply control" } : undefined,
  };
  const jumpLink = (asked: string[]) => {
    const t = pickJump(asked, jumps);
    return t ? (
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary" data-testid="task-jump" data-target={t.id} onClick={() => jumpTo(t)}>{t.label}<ArrowDown size={14} aria-hidden="true" /></Button>
        {t.disabled && <span className="text-sm text-muted" data-testid="task-jump-disabled">Not available to you right now; the reason is shown there.</span>}
      </div>
    ) : null;
  };
  return (
    <>
    <TaskFrame key={`${ws}|${cid}`} ws={ws} family="business_change" id={cid} refreshKey={data} shared={task} detail={jumpLink} />
    <section data-testid="chg-review" data-status={draft.status as string} className="rounded-lg border border-border bg-surface p-4 grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="font-semibold">Review the change</h3>
        <StatePill state={draft.status as string} label={STATUS_WORDS[draft.status as string] ?? (draft.status as string)} testid="rev-status" />
        {data.knowledge_moved && <Callout tone="warn" testid="rev-moved">The business knowledge changed after this request was confirmed. It has to be stated and confirmed again; decisions are refused.</Callout>}
      </div>
      <div className="grid gap-4 items-start xl:grid-cols-[minmax(0,1fr)_minmax(22rem,28rem)]" data-testid="rev-workspace">   {/* EUI-01: evidence / work left, decisions rail right from 1280 px */}
      <div className="grid gap-4 min-w-0" data-testid="rev-evidence">
      <Muted>{String(draft.statement)}</Muted>
      <div data-testid="rev-meaning" className="grid gap-2">
        <p className="text-sm font-medium">What the rule means</p>
        {ruleChanges.map((c, i) => (
          <div key={i} data-testid="rev-rule" data-valid={String(c.valid)} className="rounded-md border border-border p-3 text-sm grid gap-1">
            <p className="font-medium">{c.title as string}</p>
            <ClauseCompare before={c.before} after={c.after} />
            {((c.aspects ?? []) as Rec[]).length > 0 && (
              <ul className="grid gap-1" data-testid="rev-aspects" aria-label="What changes in the rule">
                {((c.aspects ?? []) as Rec[]).map((a, j) => (
                  <li key={j} data-testid="rev-aspect" data-aspect={String(a.aspect)} className="rounded-sm border-l-4 border-warning bg-surface-2 px-2 py-1">
                    <span className="font-medium">{String(a.label)}:</span>{" "}
                    {a.before != null && <span>{String(a.before)}</span>}{a.before != null && a.after != null && <span aria-label="becomes"> → </span>}
                    {a.after != null && <strong>{String(a.after)}</strong>}{a.unit ? <span className="text-muted"> {String(a.unit)}</span> : null}
                  </li>
                ))}
              </ul>
            )}
            <Tech>{((c.changes ?? []) as string[]).map((t, j) => <p key={j} data-testid="rev-change">{t}</p>)}</Tech>
            {((c.aspects ?? []) as Rec[]).length === 0 && ((c.changes ?? []) as string[]).map((t, j) => <p key={j} data-testid="rev-change-plain">{t}</p>)}
            {((c.flags ?? []) as string[]).map((t, j) => <Callout key={j} tone="warn" testid="rev-flag">{t}</Callout>)}
            {!c.valid && <Callout tone="danger">{String(c.error)}</Callout>}
          </div>
        ))}
      </div>
      <div data-testid="rev-impact" className="grid gap-1 text-sm">
        <p className="font-medium">What it touches</p>
        <ul className="list-disc pl-5">{((impact.affected_rules ?? []) as Rec[]).map((a, i) => <li key={i} data-testid="rev-affected" data-link={a.link as string} data-ref-key={a.ref_key as string | undefined} tabIndex={-1}>{a.title as string}: {a.meaning as string} ({a.severity as string}){a.unknown ? ", files unknown" : ""}{Array.isArray(a.versions) && <span data-testid="rev-affected-versions"> - reached in {(a.versions as Rec[]).length} versions: {(a.versions as Rec[]).map((v) => `${v.view === "target" ? "accepted target" : "observed in the sources"} (${v.meaning as string})`).join("; ")}</span>}</li>)}</ul>
        {((impact.excluded ?? []) as string[]).map((t, i) => <Muted key={i}>{t}</Muted>)}
        {((draft.counterevidence ?? []) as Rec[]).map((c, i) => <Muted key={i} testid="rev-counter">{c.title as string}: {c.label as string}</Muted>)}
        {((draft.proposed_dependencies ?? []) as Rec[]).map((d, i) => <Muted key={i} testid="rev-dep">{d.from as string} {d.relation as string} {d.to as string}: {d.label as string}</Muted>)}
        <Muted testid="rev-coverage">{(draft.coverage as Rec).complete ? "Everything was checked." : "Not everything could be checked: items marked unknown or unresolved are not “no impact”."}</Muted>
        {impact.simulation_state && (
          <div data-testid="rev-simulation" data-status={(impact.simulation_state as Rec).status as string} className="grid gap-1 rounded-md border border-border p-3">
            <p className="font-medium">Measured on the Harbor sample</p>
            {((impact.simulation ?? []) as Rec[]).map((row, i) => <Muted key={i} testid="rev-sim-op">{row.status as string}{row.reason ? ` — ${row.reason as string}` : ""}</Muted>)}
            {((impact.measured ?? []) as Rec[]).map((m, i) => (
              <div key={i} data-testid="rev-measured" className="text-sm grid gap-1">
                <p>{String(m.transactions)} requisitions run; <strong>{String(m.changed_count)}</strong> would be routed differently under the proposed value.</p>
                {((m.not_controlled ?? []) as Rec[]).map((n, j) => <Callout key={j} tone="warn" testid="rev-not-controlled">{n.behaviour as string}: {n.reason as string}</Callout>)}
                <Muted>{m.statement as string}</Muted>
              </div>
            ))}
            {impact.measured_statement && <Muted>{impact.measured_statement as string}</Muted>}
            {(impact.simulation_state as Rec).measurable && ["not_requested", "failed"].includes((impact.simulation_state as Rec).status as string) && s.can("change.prepare") && (
              <div><Button data-testid="rev-simulate" disabled={pending} onClick={async () => {
                const r = await run(() => api<Rec>("POST", `/v2/business/changes/${q(cid)}/impact/simulate`, { workspace_id: ws }));
                if (r) { setNote("Measuring on the Harbor sample; the counts appear when the run is done."); setTick((t) => t + 1); }
              }}>Measure on the Harbor sample</Button></div>
            )}
            {(impact.simulation_state as Rec).status === "pending" && <Muted testid="rev-sim-pending">The measurement is running.</Muted>}
          </div>
        )}
      </div>
      <div data-testid="rev-pending" className="grid gap-1 text-sm">
        <p className="font-medium">Pending implementation (nothing is changed by this review)</p>
        <p data-testid="rev-counts">Automatic edits possible: {counts.AI_EDIT_SUPPORTED ?? 0}. By hand: {counts.MANUAL ?? 0}. Already as requested: {counts.NO_OP ?? 0}. Unresolved: {counts.UNRESOLVED ?? 0}.</p>
        {ops.map((o) => <p key={o.what as string} data-testid="rev-op" data-coverage={o.file_coverage as string}>{o.what as string} — rule change {String(o.rule_change).toLowerCase().replace(/_/g, " ")}, files {o.file_coverage as string}.</p>)}
        {files.length > 0 && <ul className="list-disc pl-5" data-testid="rev-files">{files.map((f) => <li key={f.ref as string} data-testid="rev-file" data-action={f.action as string}>{f.ref as string} {f.path as string}{f.locator ? ` at ${f.locator as string}` : ""}: <span data-testid="rev-file-action">{FILE_ACTION_WORDS[f.action as string] ?? `state ${String(f.action)}`}</span>{f.instruction ? ` — ${f.instruction as string}` : ""}{f.clarification ? ` [${(f.clarification as Rec).label as string}]` : ""}{clarifyHere(f.ref as string, `${f.ref as string}: ${(f.instruction as string) ?? `${f.action as string} (${f.kind as string})`}`)}</li>)}</ul>}
        {files.some((f) => f.action === "AI_EDIT_SUPPORTED") && <CandidateFileDiff ws={ws} cid={cid} />}
        {unresolved.map((u) => <div key={u.ref as string}><p data-testid="rev-unresolved">{u.ref as string}: {u.instruction as string}{u.clarification ? ` [${(u.clarification as Rec).label as string}]` : ""}</p>{clarifyHere(u.ref as string, `${u.ref as string}: ${u.instruction as string}`)}</div>)}
        {(draft.files_withheld as number) > 0 && <Muted testid="rev-withheld">{String(draft.files_withheld)} file detail(s) need evidence rights to be shown.</Muted>}
      </div>
      {(questions.length > 0 || blockers.length > 0) && (
        <div data-testid="rev-questions" className="grid gap-1 text-sm">
          <p className="font-medium">Open questions</p>
          {questions.map((x) => <div key={x.ref as string}><p data-testid="rev-question" data-answered={String(!!x.clarification)}>{x.question as string}{x.clarification ? ` — ${(x.clarification as Rec).label as string}${(x.clarification as Rec).answer ? `: ${(x.clarification as Rec).answer as string}` : ""}` : ""}</p>{clarifyHere(x.ref as string, `Question: ${x.question as string}`)}</div>)}
          {blockers.map((b, i) => <Callout key={i} tone="warn" testid="rev-blocker">{b.text as string}</Callout>)}
        </div>
      )}
      </div>
      <aside className="grid gap-4 content-start min-w-0" aria-label="Decisions on this change" data-testid="rev-rail">
        {note && <Callout tone={note.startsWith("The ") || note.includes("not") && !note.includes("Nothing") && !note.includes("not changed") ? "warn" : "info"} testid="rev-note">{note}</Callout>}
      <div data-testid="rev-decisions" className="grid gap-1 text-sm">
        <p className="font-medium">Decisions (each one is separate)</p>
        <Muted>{String(data.decisions_are_separate)}</Muted>
        {decisions.length === 0 && <Muted testid="rev-no-decisions">No decision yet.</Muted>}
        {decisions.map((d, i) => <p key={i} data-testid="rev-decision" data-kind={d.kind as string} data-action={d.action as string} data-current={String(d.current)}>{KIND_WORDS[d.kind as string]}: {ACTION_WORDS[d.action as string]} by {d.by as string}{d.self_approval ? " (self-approval, allowed by this workspace)" : ""} — {d.reason as string}{d.current ? "" : ` [out of date: ${d.stale_reason as string}]`}</p>)}
      </div>
      {s.can("change.approve") && (
        <div data-testid="rev-decide" className="grid gap-2 rounded-md border border-border p-3">
          {/* "Your decision" only when the projection asks this person to decide now; a person who may decide but is not asked reads "Decision" (SCN-17) */}
          <Field label={askedNow.some((c) => c === "decide_knowledge" || c === "decide_file_writes") ? "Your decision" : "Decision"} hint="Accepting the knowledge change does not approve any file edit; approving file edits does not accept the change. Nothing is applied here.">
            <TextArea id={`rev-reason-${cid}`} data-testid="rev-reason" rows={2} maxLength={1000} placeholder="Why, in your own words" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          {((can.flags_to_acknowledge ?? []) as string[]).length > 0 && (
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="rev-ack" checked={ack} onChange={(e) => setAck(e.target.checked)} />I have read the warnings above and mean this change.</label>
          )}
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" data-testid="rev-approve" disabled={pending || !reason.trim() || !can.approve_knowledge} title={!can.approve_knowledge ? reasons.approve_knowledge : undefined} aria-describedby={!can.approve_knowledge && reasons.approve_knowledge ? why("knowledge") : undefined} onClick={() => void run(() => decide("knowledge", "approve"))}>Accept the knowledge change</Button>
            <Button data-testid="rev-request-changes" disabled={pending || !reason.trim() || !!kd && kd.action !== "request_changes" || data.knowledge_moved === true} onClick={() => void run(() => decide("knowledge", "request_changes"))}>Request changes</Button>
            <Button data-testid="rev-reject" disabled={pending || !reason.trim() || !!kd && kd.action !== "request_changes" || data.knowledge_moved === true} onClick={() => void run(() => decide("knowledge", "reject"))}>Reject</Button>
            <Button data-testid="rev-approve-files" disabled={pending || !reason.trim() || !can.approve_file_writes} title={!can.approve_file_writes ? reasons.approve_file_writes : undefined} aria-describedby={!can.approve_file_writes && reasons.approve_file_writes ? why("files") : undefined} onClick={() => void run(() => decide("file_writes", "approve"))}>Approve the automatic file edits</Button>
          </div>
          {!can.approve_knowledge && reasons.approve_knowledge && <Muted id={why("knowledge")} testid="rev-why-not">{reasons.approve_knowledge}</Muted>}
          {!can.approve_file_writes && reasons.approve_file_writes && <Muted id={why("files")} testid="rev-why-not-files">{reasons.approve_file_writes}</Muted>}
          {kd && <Muted testid="rev-current-knowledge">Current knowledge decision: {ACTION_WORDS[kd.action as string]} by {kd.by as string}.</Muted>}
          {fd && <Muted testid="rev-current-files">Current file-edit decision: {ACTION_WORDS[fd.action as string]} by {fd.by as string}.</Muted>}
        </div>
      )}
      <div data-testid="rev-completion" data-knowledge={String((data.completion as Rec)?.knowledge_result)} className="grid gap-1 text-sm rounded-md border border-border p-3">
        <p className="font-medium">Where this change stands (four separate axes)</p>
        <TrustStateSummary axes={[
          { key: "knowledge", label: "Knowledge", raw: (data.completion as Rec)?.knowledge_result },
          { key: "source", label: "Source files", raw: (data.completion as Rec)?.source_sync },
          { key: "code", label: "Code", raw: (data.completion as Rec)?.code_sync },
          { key: "behaviour", label: "Behaviour", raw: (data.completion as Rec)?.behavioral_verification },
        ]} testid="rev-axes" />
        <Muted>{String((data.completion as Rec)?.statement)}</Muted>
        {data.commit ? <Muted testid="rev-committed">Committed as business version {String((data.commit as Rec).business_revision)} by {String((data.commit as Rec).by)}.</Muted> : null}
        {s.can("knowledge.confirm") && !data.commit && (
          <div className="flex flex-wrap items-center gap-2">
            <Button id={`rev-commit-${cid}`} variant="primary" data-testid="rev-commit" disabled={pending || !can.commit_knowledge} title={!can.commit_knowledge ? reasons.commit_knowledge : undefined} aria-describedby={!can.commit_knowledge && reasons.commit_knowledge ? why("commit") : undefined}
              onClick={() => void run(async () => {
                setNote(null);
                try {
                  const r = await api<Rec>("POST", `/v2/business/changes/${q(cid)}/commit`, { workspace_id: ws, binding });
                  setNote(r.replayed ? "This change was already committed." : `Knowledge change committed as business version ${String(r.business_revision)}. Files and code are not changed.`);
                } catch (e) { setNote(errText(e)); }
                setTick((t) => t + 1);
              })}>Commit the knowledge change</Button>
            {!can.commit_knowledge && reasons.commit_knowledge && <Muted id={why("commit")} testid="rev-why-not-commit">{reasons.commit_knowledge}</Muted>}
          </div>
        )}
        {source && (
          <div data-testid="rev-source" data-job={String(sourceJob?.status ?? "none")} className="grid gap-2 rounded-md border border-border p-3">
            <p className="font-medium">Source files: your choice</p>
            <Muted>{String(source.statement)}</Muted>
            {source.automatic_edit_available
              ? <p data-testid="rev-source-auto">One automatic edit is possible{source.file_writes_approved ? " and approved" : " (not approved yet)"}. By hand: {(source.manual as Rec[]).length}.</p>
              : <p data-testid="rev-source-auto">No automatic edit is possible; every item is done by a person ({(source.manual as Rec[]).length}).</p>}
            {sourceJob && <p data-testid="rev-source-job">Automatic edit job: {String(sourceJob.status).toLowerCase()}{sourceJob.reason ? ` (${String(sourceJob.reason)})` : ""}.{sourceJob.published ? " A new source revision was published; the next analysis reads it back." : ""}</p>}
            {!!data.commit && !sourceJob && (
              <div className="flex flex-wrap items-center gap-2">
                <Button id={`rev-apply-${cid}`} variant="primary" data-testid="rev-apply-source" disabled={pending || !can.apply_source_edits} title={!can.apply_source_edits ? reasons.apply_source_edits : undefined} aria-describedby={!can.apply_source_edits && reasons.apply_source_edits ? why("apply") : undefined}
                  onClick={() => void run(async () => {
                    setNote(null);
                    try {
                      const r = await api<Rec>("POST", `/v2/business/changes/${q(cid)}/source/apply`, { workspace_id: ws, mode: "ai", binding });
                      setNote(r.created ? "The approved edit is being written into a new source revision. Code and documents are not changed." : "The automatic edit job already exists.");
                    } catch (e) { setNote(errText(e)); }
                    setTick((t) => t + 1);
                  })}>Let Ripple write the approved edit</Button>
                <Button data-testid="rev-manual-source" disabled={pending}
                  onClick={() => void run(async () => {
                    setNote(null);
                    try {
                      const r = await api<Rec>("POST", `/v2/business/changes/${q(cid)}/source/apply`, { workspace_id: ws, mode: "manual", binding });
                      setNote(`Nothing is written by Ripple. ${(r.manual as Rec[]).length} item(s) to edit by hand; refresh the sources afterwards so the knowledge can read them back.`);
                    } catch (e) { setNote(errText(e)); }
                  })}>A person edits the files</Button>
                {!can.apply_source_edits && reasons.apply_source_edits && <Muted id={why("apply")} testid="rev-why-not-apply">{reasons.apply_source_edits}</Muted>}
              </div>
            )}
          </div>
        )}
      </div>
      </aside>
      </div>
      <CodeRecommendations ws={ws} cid={cid} />
      <ChangeAiPanels ws={ws} cid={cid} knowledgeMoved={data.knowledge_moved === true} />
      {data.commit ? <CodeCandidate ws={ws} cid={cid} /> : <Muted testid="code-candidate-later">{refusalWords("knowledge_not_committed")}</Muted>}
      {journey && (
        <div data-testid="rev-journey" data-scope={String(journey.scope)} data-knowledge-complete={String(journey.knowledge_only_complete)} data-implemented={String(journey.fully_implemented)} className="grid gap-2 rounded-md border border-border p-3 text-sm">
          <p className="font-medium">Journey ({String(journey.scope) === "NARROW" ? "knowledge-only change" : "knowledge, files and code"})</p>
          <ol className="grid gap-1">
            {((journey.steps ?? []) as Rec[]).map((st) => (
              <li key={st.key as string} data-testid="rev-journey-step" data-step={st.key as string} data-state={st.state as string} className="flex flex-wrap gap-2">
                <StatePill state={st.state as string} label={String(st.state).replace(/_/g, " ")} />
                <span><strong>{st.title as string}.</strong> {st.what ? `${st.what as string} ` : ""}{st.why ? <Muted>{st.why as string}</Muted> : null}</span>
              </li>
            ))}
          </ol>
          {journey.next ? <p data-testid="rev-journey-next"><strong>Next:</strong> {String((journey.next as Rec).what)} <Muted>({String((journey.next as Rec).who)})</Muted></p>
                        : <p data-testid="rev-journey-next">{journey.rejected ? "This change was rejected; nothing else happens to it." : "No next step is open."}</p>}
          <Muted testid="rev-journey-statement">{String(journey.statement)}</Muted>
        </div>
      )}
      <Tech>
        <div data-testid="rev-technical" className="rounded-md border border-border p-3 text-xs grid gap-1">
          <p>semantic diff <code>{String(binding.semantic_diff_hash)}</code></p>
          <p>draft <code>{String(binding.draft_hash)}</code>{binding.edits_hash ? <> edits <code>{String(binding.edits_hash)}</code></> : null}</p>
          {(draft.technical as Rec | undefined) && <p>pin <code>{JSON.stringify((draft.technical as Rec).pin)}</code>; write scope {(((draft.technical as Rec).write_scope ?? []) as string[]).join(", ")}</p>}
        </div>
      </Tech>
    </section>
    </>
  );
}
