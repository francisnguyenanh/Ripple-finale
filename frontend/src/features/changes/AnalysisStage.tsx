import { stageNo, stageTitle } from "./registry";
import { StageCard } from "./StageCard";
import { SimulationAnalysisCard } from "@/features/explain/SimulationAnalysisCard";
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, HowItWorks, Muted, Tech, usePlain } from "@/components/bits";
import { basename, humanize, newId, ruleTitle } from "@/lib/fmt";
import { useSession } from "@/session";
import { aiWhy } from "@/lib/aiKinds";
import { useNotify } from "@/lib/notify";
import { draftResult } from "./DraftStage";
import { useFlow } from "./flow";
import { ensureCoverage, readCoverage } from "./Coverage";
import { JobLine, useCreateStage } from "./StageBits";
import type { Rec } from "./types";
import { isDone, isTerminal } from "./types";
import { SimulationOutcome } from "@/components/decision/SimulationOutcome";
import { outcomeOf } from "@/components/decision/outcomeModel";

const REASONS: Record<string, string> = {
  no_confirmed_relation_evidence_bounded_investigation_not_run: "No relation evidence covers this rule and the target - UNKNOWN (not 'unaffected'). Compute relation coverage and run the impact analysis again.",
  candidate_not_found: "Not a rule of this workspace (or not visible to you).",
  no_target_candidate_resolved: "No target rule could be resolved.",
};

type Labels = Record<string, { label?: string; logical_path?: string }>;
const SHOWN = 10;

export function ImpactReport({ res, relationJobs = 0 }: { res: Rec; relationJobs?: number }) {
  const { ws } = useFlow();
  const p = usePlain();
  const tech = useSession().mode === "technical";
  const [labels, setLabels] = React.useState<Labels>({});
  React.useEffect(() => {
    let live = true;
    if (res.target_candidate_id) {
      api<{ candidates?: Labels }>("GET", `/v2/change/impact-context?workspace_id=${encodeURIComponent(ws)}&target_candidate_id=${encodeURIComponent(res.target_candidate_id)}`)
        .then((cx) => { if (live) setLabels(cx.candidates ?? {}); }).catch(() => undefined);
    }
    return () => { live = false; };
  }, [ws, res.target_candidate_id]);
  const words = (l: string) => {                                                         // the server label is "subject / predicate = value": Business view names the rule in words
    const m = /^(.*?) \/ (.*?) = (.*)$/.exec(l);
    return tech || !m ? l : ruleTitle(m[1], m[2]);
  };
  const lab = (id: string) => (labels[id]?.label ? words(labels[id].label!) + (labels[id].logical_path ? ` (${tech ? labels[id].logical_path : basename(labels[id].logical_path)})` : "") : "a related rule");
  const cite = (ev: Rec[] | undefined) => (ev ?? []).map((e, i) => <span key={i} className="block text-muted">cites <span data-verbatim>{e.quote || e.text || ""}</span><Tech> <code>{JSON.stringify(e.locator)}</code></Tech></span>);
  const uid = React.useId().replace(/:/g, "");
  // UX-09: a long category shows its first items; the rest open on demand. The count in the heading and in the worklist is always the full count.
  const section = (title: string, items: Rec[], render: (x: Rec) => React.ReactNode, testid: string) => (
    <div data-testid={testid} id={`${uid}${testid}`} tabIndex={-1} className="grid gap-1">
      <h4 className="font-semibold">{title} ({items.length})</h4>
      {items.length ? <ul className="list-disc pl-5 text-sm grid gap-1">{items.slice(0, SHOWN).map((x, i) => <li key={i}>{render(x)}</li>)}</ul> : <Muted>none</Muted>}
      {items.length > SHOWN && (
        <details data-testid={`${testid}-more`}><summary className="cursor-pointer text-sm text-primary">Show all {items.length} ({items.length - SHOWN} more)</summary>
          <ul className="list-disc pl-5 text-sm grid gap-1">{items.slice(SHOWN).map((x, i) => <li key={i}>{render(x)}</li>)}</ul>
        </details>
      )}
    </div>
  );
  const worklist = [["impact-direct", "Directly affected", res.direct], ["impact-indirect", "Text-scan hints", res.indirect], ["impact-indirect-model", "AI judgement", res.indirect_model],
    ["impact-unknown", "Unknown (not covered)", res.unknown], ["impact-excluded", "Excluded", res.excluded]] as const;
  const cov = (res.coverage ?? {}) as Rec;
  const why = [
    (cov.omitted_relation_jobs ?? []).length && "some relation jobs were unusable",
    (cov.unresolved_candidate_ids ?? []).length && "some rules could not be resolved",
    cov.truncated && "the context was truncated",
    cov.omitted_scan_observations && `the scan omitted ${cov.omitted_scan_observations} observations`,
    (res.unknown ?? []).length && `${(res.unknown ?? []).length} rule(s) are UNKNOWN`,
  ].filter(Boolean).join("; ");
  return (
    <div data-testid="impact-report" className="grid gap-3">
      <nav aria-label="Impact categories" data-testid="impact-worklist" className="flex flex-wrap gap-1.5 text-sm">
        {worklist.filter(([id, , items]) => id !== "impact-indirect-model" || (items ?? []).length > 0).map(([id, label, items]) => (
          <a key={id} href={`#${uid}${id}`} data-testid="impact-worklist-item" data-key={id} data-count={(items ?? []).length}
            className="rounded-full border border-border px-2 py-0.5 hover:bg-surface-2" onClick={(e) => { e.preventDefault(); const el = document.getElementById(`${uid}${id}`); el?.scrollIntoView({ block: "start" }); el?.focus(); }}>
            {label}: {(items ?? []).length}</a>))}
      </nav>
      {section(tech ? "Direct" : "Directly affected", res.direct ?? [], (x) => <><strong>{lab(x.candidate_id)}</strong><Tech> path: {(x.path ?? []).join(" → ")}</Tech>{cite(x.evidence)}</>, "impact-direct")}
      {section(tech ? "Indirect (file level, text-scan hints)" : "Possibly affected: text-scan hints (file level, not an AI finding)", res.indirect ?? [], (x) => <><strong>{basename(x.logical_path)}</strong><Tech> rule {x.rule}; path: {(x.path ?? []).join(" → ")}</Tech>{cite(x.evidence)}</>, "impact-indirect")}
      {(res.indirect_model ?? []).length > 0 && section("Indirectly affected (AI judgement, re-checked by the server)", res.indirect_model, (x) => <><strong>{basename(x.logical_path)}</strong> quote: <span data-verbatim>{x.evidence_quote}</span></>, "impact-indirect-model")}
      {section("Unknown", res.unknown ?? [], (x) => <><strong>{lab(x.candidate_id)}</strong> - {x.reason === "no_confirmed_relation_evidence_bounded_investigation_not_run" && relationJobs > 0 ? "No confirmed relation links this rule to the target - UNKNOWN (not 'unaffected'). The relation job already ran and kept no confirmed link, so running it again gives the same answer." : REASONS[x.reason] ?? p(x.reason)}</>, "impact-unknown")}
      {section("Excluded", res.excluded ?? [], (x) => <><strong>{lab(x.candidate_id)}</strong> - {REASONS[x.reason] ?? p(x.reason)}</>, "impact-excluded")}
      {(res.existing_conflicts ?? []).length > 0 && section("Existing findings carried forward", res.existing_conflicts, (x) => `${humanize(x.kind ?? "finding")}: ${p(x.reason ?? "")}`, "impact-findings")}
      <p data-testid="impact-coverage" data-traversal={String(!!cov.traversal_complete)} data-impact={String(!!cov.impact_complete)} className={`text-sm ${cov.impact_complete ? "text-muted" : "text-warning"}`}>
        {cov.impact_complete ? "Impact fully resolved (no UNKNOWN rule)." : `${cov.traversal_complete ? "Traversal finished, but the impact is NOT fully resolved" : "Traversal INCOMPLETE"}: ${why}`}
      </p>
      <HowItWorks testid="impact-how" title="What kinds of evidence this page shows"><Muted testid="impact-kinds">Three different kinds of evidence appear on this page. <strong>AI-extracted rules</strong> carry a quote that the server checked against the stored file. <strong>Text-scan hints</strong> come from a deterministic scan (a number or file name that also appears elsewhere): a place to look, not a proven dependency and not an AI finding. The <strong>simulation</strong> runs the real, pinned Harbor code with the changed configuration. Paths come from stored relations and a deterministic text scan of the pinned revision. A graph projection is not a causal dependency; UNKNOWN means not covered, not unaffected.</Muted></HowItWorks>
    </div>
  );
}

export function SimulationReport({ h }: { h: Rec }) {
  const p = usePlain();
  const tech = useSession().mode === "technical";
  return (
    <div data-testid="sim-report" data-changed={String(h.changed_count)} data-unexplained={String(h.unexplained_changes)} className="grid gap-3">
      <h4 className="font-semibold flex items-center gap-2"><span className="rounded-full border border-border px-2 text-xs">DETERMINISTIC</span>Harbor itself, before vs after</h4>
      <p data-testid="sim-integrity" className="text-sm text-muted">
        Adapter check: the starting configuration reproduced Harbor's shipped output ({h.integrity?.rows}/{h.integrity?.rows} rows {h.integrity?.seed_equals_shipped ? "identical" : "DIFFERENT"}).
        <Tech> Baseline: {h.baseline_source?.logical_path ?? h.baseline_source?.kind}{h.baseline_source?.source_revision_id ? ` @ ${h.baseline_source.source_revision_id}` : ""}.</Tech>
      </p>
      <p className="text-sm">Edit(s): {((h.edits ?? []) as Rec[]).map((e) => `${e.from} → ${e.to}${e.no_op ? " (no change)" : ""}`).join("; ")}<Tech> <code>{((h.edits ?? []) as Rec[]).map((e) => e.path).join(", ")}</code></Tech></p>
      <SimulationOutcome o={outcomeOf(h)} tech={tech} />
      {h.snapshot && <Tech><p data-testid="sim-snapshot" className="text-sm text-muted">{h.snapshot.statement}. Code {h.snapshot.code_sha256.slice(0, 10)}, input {h.snapshot.input_sha256.slice(0, 10)} ({(h.snapshot.input_files ?? []).join(", ")}), config {h.snapshot.config_baseline_sha256.slice(0, 10)} vs {h.snapshot.config_proposed_sha256.slice(0, 10)}.</p></Tech>}
      {(h.not_controlled ?? []).length > 0 && (
        <div data-testid="sim-not-controlled"><h4 className="font-semibold">The configuration does not control this behaviour</h4>
          <ul className="list-disc pl-5 text-sm">{(h.not_controlled as Rec[]).map((n, i) => <li key={i}>{p(n.behaviour)}: {p(n.proof)}<Tech> [{n.evidence.file}:{n.evidence.line} {n.evidence.text}]</Tech></li>)}</ul></div>
      )}
      {(h.no_change_edits ?? []).length > 0 && (
        <div data-testid="sim-no-change"><h4 className="font-semibold">No change in this sample (the code does read this value)</h4>
          <ul className="list-disc pl-5 text-sm">{(h.no_change_edits as Rec[]).map((n, i) => <li key={i}>{n.from} → {n.to}<Tech>: consumed at {(n.consumers as Rec[]).map((c) => `${c.file}:${c.line}`).join(", ")}</Tech></li>)}</ul></div>
      )}
      {(h.inert_edits ?? []).length > 0 && (
        <div data-testid="sim-inert"><h4 className="font-semibold">Edits the code does not read</h4>
          <ul className="list-disc pl-5 text-sm">{(h.inert_edits as Rec[]).map((n, i) => <li key={i}>{n.from} → {n.to}: {p(n.reason)}<Tech>{n.code_ref ? ` [${n.code_ref.file}:${n.code_ref.line}]` : ""}</Tech></li>)}</ul></div>
      )}
      <h4 className="font-semibold">Limits of this simulation</h4>
      <ul className="list-disc pl-5 text-sm">{((h.limits ?? []) as string[]).map((t, i) => <li key={i}>{p(t)}</li>)}</ul>
      <Muted testid="sim-runner">{h.runner?.statement}</Muted>
    </div>
  );
}

export function AnalysisStage() {
  const { ws, data, setIds, refresh } = useFlow();
  const p = usePlain();
  const s = useSession();
  const notify = useNotify();
  const { create, pending, run } = useCreateStage();
  const [picked, setPicked] = React.useState<string>("");
  const [rel, setRel] = React.useState<{ jobs: number; uncovered: number; total: number } | null>(null);
  const [relTick, setRelTick] = React.useState(0);
  const [relReadFor, setRelReadFor] = React.useState("");      // which rule the count above was read for (or the read failed for): until then the button waits
  const [inv, setInv] = React.useState<{ step: number; total: number; label: string } | null>(null);       // all hooks stay ABOVE the early return below
  const [invNote, setInvNote] = React.useState<string>("");
  const d = data;
  const relTarget = (() => {
    const edits = (draftResult(d?.draft ?? null)?.edits ?? []) as Rec[];
    const ids = Array.from(new Set(edits.flatMap((e) => (e.candidate_ids ?? []) as string[])));
    return ids.length === 1 ? ids[0] : (ids.length > 1 ? picked : "");       // only a rule the draft or the person made definite; never a guess
  })();
  // How many relation jobs the server holds for this rule (no AI call). Read when the page opens and after the button; a created job is awaited a few times, then we stop.
  React.useEffect(() => {
    let live = true;
    if (!ws || !relTarget) { setRel(null); return; }
    const read = async (): Promise<number> => {
      try {
        const cx = await api<Rec>("GET", `/v2/change/impact-context?workspace_id=${encodeURIComponent(ws)}&target_candidate_id=${encodeURIComponent(relTarget)}`);
        if (live) { setRel({ jobs: (cx.relation_job_ids ?? []).length, uncovered: (cx.uncovered_candidate_ids ?? []).length, total: (cx.candidate_ids ?? []).length }); setRelReadFor(`${ws}|${relTarget}`); }
        return (cx.relation_job_ids ?? []).length;
      } catch { if (live) { setRel(null); setRelReadFor(`${ws}|${relTarget}`); } return -1; }    // unreadable: the button works as before; the server re-checks
    };
    void (async () => {
      const first = await read();
      if (relTick === 0) return;
      for (let i = 0; i < 5 && live; i++) { await new Promise((r) => setTimeout(r, 2000)); const n = await read(); if (n > first) break; }
    })();
    return () => { live = false; };
  }, [ws, relTarget, relTick, d?.draft?.job_id]);
  if (!d || !d.intent) return null;
  const confirmed = d.intent.status === "CONFIRMED";
  const mayRun = s.can("change.prepare");
  const dr = draftResult(d.draft);
  const targetOptions = (() => {
    const seen = new Map<string, string>();
    for (const e of (dr?.edits ?? []) as Rec[]) for (const id of (e.candidate_ids ?? []) as string[]) {
      if (!seen.has(id)) seen.set(id, `${e.logical_path ?? "edit"}${e.proposal_edit?.path ? ` · ${e.proposal_edit.path}` : ""}${e.proposal_edit ? ` (${e.proposal_edit.expected_current ?? "?"} → ${e.proposal_edit.proposed})` : ""}`);
    }
    return [...seen.entries()].map(([id, label]) => ({ id, label }));
  })();
  const ambiguous = targetOptions.length > 1 && !targetOptions.some((o) => o.id === picked);
  // never first-wins: several extracted rules match this draft, so the person chooses which one the analysis is about
  const target = (): string | undefined => {
    if (targetOptions.length === 1) return targetOptions[0].id;
    if (targetOptions.length > 1) return targetOptions.find((o) => o.id === picked)?.id;
    return s.route.params.rule || d.intent?.target || undefined;
  };
  // One idempotent server step creates the relation coverage AND the conflict check of the current revision (deterministic, no AI); a second click creates nothing.
  const relations = async (): Promise<boolean> => {
    const cov = await run(() => ensureCoverage(ws));
    if (!cov) return false;
    setRelTick((n) => n + 1);
    notify.status("The relation coverage and the conflict check were started (no AI call). When they end, run the impact analysis so it uses them. Without them, other rules stay UNKNOWN.");
    return true;
  };
  // The impact analysis needs the relation coverage first. When none exists for this revision it is computed here, waited for a bounded time (about 10 s).
  const ensureRelations = async (t: string) => {
    if (rel === null || rel.jobs > 0 || !t) return;
    notify.status("Computing the relation coverage first (no AI call).");
    if (!(await relations())) return;
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      try { if ((await readCoverage(ws)).relation.state !== "running" && (await readCoverage(ws)).relation.state !== "none") return; } catch { return; }
    }
    notify.status("The relation coverage is still being computed; the analysis runs now and rules without relation evidence stay UNKNOWN.");
  };
  const impact = async (judgeIds: string[] = []) => {
    const t = target();
    if (t && judgeIds.length === 0) await ensureRelations(t);
    let cx: Rec | null = null;
    if (t) {
      try { cx = await api<Rec>("GET", `/v2/change/impact-context?workspace_id=${encodeURIComponent(ws)}&target_candidate_id=${encodeURIComponent(t)}`); }
      catch (e) {
        if (!(e instanceof ApiError) || e.status !== 404) { notify.fail(e); return; }
        notify.status("No extracted rule matches this request, so the impact analysis runs without a target rule: related rules stay UNKNOWN.");
      }
    }
    const id = newId("ui-impact");
    const payload = cx
      ? { candidate_ids: cx.candidate_ids, target_candidate_id: t, relation_job_ids: cx.relation_job_ids, existing_finding_ids: cx.existing_finding_ids, expected_decision_epoch: cx.expected_decision_epoch, context_truncated: cx.context_truncated, ...(judgeIds.length ? { judge_job_ids: judgeIds } : {}) }
      : { candidate_ids: [], existing_finding_ids: [] };
    if (await create("impact", id, payload)) { setIds({ impact: id, simulation: "", proposal: "", review: "", approval: "", apply: "" }); await refresh(); }
  };
  /** The AI looks at the files that still hold UNKNOWN rules: at most 3 files, one call each, one after the other; what it finds is kept apart from the deterministic evidence. */
  const investigate = async () => {
    const t = target();
    const res0 = (d.impact?.execution_result ?? {}) as Rec;
    if (!t || !isDone(d.impact)) return;
    let cx: Rec;
    try { cx = await api<Rec>("GET", `/v2/change/impact-context?workspace_id=${encodeURIComponent(ws)}&target_candidate_id=${encodeURIComponent(t)}`); } catch (e) { notify.fail(e); return; }
    const labels = (cx.candidates ?? {}) as Record<string, { logical_path?: string }>;
    const targetPath = labels[t]?.logical_path;
    const count = new Map<string, number>();
    const flagged = new Set(((res0.indirect ?? []) as Rec[]).map((x) => x.logical_path as string));          // a file the deterministic scan already flagged needs no AI judgement (the server would turn it away)
    for (const x of (res0.unknown ?? []) as Rec[]) { const lp = labels[x.candidate_id]?.logical_path; if (lp && lp !== targetPath && !flagged.has(lp)) count.set(lp, (count.get(lp) ?? 0) + 1); }
    const files = [...count.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 3).map(([lp]) => lp);
    const judged: string[] = [];
    const notes: string[] = [];
    if (files.length === 0) { setInvNote("No other file holds an UNKNOWN rule of this change, so there is nothing to look at."); return; }
    for (let i = 0; i < files.length; i++) {
      const aid = (d.arts ?? []).find((a) => a.logical_path === files[i])?.artifact_id as string | undefined;
      if (!aid) { notes.push(`${basename(files[i])}: the file is not in the current revision's list`); continue; }
      setInv({ step: i + 1, total: files.length, label: basename(files[i]) });
      const id = newId("ui-judge");
      const made = await create("impact.judge", id, { target_candidate_id: t, scenario_candidate_ids: cx.candidate_ids, artifact_id: aid, scope_artifact_ids: [aid] });
      if (!made) { notes.push(`${basename(files[i])}: not started`); continue; }
      let end = "";
      for (let k = 0; k < 75 && !end; k++) {             // about 2.5 minutes at most per file; a provider that is busy is tried again by the server
        await new Promise((r) => setTimeout(r, 2000));
        try { const j = await api<Rec>("GET", `/v2/jobs/${encodeURIComponent(id)}?workspace_id=${encodeURIComponent(ws)}`); if (["COMPLETED", "FAILED", "CANCELLED"].includes(j.execution_status)) end = j.execution_status; } catch { break; }
      }
      if (end === "COMPLETED") judged.push(id); else notes.push(`${basename(files[i])}: ${end || "still running"}`);
    }
    setInv(null);
    setInvNote(notes.length ? `Not all files were investigated (${notes.join("; ")}).` : "");
    if (judged.length) await impact(judged);
    else notify.status("Nothing was added to the impact analysis.");
  };
  const simulate = async () => {
    const id = newId("ui-simulation");
    // the server builds the simulation inputs from the stored draft: nothing the browser sends can change what is tested
    const payload = d.draft?.job_id ? { adapter: "harbor_cli", from_draft_job_id: d.draft.job_id } : {};
    if (await create("simulation", id, payload)) { setIds({ simulation: id, proposal: "", review: "", approval: "", apply: "" }); await refresh(); }
  };
  const simDone = isDone(d.simulation);
  const sim = (d.simulation?.execution_result ?? {}) as Rec;
  const cov = (sim.coverage ?? {}) as Rec;
  const imp = (d.impact?.execution_result ?? {}) as Rec;
  const hs = sim.harbor as Rec | undefined;
  const anaDone = isDone(d.impact) && simDone && !!d.proposal;
  const anaSummary = (
    <>Impact: {(imp.direct ?? []).length} direct, {(imp.indirect ?? []).length} indirect, {(imp.unknown ?? []).length} unknown (unknown is not the same as unaffected). Simulation: {hs ? `${hs.changed_count} of ${hs.transactions} sample cases change their outcome` : "not available - this is not a no-impact result"}.</>
  );
  return (
    <StageCard title={stageTitle("analysis")} testid="flow-analysis" done={anaDone} summary={anaSummary}>
      {rel && (
        <p className="text-sm" data-testid="relation-coverage" data-jobs={rel.jobs}>
          Relation coverage (no AI call): {rel.jobs} relation job{rel.jobs === 1 ? "" : "s"} done; {rel.total - rel.uncovered} of {rel.total} rules have relation evidence{rel.uncovered ? `, ${rel.uncovered} do not (they stay UNKNOWN, never "unaffected")` : ""}.
        </p>
      )}
      <JobLine name="impact" job={d.impact} />
      {mayRun && (
        <div className="flex flex-wrap gap-2">
          <Button data-testid="action-run-relations" data-read={!relTarget || relReadFor === `${ws}|${relTarget}` ? "done" : "pending"} title={(rel?.jobs ?? 0) > 0 ? `Already computed for this revision (${rel!.jobs} job(s)): computing it again gives the same result.` : undefined} disabled={!confirmed || !target() || ambiguous || pending || (rel?.jobs ?? 0) > 0 || (!!relTarget && relReadFor !== `${ws}|${relTarget}`)} onClick={relations}>Compute relation coverage (no AI call)</Button>
          <Button variant="primary" data-testid="action-run-impact" title={!confirmed ? `Confirm the request first (stage ${stageNo("request")}).` : ambiguous ? "Pick which extracted rule this change is about." : d.impact && !isTerminal(d.impact) ? "An impact analysis is already running." : undefined} disabled={!confirmed || ambiguous || (!!d.impact && !isTerminal(d.impact)) || pending} onClick={() => void impact()}>Run impact analysis</Button>
        </div>
      )}
      {mayRun && targetOptions.length > 1 && (
        <label className="text-sm block" data-testid="impact-target-picker">Several extracted rules match this change. Choose the one to analyse:{" "}
          <select value={picked} onChange={(e) => setPicked(e.target.value)} className="border rounded px-2 py-1">
            <option value="">Select a rule</option>
            {targetOptions.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select></label>
      )}
      {isDone(d.impact) && <ImpactReport res={d.impact!.execution_result ?? {}} relationJobs={rel?.jobs ?? 0} />}
      {isDone(d.impact) && ((d.impact!.execution_result?.unknown ?? []) as Rec[]).length > 0 && !d.simulation && s.can("ai.use") && mayRun && (
        <div data-testid="investigate-unknown" className="rounded-md border border-border p-3 grid gap-2 text-sm">
          <h3 className="font-semibold">Look at the UNKNOWN rules with the AI (optional, limited)</h3>
          <Muted>The AI reads up to <strong>3 files</strong> that still hold UNKNOWN rules, one call each, one after the other, and says whether each file depends on this change, with a quote the server re-checks. Its answer is shown apart ("AI judgement, re-checked by the server"): it is never a proven dependency, and a rule it does not flag stays UNKNOWN. Running this repeats the impact analysis, so do it before the simulation.</Muted>
          {inv && <Callout tone="wait" testid="investigate-running" role="status"><span>Looking at file {inv.step} of {inv.total}: <strong>{inv.label}</strong> (waiting for the AI)...</span></Callout>}
          {invNote && <Muted testid="investigate-note">{invNote}</Muted>}
          <div><Button data-testid="action-investigate-unknown" title={aiWhy(s, "impact.judge")} disabled={pending || !s.aiKind("impact.judge") || !!inv} onClick={() => void investigate()}>Ask the AI to look at up to 3 files (uses the paid model)</Button></div>
          {((((d.impact!.execution_result ?? {}) as Rec).coverage?.model_judgments?.requested ?? 0) > 0) ? <Muted testid="investigate-intake">Last investigation: {JSON.stringify(((d.impact!.execution_result ?? {}) as Rec).coverage?.model_judgments)}</Muted> : null}
        </div>
      )}
      <JobLine name="simulation" job={d.simulation} />
      {mayRun && (
        <div><Button variant="primary" data-testid="action-run-simulation" title={!isDone(d.impact) ? "Run the impact analysis first." : d.simulation && !isTerminal(d.simulation) ? "A simulation is already running." : undefined} disabled={!isDone(d.impact) || (!!d.simulation && !isTerminal(d.simulation)) || pending} onClick={simulate}>Run simulation</Button></div>
      )}
      {simDone && (
        <>
          <p data-testid="sim-denominator" className="text-sm">Cases submitted: {cov.submitted === undefined ? "not reported" : cov.submitted}<Tech> {JSON.stringify(cov)}</Tech></p>
          {(cov.submitted === undefined || cov.submitted === 0) && <Callout tone="warn" testid="sim-unavailable">Simulation UNAVAILABLE for this change: no behaviour data or adapter is connected, so nothing was simulated. This is NOT a "no impact" result.</Callout>}
          <p data-testid="sim-mode" className="text-sm">Mode: {humanize(sim.status ?? "unknown")}</p>
          <p data-testid="sim-limits" className="text-sm">Limits: {sim.reason ? p(sim.reason) : "none reported"}</p>
          {sim.status === "NOT_EXECUTABLE" && <Callout tone="warn" testid="sim-not-run">Simulation NOT RUN: {humanize(sim.reason ?? "no supported edit")}. This is not a "no impact" result.</Callout>}
          {sim.status === "STALE_BASELINE" && <Callout tone="warn" testid="sim-stale">The configuration changed since this draft was made ({p(sim.reason)}). Make a new draft; this result cannot support an approval.</Callout>}
          {sim.status === "PARTIAL" && <Callout tone="warn" testid="sim-partial">Only part of this change was simulated: {p(sim.reason)}.</Callout>}
          {(sim.untested_edits ?? []).length > 0 && (
            <div data-testid="sim-untested"><h4 className="font-semibold">Edits in the draft that were NOT simulated</h4>
              <ul className="list-disc pl-5 text-sm">{(sim.untested_edits as Rec[]).map((u, i) => <li key={i}>{u.logical_path ?? "manual item"}{u.path ? ` · ${u.path}` : ""}: no supported simulation for this edit</li>)}</ul></div>
          )}
          {sim.direction && <p className="text-sm" data-testid="sim-direction">Direction tested: {sim.direction === "forward_requested_change" ? "the requested change (baseline → proposed)" : p(sim.direction)}.</p>}
          {(sim.families ?? []).length > 1 && (sim.families as Rec[]).map((f, i) => f.harbor && f.case !== sim.harbor?.case ? <div key={i} data-testid={`sim-family-${f.case}`}><h4 className="font-semibold">Also simulated: {f.case} rules</h4><SimulationReport h={f.harbor} /></div> : null)}
          {sim.harbor && ["EXECUTED", "PARTIAL"].includes(sim.status) && <SimulationReport h={sim.harbor} />}
          {sim.harbor && ["EXECUTED", "PARTIAL"].includes(sim.status) && d.simulation?.job_id && <SimulationAnalysisCard ws={ws} simulationJobId={d.simulation.job_id} impactJobId={d.impact?.job_id} />}
        </>
      )}
    </StageCard>
  );
}
