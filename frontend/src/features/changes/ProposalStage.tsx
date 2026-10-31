import { stageNo } from "./registry";
import { stageTitle } from "./registry";
import { StageCard } from "./StageCard";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Tech, TechField, TextInput, usePlain, Select } from "@/components/bits";
import { basename, newId, parseValue } from "@/lib/fmt";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { draftResult } from "./DraftStage";
import { useFlow } from "./flow";
import { JobLine, useCreateStage } from "./StageBits";
import { GuidedPreparation } from "./GuidedPreparation";
import type { Rec } from "./types";
import { isDone } from "./types";

const KINDS: [string, string][] = [["yaml_typed_edit", "YAML value edit"], ["xlsx_typed_edit", "Workbook cell edit"], ["markdown", "Markdown text"], ["python", "Python code"]];

export function DiffView({ diff }: { diff: Rec }) {
  const lines = (diff.lines ?? []) as Rec[];
  return (
    <div data-testid="diff-view" data-verbatim className="rounded-md border border-border overflow-x-auto text-sm font-mono">
      {!lines.length && <p className="p-3 text-muted font-sans">{diff.is_no_op ? "This proposal changes nothing (no-op)." : "No machine-produced text diff: a person must author this change."}</p>}
      {lines.map((l, i) =>
        l.kind === "hunk" ? (
          <div key={i} data-testid="diff-hunk" className="px-3 py-0.5 bg-surface-2 text-muted whitespace-pre">{l.text}</div>
        ) : (
          <div key={i} data-testid="diff-line" data-kind={l.kind}
            className={`px-3 py-0.5 whitespace-pre ${l.kind === "add" ? "bg-success/10" : l.kind === "remove" ? "bg-danger/10" : ""}`}>
            <span aria-hidden="true" className="inline-block w-4 select-none">{l.kind === "add" ? "+" : l.kind === "remove" ? "-" : " "}</span>
            <span className="sr-only">{l.kind === "add" ? "added: " : l.kind === "remove" ? "removed: " : ""}</span>
            {String(l.text).replace(/^[+\- ]/, "")}
          </div>
        ),
      )}
      {diff.truncated && <p className="p-3 text-muted font-sans">The diff is truncated at {lines.length} lines.</p>}
    </div>
  );
}

export function ProposalStage() {
  const { ids, data, setIds, refresh, ws } = useFlow();
  const p = usePlain();
  const s = useSession();
  const notify = useNotify();
  const { create, pending } = useCreateStage();
  const d = data;
  const dr = draftResult(d?.draft ?? null);
  const proposed = ((dr?.edits ?? []) as Rec[]).filter((e) => e.status === "PROPOSED");
  const [chosen, setChosen] = React.useState<Set<string>>(new Set());
  const seen = React.useRef("");
  const sig = proposed.map((e) => e.ref).join("|");
  React.useEffect(() => { if (seen.current !== sig) { seen.current = sig; setChosen(new Set(proposed.map((e) => e.ref))); } }, [sig, proposed]);

  const [kind, setKind] = React.useState("yaml_typed_edit");
  const [art, setArt] = React.useState("");
  const [f, setF] = React.useState({ path: "", sheet: "", cell: "", expected: "", value: "" });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF((p) => ({ ...p, [k]: e.target.value }));
  if (!d) return null;
  const staleDraft = !!d.gaps?.stale;
  const simDone = isDone(d.simulation);
  const pr = isDone(d.proposal) ? (d.proposal!.execution_result ?? {}) : null;
  const sf = (d.intent?.structured_form ?? {}) as Rec;

  const fromDraft = async () => {
    const refs = proposed.filter((e) => chosen.has(e.ref));
    if (!refs.length) { notify.status("Keep at least one proposed edit."); return; }
    const target = refs[0].artifact_id;
    if (refs.some((e) => e.artifact_id !== target)) { notify.status("One proposal covers one file; keep edits of a single file."); return; }
    const id = newId("ui-proposal");
    const r = await create("proposal", id, { intent_id: ids.intent, impact_job_id: ids.impact, simulation_job_id: ids.simulation, target_artifact_id: target, edit_kind: "yaml_typed_edit", draft_job_id: ids.draft, accepted_refs: refs.map((e) => e.ref) });
    if (r) { setIds({ proposal: id, review: "", approval: "", apply: "" }); await refresh(); }
  };
  const manual = async () => {
    if (!art) { notify.status("Select the file first."); return; }
    const payload: Rec = { intent_id: ids.intent, impact_job_id: ids.impact, simulation_job_id: ids.simulation, target_artifact_id: art, edit_kind: kind };
    if (kind === "yaml_typed_edit") payload.edits = [{ path: f.path.trim(), expected_current: parseValue(f.expected), proposed: parseValue(f.value) }];
    if (kind === "xlsx_typed_edit") payload.edits = [{ sheet: f.sheet.trim(), cell: f.cell.trim(), expected_current: parseValue(f.expected), proposed: parseValue(f.value) }];
    const id = newId("ui-proposal");
    const r = await create("proposal", id, payload);
    if (r) { setIds({ proposal: id, review: "", approval: "", apply: "" }); await refresh(); }
  };
  const bind = pr?.draft_binding as Rec | undefined;

  const propDone = !!pr && !!d.review;
  const propSummary = (
    <>Prepared: {proposed.length ? proposed.map((e, i) => <span key={i}>{i ? "; " : ""}{basename(e.logical_path)}: {String(e.proposal_edit.expected_current)} <span aria-hidden="true">→</span><span className="sr-only">to</span> {String(e.proposal_edit.proposed)}{sf.currency ? ` ${sf.currency}` : ""}</span>) : "a proposal"}.
      {bind && (bind.left_for_humans ?? []).length > 0 ? ` ${(bind.left_for_humans as Rec[]).length} item(s) are left for people.` : ""}</>
  );
  return (
    <StageCard title={stageTitle("proposal")} testid="flow-proposal" done={propDone} summary={propSummary}>
      <JobLine name="proposal" job={d.proposal} />
      {ids.intent && !d.proposal && s.can("change.prepare") && dr && !staleDraft && (
        <GuidedPreparation ws={ws} intentId={ids.intent} onReady={(pid) => { setIds({ proposal: pid, review: "", approval: "", apply: "" }); void refresh(); }} />
      )}
      {bind && (bind.left_for_humans ?? []).length > 0 && (
        <div className="grid gap-2" data-testid="left-for-humans-list">
          <h3 className="font-semibold">Left for people (not part of this proposal)</h3>
          <Muted>Open items from the draft and its earlier drafts. None of them is resolved by this proposal; "outside this request" means decided out of scope by the person named, not solved.</Muted>
          {(bind.left_for_humans as Rec[]).map((x, i) => <Callout key={i} tone="warn" testid="left-for-humans">{basename(x.logical_path)} - {p(x.status)}{x.reason ? ` - ${p(x.reason)}` : ""}{x.carried_from ? (x.disposition === "outside_this_request" ? ` [outside this request - NOT resolved, kept from the earlier draft${x.disposition_basis?.by ? `; decided by ${x.disposition_basis.by}` : ""}${x.disposition_basis?.reason ? `; reason: ${x.disposition_basis.reason}` : ""}]` : " [still unresolved - kept from the earlier draft]") : (x.disposition === "potentially_affected" ? " [potentially affected - same rule, a different value; NOT resolved]" : "")}</Callout>)}
        </div>
      )}
      {staleDraft && <Callout tone="warn" testid="proposal-draft-stale">This draft is out of date ({(d.gaps?.stale_reasons ?? []).join("; ")}). Generate a new draft before preparing a proposal from it.</Callout>}
      {s.can("change.prepare") && simDone && proposed.length > 0 && !staleDraft && (
        <div className="grid gap-2">
          <h3 className="font-semibold">Choose which proposed edits go into the proposal</h3>
          <Muted testid="proposal-select-note">Ticking only selects what goes into the proposal. It is <strong>not an approval</strong>: a different person reviews and approves it later (stage {stageNo("approval")}), and nothing is applied before that. Nothing needs to be retyped.</Muted>
          {proposed.map((e) => (
            <label key={e.ref} className="flex items-start gap-2 text-sm">
              <input type="checkbox" aria-label={`Include in the proposal: ${basename(e.logical_path)} ${String(e.proposal_edit.expected_current)} to ${String(e.proposal_edit.proposed)}`} data-testid={`proposal-accept-${e.ref}`} checked={chosen.has(e.ref)} className="mt-1"
                onChange={(ev) => setChosen((p) => { const n = new Set(p); if (ev.target.checked) n.add(e.ref); else n.delete(e.ref); return n; })} />
              <span>{basename(e.logical_path)}: <strong>{String(e.proposal_edit.expected_current)} → {String(e.proposal_edit.proposed)}</strong>{sf.currency ? ` ${sf.currency}` : ""}<TechField label="key" value={e.proposal_edit.path} /></span>
            </label>
          ))}
          <div><Button variant="primary" data-testid="action-prepare-proposal-from-draft" disabled={pending} onClick={fromDraft}>Prepare the proposal with the ticked edits</Button></div>
        </div>
      )}
      {s.can("change.prepare") && simDone && proposed.length === 0 && (
        <div className="grid gap-3">
          <Muted testid="proposal-no-edit">The AI draft did not propose an automatic edit, so there is nothing to select. Describe the change again or ask for a new draft; a person with technical knowledge can prepare the proposal by hand under Advanced.</Muted>
          <details data-testid="proposal-advanced" className="rounded-md border border-border px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">Advanced: prepare the proposal by hand (file, key and values)</summary>
          <div className="grid gap-3 mt-3">
          <Field label="File">
            <Select data-testid="field-proposal-artifact" value={art} onChange={(e) => setArt(e.target.value)}>
              <option value="">Select the file to change</option>
              {(d.arts ?? []).map((a) => <option key={a.artifact_id} value={a.artifact_id}>{a.logical_path}</option>)}
            </Select>
          </Field>
          <Field label="Kind of edit">
            <Select data-testid="field-proposal-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
          </Field>
          {kind === "yaml_typed_edit" && <Field label="Key path (dotted)"><TextInput data-testid="field-proposal-path" value={f.path} onChange={set("path")} /></Field>}
          {kind === "xlsx_typed_edit" && <><Field label="Sheet name"><TextInput data-testid="field-proposal-sheet" value={f.sheet} onChange={set("sheet")} /></Field><Field label="Cell (for example B2)"><TextInput data-testid="field-proposal-cell" value={f.cell} onChange={set("cell")} /></Field></>}
          {(kind === "yaml_typed_edit" || kind === "xlsx_typed_edit") && <>
            <Field label="Expected current value"><TextInput data-testid="field-proposal-expected" value={f.expected} onChange={set("expected")} /></Field>
            <Field label="New value"><TextInput data-testid="field-proposal-value" value={f.value} onChange={set("value")} /></Field></>}
          <div><Button variant="primary" data-testid="action-prepare-proposal" disabled={pending} onClick={manual}>Prepare proposal</Button></div>
          </div>
          </details>
        </div>
      )}
      {pr && (
        <>
          <Muted>This proposal is ready for review.<Tech> classification {String(pr.classification)} - readiness {String(pr.readiness)} - kind {String(pr.edit_kind)}</Tech></Muted>
          <Tech><p className="text-sm break-all">File fingerprint after the change: <code data-testid="diff-after-hash">{pr.after_blob_hash || "unchanged"}</code> <span className="text-muted">(before: {pr.before_blob_hash || "n/a"})</span></p></Tech>
          {d.diff && (
            <>
              <h3 className="font-semibold">Exact change in {basename(d.diff.logical_path || d.diff.target_artifact_id)}</h3>
              <DiffView diff={d.diff} />
            </>
          )}
          {!d.diff && !s.can("evidence.read") && !s.can("change.approve") && (
            <Muted data-testid="diff-withheld">The exact file diff is shown to the approver and to members who may read evidence; the fingerprints above identify the change.</Muted>
          )}
        </>
      )}
    </StageCard>
  );
}
