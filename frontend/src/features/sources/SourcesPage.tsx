// Sources: the documents, code and workbooks the knowledge is built from. Document cards, coverage
// that never hides partial results, an evidence drawer with verbatim source values, an import with
// a manifest and acknowledgement, and a manual refresh with a diff. Every number is read from the server.
import * as React from "react";
import { FileCode2, FileSpreadsheet, FileText, Search } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/dialog";
import { Denied, Callout, Card, Field, HowItWorks, Muted, Name, Path, StatePill, Tech, TechField, TextInput } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { downloadArtifact } from "@/lib/download";
import { basename, humanize, newId } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { hashFor } from "@/lib/route";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { jobState, isTerminal, type Job, type Rec } from "@/features/changes/types";
import { AnalysisButtons, FileStages, LifeSummary, costNote } from "./Lifecycle";
import { useParam } from "@/lib/param";
import { filterFiles } from "./fileFilter";
import { RepoSync } from "./RepoSync";
import { RefreshCorrespondence, type HumanProof } from "./RefreshCorrespondence";

const q = encodeURIComponent;

async function fileEntries(files: File[]): Promise<{ path: string; content_b64: string }[]> {
  const out = [];
  for (const file of files) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    out.push({ path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name, content_b64: btoa(bin) });
  }
  return out;
}

const KIND_WORDS: Record<string, string> = {
  "application/yaml": "Settings file (YAML)", "text/markdown": "Document (Markdown)", "text/x-python": "Program code (Python)",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel workbook",
};
const REFRESH_WORDS: Record<string, string> = { PREPARED: "reviewed, not published yet", COMMITTED: "published", PUBLISHED: "published", NOOP: "no change, nothing published" };
const sizeText = (n: number) => (n >= 1024 ? `${Math.round(n / 102.4) / 10} KB` : `${n} bytes`);
const KindIcon = ({ mt }: { mt: string }) => (mt.includes("spreadsheet") ? <FileSpreadsheet size={18} aria-hidden="true" /> : mt.includes("python") ? <FileCode2 size={18} aria-hidden="true" /> : <FileText size={18} aria-hidden="true" />);

// ---------------------------------------------------------------- analysis job
function JobPanel({ ws, id }: { ws: string; id: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const [job, setJob] = React.useState<Job | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const tick = React.useCallback(async () => {
    try { setJob(await api<Job>("GET", `/v2/jobs/${q(id)}?workspace_id=${q(ws)}`)); setFailed(false); setErr(null); }
    catch (e) { if (e instanceof ApiError) { setFailed(true); setErr(e); } else throw e; }
  }, [ws, id]);
  React.useEffect(() => { void tick(); }, [tick]);
  const state = job ? jobState(job) : "";
  React.useEffect(() => {
    if (!job || isTerminal(job)) return;
    const t = window.setTimeout(() => void tick(), 1500);
    return () => window.clearTimeout(t);
  }, [job, tick]);
  if (!job) return err ? <Callout tone="danger" testid="link-unavailable">This job is not available to your account. Nothing was changed.</Callout> : <StatusBadge status="loading" />;
  const res = (job.execution_result ?? {}) as Rec;
  const cands = Array.isArray(res.candidates) ? res.candidates : null;
  const live = ["QUEUED", "RUNNING", "PENDING", "PENDING_APPROVAL"].includes(state);
  const cost = job.execution_cost_usd;
  return (
    <Card title="Analysis job" testid="job-panel" data={{ "job-id": job.job_id }}>
      <Muted>The AI reads this file and proposes rules with evidence. Nothing is confirmed until a person decides.</Muted>
      <Tech><Muted>kind {job.kind}{job.payload?.artifact_id ? ` - artifact ${job.payload.artifact_id}` : ""}</Muted></Tech>
      {failed && <Callout tone="warn" testid="job-refresh-failed"><span>The latest status could not be read. What is shown may be out of date.</span><div><Button onClick={() => void tick()}>Refresh status</Button></div></Callout>}
      <p className="text-sm">Status: <StatePill state={state} label={state === "COMPLETED" ? "Done" : state === "FAILED" ? "Failed" : state === "CANCELLED" ? "Cancelled" : state === "PENDING_APPROVAL" ? "Waiting for a person to allow this AI run" : "Running"} testid="job-status" /></p>
      <p data-testid="job-cost" className="text-sm">Cost (USD): {cost === null || cost === undefined ? "not recorded yet" : String(cost)}</p>
      {state === "COMPLETED" && cands && <p className="text-sm">{cands.length} candidate{cands.length === 1 ? "" : "s"} extracted{Array.isArray(res.rejected) && res.rejected.length ? `, ${res.rejected.length} rejected` : ""}. Review them in <a className="text-primary underline" href={hashFor(ws, "questions")}>Questions &amp; Decisions</a>.</p>}
      {state === "FAILED" && <Callout tone="danger" testid="job-error">Reason: {job.execution_reason || "the job failed without a recorded reason"}</Callout>}
      {state === "CANCELLED" && <Muted>This job was cancelled and will not produce a result.</Muted>}
      <div className="flex gap-2">
        {s.can("source.import") && live && <Button disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${q(job.job_id)}/cancel`, { workspace_id: ws }), { stale: true })) await tick(); }}>Cancel</Button>}
        {state === "FAILED" && job.retry?.allowed === false && <Muted testid="job-no-attempts-left">No provider attempts left ({job.retry.attempts_used} of {job.retry.attempt_limit} used): this analysis will not run again and nothing more is charged. Start a new analysis of the file instead.</Muted>}
        {s.can("source.import") && state === "FAILED" && job.retry?.allowed !== false && <Button data-testid="action-retry-job" disabled={pending} onClick={async () => { if (await run(() => api("POST", `/v2/jobs/${q(job.job_id)}/retry`, { workspace_id: ws }), { stale: true })) await tick(); }}>Retry</Button>}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- evidence drawer
/** What the parser read from one stored file: its observations, and the verbatim value of the one picked. Shared by the evidence drawer and the selected-file pane
 * (D6); `idp` prefixes the test ids so both can be on a page. Reads only (evidence.read). */
function EvidenceBody({ ws, rev, art, onFail, idp = "" }: { ws: string; rev: string; art: Rec | null; onFail?: () => void; idp?: string }) {
  const notify = useNotify();
  const [obs, setObs] = React.useState<Rec[] | null>(null);
  const [sel, setSel] = React.useState<Rec | null>(null);
  React.useEffect(() => {
    setObs(null); setSel(null);
    if (!art) return;
    let live = true;
    api<{ observations?: Rec[] }>("GET", `/v2/sources/revisions/${q(rev)}/artifacts/${q(art.artifact_id)}/observations?workspace_id=${q(ws)}`)
      .then((r) => { if (live) setObs(r.observations ?? []); }).catch((e) => { notify.fail(e); onFail?.(); });
    return () => { live = false; };
  }, [art, rev, ws]); // eslint-disable-line react-hooks/exhaustive-deps
  const pick = async (o: Rec) => {
    try { setSel(await api<Rec>("GET", `/v2/sources/revisions/${q(rev)}/evidence/${q(o.observation_id)}?workspace_id=${q(ws)}`)); } catch (e) { notify.fail(e); }
  };
  const loc = sel?.locator && typeof sel.locator === "object" ? Object.entries(sel.locator as Rec).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("; ") : JSON.stringify(sel?.locator ?? "");
  return (
    <div data-testid={`${idp}evidence-box`} className="grid gap-3">
      {obs === null && <StatusBadge status="loading" />}
      {obs && !obs.length && <Muted>This file has no observations.</Muted>}
      {obs && obs.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {obs.map((o) => <Button key={o.observation_id} data-testid={`${idp}observation-item`} onClick={() => void pick(o)}>{humanize(o.kind)} <Tech><span> @ {JSON.stringify(o.locator)}</span></Tech></Button>)}
        </div>
      )}
      {sel && art && (
        <section data-testid={`${idp}evidence-panel`} data-provenance="observed" className="rounded-md border border-border p-3 grid gap-2 text-sm">
          <p>File: <code data-testid={`${idp}evidence-file`}>{art.logical_path || art.artifact_id}</code></p>
          <p>Location: <code data-testid={`${idp}evidence-locator`}>{loc}</code></p>
          <p>Source version: <code data-testid={`${idp}evidence-revision`}>{sel.source_revision_id}</code></p>
          <p>Value as written: <code data-testid={`${idp}evidence-value`}>{typeof sel.raw_value === "string" ? sel.raw_value : JSON.stringify(sel.raw_value)}</code></p>
          <Muted>Provenance: observed (read by the parser from the stored file, not inferred).</Muted>
        </section>
      )}
    </div>
  );
}

function EvidenceDrawer({ ws, rev, art, onClose }: { ws: string; rev: string; art: Rec | null; onClose: () => void }) {
  return (
    <Modal open={!!art} onClose={onClose} side="right" title={`Evidence: ${basename(art?.logical_path ?? art?.artifact_id)}`} description="What the parser read from the stored file. These are the original values, not interpretations.">
      <EvidenceBody ws={ws} rev={rev} art={art} onFail={onClose} />
    </Modal>
  );
}

/** D6 (owner, 2026-10-21): the details of ONE selected file beside the unchanged cards. Reading, AI extraction and person confirmation stay separate stages; the
 * paid-action note sits next to the analysis buttons; what the parser read is shown inline for evidence readers (same reads as the drawer). Nothing starts on select. */
function SelectedFile({ ws, rev, a, row, provider, busy, onAnalyze, onClose }: {
  ws: string; rev: string; a: Rec | null; row: Rec | undefined; provider?: Rec; busy: boolean; onAnalyze: (a: Rec) => void; onClose: () => void;
}) {
  const s = useSession();
  const notify = useNotify();
  const head = React.useRef<HTMLHeadingElement>(null);
  React.useEffect(() => { head.current?.focus(); }, [a?.artifact_id]);
  return (
    <aside data-testid="sources-selected" data-artifact-id={a?.artifact_id ?? ""} aria-label="Selected file"
      className="order-first lg:order-none lg:col-start-2 lg:row-start-1 rounded-lg border border-border bg-surface p-4 grid gap-3 min-w-0">
      <div className="flex items-start gap-2">
        <h2 ref={head} tabIndex={-1} className="font-semibold break-all flex-1 flex items-center gap-2">{a ? <><KindIcon mt={a.media_type} /><Path p={a.logical_path} /></> : "Selected file"}</h2>
        <Button size="sm" variant="ghost" data-testid="sources-selected-close" onClick={onClose}>Close</Button>
      </div>
      {!a ? <Muted testid="sources-selected-missing">This file is not in the current version of the sources.</Muted> : (
        <>
          <Muted>{sizeText(a.byte_size)} - {KIND_WORDS[a.media_type] ?? a.media_type}</Muted>
          {row ? <FileStages row={row} provider={provider} /> : <Muted testid="sources-selected-no-stages">The stages of this file could not be read.</Muted>}
          <div className="grid gap-1" data-testid="sources-selected-actions">
            <div className="flex flex-wrap gap-2 items-center">
              {row ? <AnalysisButtons ws={ws} row={row} canRun={s.can("source.import")} busy={busy} onStart={() => onAnalyze(a)} provider={provider} />
                : s.can("source.import") && <Button disabled={s.aiOff} onClick={() => onAnalyze(a)}>Analyze</Button>}
              {s.can("evidence.read") && <a role="link" tabIndex={0} className="text-primary underline cursor-pointer text-sm"
                onClick={() => void downloadArtifact(ws, rev, a, notify.fail)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void downloadArtifact(ws, rev, a, notify.fail); } }}>Export</a>}
            </div>
            {s.can("source.import") && !row && <Muted testid="sources-selected-cost">{costNote(provider)}</Muted>}{/* the analysis buttons state it themselves when the stages are known */}
          </div>
          <div className="grid gap-1">
            <h3 className="text-sm font-medium">What the parser read</h3>
            {s.can("evidence.read") ? <EvidenceBody ws={ws} rev={rev} art={a} idp="sel-" /> : <Muted testid="sources-selected-no-evidence">Reading the file contents needs evidence rights.</Muted>}
          </div>
        </>
      )}
    </aside>
  );
}

// ---------------------------------------------------------------- coverage
function Coverage({ cov }: { cov: Rec | null }) {
  if (!cov) return <p data-testid="coverage-analysis" data-value="none" className="text-sm">Analysis coverage: not available for this version (no import record).</p>;
  return (
    <div className="grid gap-1 text-sm">
      <p data-testid="coverage-admission" data-value={String(cov.admission_complete)}>Files admitted: {cov.admission_complete ? "all (nothing skipped)" : `incomplete (${((cov.skipped ?? []) as Rec[]).length} file(s) skipped)`}</p>
      <p data-testid="coverage-analysis" data-value={cov.status}>Reading (parse): {cov.status}{cov.status === "complete" ? "" : cov.complete ? " - complete" : " - incomplete"}</p>
      <p data-testid="coverage-index" data-value={cov.index_status}>Search index: {cov.index_status}{cov.index_reason ? ` (${cov.index_reason})` : ""}</p>
      <p data-testid="coverage-semantic" data-value={cov.semantic_stage}>AI rule extraction: {humanize(cov.semantic_stage)}</p>
    </div>
  );
}

/** What must never wait behind a disclosure: skipped files and partial coverage. */
function CoverageWarnings({ cov }: { cov: Rec | null }) {
  if (!cov) return null;
  const skipped = (cov.skipped ?? []) as Rec[];
  return (
    <>
      {skipped.length > 0 && <Muted>Skipped: {skipped.map((x) => `${basename(x.logical_path)} (${x.reason})`).join(", ")}</Muted>}
      {(!cov.complete || !cov.admission_complete) && <Callout tone="warn" testid="state-partial">Coverage is partial: not every file was admitted and analysed. Do not read absent results as "nothing found".</Callout>}
    </>
  );
}

// ---------------------------------------------------------------- import
function ImportPanel({ ws, onDone }: { ws: string; onDone: () => Promise<void> }) {
  const notify = useNotify();
  const { run, pending } = useAction();
  const files = React.useRef<HTMLInputElement>(null);
  const [prep, setPrep] = React.useState<Rec | null>(null);
  const [ack, setAck] = React.useState(false);
  const skips = (prep?.acknowledgement_required ?? []) as unknown[];
  return (
    <Card title="Import source files" testid="import-panel">
      <Muted>Files are checked first; nothing becomes current until you commit the manifest below.</Muted>
      <Field label="Upload source files"><input ref={files} type="file" multiple className="text-sm w-full max-w-full min-w-0" /></Field>
      <div>
        <Button disabled={pending} onClick={async () => {
          const list = Array.from(files.current?.files ?? []);
          if (!list.length) { notify.status("Choose one or more files first."); return; }
          const r = await run(async () => api<Rec>("POST", "/v2/sources/imports", { workspace_id: ws, idempotency_key: newId("imp"), files: await fileEntries(list) }));
          if (r) { setPrep(r); setAck(false); notify.status("Manifest ready: review it, then commit the import."); }
        }}>Prepare import</Button>
      </div>
      {prep && (
        <div className="grid gap-2">
          <h3 className="font-semibold">Import manifest</h3>
          {((prep.items ?? []) as Rec[]).map((it) => (
            <div key={it.logical_path} data-testid="import-item" data-logical-path={it.logical_path} data-disposition={it.disposition} className="rounded-md border border-border p-2 text-sm flex flex-wrap items-center gap-2">
              <FileText size={16} aria-hidden="true" />
              <strong><Path p={it.logical_path} /></strong>
              <StatePill state={it.disposition} label={it.disposition === "admitted" ? "Will be added" : "Will be skipped"} />
              <span className="text-muted">{sizeText(it.byte_size)}</span>
              {it.reason && <span>- {it.reason}</span>}
            </div>
          ))}
          {skips.length > 0 && (
            <>
              <Muted>Skipped files will not be part of the new version.</Muted>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="import-ack" checked={ack} onChange={(e) => setAck(e.target.checked)} />I acknowledge the skipped files</label>
            </>
          )}
          <div>
            <Button variant="primary" disabled={(skips.length > 0 && !ack) || pending} onClick={async () => {
              const r = await run(() => api("POST", `/v2/sources/imports/${q(prep.import_id)}/commit`, { workspace_id: ws, expected_version: prep.base_version, acknowledged_skips: skips.length && ack ? skips : [] }), { stale: true });
              if (r) { setPrep(null); setAck(false); if (files.current) files.current.value = ""; notify.status("Import committed."); await onDone(); }
            }}>Commit import</Button>
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- refresh
function RefreshPanel({ ws, onDone }: { ws: string; onDone: () => Promise<void> }) {
  const notify = useNotify();
  const sess = useSession();
  const [proofs, setProofs] = React.useState<HumanProof[]>([]);                  // the person's own rename decisions (SUR-05 adjudication), sent with the next review
  const [corrRefresh, setCorrRefresh] = React.useState<string | null>(null);     // the refresh the proposal was made for (a re-review with a proof keeps it)
  const { run, pending } = useAction();
  const files = React.useRef<HTMLInputElement>(null);
  const [view, setView] = React.useState<Rec | null>(null);
  const [status, setStatus] = React.useState<Rec | null>(null);
  const [committed, setCommitted] = React.useState(false);
  const [ack, setAck] = React.useState(false);
  const getStatus = React.useCallback(async (id: string) => {
    try { setStatus(await api<Rec>("GET", `/v2/sources/refresh/${q(id)}?workspace_id=${q(ws)}`)); } catch (e) { notify.fail(e); }
  }, [ws, notify]);
  const pendingWorker = !!status?.pending_worker;
  React.useEffect(() => {
    if (!committed || !pendingWorker || !view) return;
    const t = window.setTimeout(() => { void getStatus(view.refresh_id); void onDone(); }, 2000);
    return () => window.clearTimeout(t);
  }, [committed, pendingWorker, view, status, getStatus, onDone]);
  const prepare = async (withProofs: HumanProof[]): Promise<Rec | null> => {
    const list = Array.from(files.current?.files ?? []);
    if (!list.length) { notify.status("Choose the whole changed source first."); return null; }
    const r = await run(async () => api<Rec>("POST", "/v2/sources/refresh/prepare", { workspace_id: ws, idempotency_key: newId("ref"), files: await fileEntries(list),
      ...(withProofs.length ? { rename_proofs: withProofs } : {}) }));
    if (r) { setView(r); setStatus(null); setCommitted(false); setAck(false); }
    return r ?? null;
  };
  const d = (view?.diff ?? {}) as Rec;
  const deleted = ((d.deleted ?? []) as Rec[]).map((x) => x.logical_path);
  const row = (kind: string, item: Rec, text: string) => (
    <div key={kind + text} data-testid="refresh-diff" data-kind={kind} data-artifact-id={item.artifact_id ?? ""} className="text-sm flex items-center gap-2">
      <span data-kind={kind} className="rounded-full border border-border px-2 text-xs">{kind}</span> <code>{text}</code>
    </div>
  );
  return (
    <Card title="Update source files" testid="refresh-panel">
      <p className="text-sm">Use this when the real files <strong>changed outside Ripple</strong>. It is not a screen reload.</p>
      <p className="text-sm" data-testid="refresh-consequence">Upload the <strong>whole</strong> current set: a file missing from your upload counts as <strong>deleted</strong> from the new version. Nothing changes until you publish, and publishing approves nothing.</p>
      <HowItWorks testid="refresh-how">
        <ol className="list-decimal pl-5 grid gap-1" data-testid="refresh-steps">
          <li>Upload the <strong>whole</strong> current set of source files.</li>
          <li>Review what was added, changed or deleted.</li>
          <li>Publish the new version on purpose. Nothing changes before this step.</li>
          <li>Ripple then marks what became out of date and shows whether the files need to be analysed again.</li>
        </ol>
        <Muted>A person starts every update; Ripple does not watch a repository.</Muted>
      </HowItWorks>
      <Field label="Changed source files"><input ref={files} id="refresh-files" data-testid="field-refresh-files" type="file" multiple className="text-sm w-full max-w-full min-w-0" /></Field>
      <div>
        <Button data-testid="action-refresh-prepare" disabled={pending} onClick={async () => {
          const r = await prepare([]);
          if (r) { setProofs([]); setCorrRefresh(String(r.refresh_id)); }
        }}>Review the changes</Button>
      </div>
      {view && (
        <div className="grid gap-2">
          <h3 className="font-semibold">What would change</h3>
          {((d.added ?? []) as Rec[]).map((x) => row("added", x, x.logical_path))}
          {((d.changed ?? []) as Rec[]).map((x) => row("changed", x, x.logical_path))}
          {((d.deleted ?? []) as Rec[]).map((x) => row("deleted", x, x.logical_path))}
          {((d.renamed ?? []) as Rec[]).map((x) => row("renamed", x, `${x.from_path} -> ${x.to_path}`))}
          <Muted>Unchanged: {d.unchanged_count ?? 0}{view.noop ? " - no change: nothing would be published." : ""}</Muted>
          {!view.noop && corrRefresh && (
            <RefreshCorrespondence ws={ws} refreshId={corrRefresh} open={!committed && String(view.status ?? "PREPARED") === "PREPARED"} proofs={proofs}
              canRequest={sess.can("source.import") && sess.can("ai.use") && sess.can("evidence.read")} canRead={sess.can("evidence.read")}
              onAdjudicate={async (proof) => {
                const next = [...proofs.filter((x) => !(x.from_path === proof.from_path && x.to_path === proof.to_path)), proof];
                const r = await prepare(next);
                if (!r) throw new Error("The changes could not be reviewed again with your decision; nothing was linked.");
                setProofs(next);
              }} />
          )}
          <p data-testid="refresh-provenance" data-value={view.provenance?.kind ?? "external_observation"} className="text-sm text-muted">{sess.mode === "technical" ? `Provenance: ${view.provenance?.kind ?? "external_observation"}` : "Recorded as a change made outside Ripple"} (an observation, not an approval)</p>
          {!view.noop && (
            <>
              {deleted.length > 0 && <Callout tone="warn" testid="refresh-deleted-warning"><span>{deleted.length} file{deleted.length === 1 ? " is" : "s are"} missing from your upload, so {deleted.length === 1 ? "it" : "they"} will be <strong>deleted from the new version</strong>. If you only meant to update some files, upload the whole source again instead.</span></Callout>}
              {deleted.length > 0 && <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="refresh-ack" checked={ack} onChange={(e) => setAck(e.target.checked)} />I acknowledge the deleted files listed above</label>}
              <div>
                <Button variant="primary" data-testid="action-refresh-commit" disabled={committed || (deleted.length > 0 && !ack) || pending} onClick={async () => {
                  const r = await run(() => api("POST", "/v2/sources/refresh/commit", { workspace_id: ws, refresh_id: view.refresh_id, expected_version: view.base.version, expected_refresh_digest: view.refresh_digest, acknowledged_deletions: deleted.length && ack ? deleted : [] }));
                  if (r) { setCommitted(true); await getStatus(view.refresh_id); await onDone(); }
                }}>Publish the new version</Button>
              </div>
            </>
          )}
          <p data-testid="refresh-status" data-state={status?.status ?? view.status ?? "PREPARED"} className="text-sm">Refresh status: {REFRESH_WORDS[String(status?.status ?? view.status ?? "PREPARED")] ?? humanize(String(status?.status ?? view.status ?? "")).toLowerCase()}</p>
          {status && (
            <div className="grid gap-1">
              <h3 className="font-semibold">What became out of date</h3>
              <Muted>Dependent knowledge and drafts made from the old files are marked out of date (policy: {status.stale_policy}). Re-analysis: {status.reanalysis?.state}{status.pending_worker ? " (still processing)" : ""}. Open the Sources list to see the analysis state of each file.</Muted>
              {((status.stale_reasons ?? []) as Rec[]).map((r, i) => (
                <div key={i}>
                  <div data-testid="stale-reason-item" data-kind={r.kind} data-ref={r.ref} className="text-sm">{humanize(r.kind)}: {r.reason} (next: {r.next_owner})<Tech> <code>{r.ref}</code></Tech></div>
                  {r.kind === "approval_stale" && <div data-testid="stale-approval-item" data-job-id={r.ref} className="text-sm text-warning">An approval prepared earlier is out of date and cannot be approved as shown.</div>}
                </div>
              ))}
              {!((status.stale_reasons ?? []) as Rec[]).length && <Muted>No dependent work was marked out of date.</Muted>}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- page
export function SourcesPage() {
  const s = useSession();
  const notify = useNotify();
  const ws = s.route.ws!;
  const sub = s.route.sub;
  const [view, setView] = React.useState<Rec | null>(null);
  const [life, setLife] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [loaded, setLoaded] = React.useState(false);
  const [art, setArt] = React.useState<Rec | null>(null);
  const polls = React.useRef(0);
  const headRev = s.head?.source_revision_id ?? null;
  const sessionRefresh = s.refresh;
  const { run } = useAction();
  const [starting, setStarting] = React.useState<string | null>(null);
  const [fileFilter, setFileFilter] = React.useState("");
  const [selId, setSelId] = useParam("file");                                     // D6: the selected file lives in the URL (reload / Back keep it)
  React.useEffect(() => { setArt(null); }, [ws]);                                 // another workspace closes the evidence drawer

  const load = React.useCallback(async () => {
    try {
      const head = await api<Rec>("GET", `/v2/workspaces/${q(ws)}/head`);
      void sessionRefresh();
      if (!head.source_revision_id) { setView(null); setLife(null); setLoaded(true); setErr(null); return; }
      const [rev, lf] = await Promise.all([
        api<Rec>("GET", `/v2/sources/revisions/${q(head.source_revision_id)}?workspace_id=${q(ws)}`),
        api<Rec>("GET", `/v2/sources/lifecycle?workspace_id=${q(ws)}`).catch(() => null),
      ]);
      setView(rev); setLife(lf);
      setLoaded(true); setErr(null);
    } catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws, sessionRefresh]);
  React.useEffect(() => { polls.current = 0; void load(); }, [load]);
  const busy = !!view?.coverage && ["pending", "running", "queued", "not_started"].includes(view.coverage.status);
  const states = ((life?.artifacts ?? []) as Rec[]).map((r) => r.extraction?.state as string);
  const working = states.some((x) => x === "queued" || x === "running");
  const waitingForPerson = states.some((x) => x === "pending_approval");
  // Bounded: 2 s while something runs, 6 s (at most 20 looks) while a person has to allow an AI run.
  React.useEffect(() => {
    if (!(busy || working) || polls.current >= 60) return;
    const t = window.setTimeout(() => { polls.current += 1; void load(); }, 2000);
    return () => window.clearTimeout(t);
  }, [busy, working, view, life, load]);
  const slow = React.useRef(0);
  React.useEffect(() => {
    if (busy || working || !waitingForPerson || slow.current >= 20) return;
    const t = window.setTimeout(() => { slow.current += 1; void load(); }, 6000);
    return () => window.clearTimeout(t);
  }, [busy, working, waitingForPerson, life, load]);
  void headRev;

  if (err && (err.status === 403 || err.status === 404)) return (<><PageHeader id="sources" /><Denied err={err} /></>);
  if (err) return (<><PageHeader id="sources" /><Callout tone="danger" testid={err.status === 403 || err.status === 404 ? "link-unavailable" : "state-error"}>{err.status === 403 || err.status === 404 ? "This page is not available to your account. Nothing was changed and nothing from that workspace is shown." : "The server could not complete the request."} <Button onClick={() => void load()}>Try again</Button></Callout></>);
  const allFiles = ((view?.artifacts ?? []) as Rec[]);
  const shownFiles = filterFiles(allFiles, fileFilter);
  const rowOf = (a: Rec): Rec | undefined => ((life?.artifacts ?? []) as Rec[]).find((r) => r.artifact_id === a.artifact_id);
  const analyze = async (a: Rec) => {
    const jobId = newId("ui-extract");
    setStarting(a.artifact_id as string);
    const r = await run(() => api<Rec>("POST", "/v2/jobs", { workspace_id: ws, kind: "extraction", job_id: jobId, payload: { artifact_id: a.artifact_id, source_role: a.source_role || "unknown" } }));
    setStarting(null);
    if (r) location.hash = hashFor(ws, "sources", r.job_id || jobId);
  };
  return (
    <>
      <PageHeader id="sources" next={view ? "Open the Explorer to see what was read, or ask the AI to analyse a file." : "Add your first files with Import source files below."} />
      <div className="grid gap-4">
        {sub && <JobPanel ws={ws} id={sub} key={sub} />}
        {!loaded && <StatusBadge status="loading" />}
        {loaded && !view && <div data-testid="state-empty" className="rounded-lg border border-border bg-surface p-6">No source has been imported into this workspace yet.</div>}
        {view && (
          <>
            {life ? (
              <LifeSummary life={life} paths={((view.artifacts ?? []) as Rec[]).map((a) => String(a.logical_path))} coverage={
                <>
                  <Muted testid="sources-note">Reading a file (parsing) is not the same as the AI extracting rules from it: each file below shows both, separately.</Muted>
                  <Coverage cov={view.coverage ?? null} />
                </>
              } />
            ) : (
              <Card title="Import and reading coverage">
                <Muted testid="sources-note">Reading a file (parsing) is not the same as the AI extracting rules from it: each file below shows both, separately.</Muted>
                <Coverage cov={view.coverage ?? null} />
              </Card>
            )}
            <CoverageWarnings cov={view.coverage ?? null} />
            <Tech><p className="text-sm">Revision: <code data-testid="revision-id">{view.revision_id}</code>{view.trigger ? <span className="text-muted"> ({view.trigger})</span> : null}</p></Tech>
            {!life && (
              <Callout tone="warn" testid="lifecycle-unavailable" role="status">
                <span>The state of each file (reading, AI extraction, confirmation) could not be read, so the files below are listed without their stages. This is not the same as nothing having been analysed.</span>
                <div><Button onClick={() => void load()} data-testid="lifecycle-retry">Try again</Button></div>
              </Callout>
            )}
            {allFiles.length > 8 && (
              <div className="grid gap-1" data-testid="files-filter">
                <Field label="Find a file" hint={`Narrows the ${allFiles.length} files of this version by name or type; nothing is requested.`}>
                  <TextInput data-testid="files-filter-input" value={fileFilter} onChange={(e) => setFileFilter(e.target.value)} />
                </Field>
                <Muted testid="files-filter-count">Showing {shownFiles.length} of {allFiles.length} files.</Muted>
              </div>
            )}
            <div className={selId ? "grid gap-4 lg:grid-cols-[minmax(0,1fr)_26rem] items-start" : ""}>
            {selId && <SelectedFile ws={ws} rev={view.revision_id} a={allFiles.find((x) => x.artifact_id === selId) ?? null} row={(() => { const f = allFiles.find((x) => x.artifact_id === selId); return f ? rowOf(f) : undefined; })()}
              provider={life?.provider as Rec} busy={starting === selId} onAnalyze={(a) => void analyze(a)} onClose={() => setSelId("")} />}
            <div className={selId ? "grid gap-3 lg:col-start-1 lg:row-start-1" : "grid gap-3 md:grid-cols-2"}>{/* owner decision D6, updated 2026-10-21: the file cards stay; a selected-file pane is added beside them */}
              {shownFiles.map((a) => (
                <article key={a.artifact_id} data-testid="artifact-row" data-logical-path={a.logical_path} data-artifact-id={a.artifact_id} data-selected={String(selId === a.artifact_id)}
                  className={`rounded-lg border bg-surface p-4 grid gap-2 min-w-0 ${selId === a.artifact_id ? "border-primary ring-2 ring-primary" : "border-border"}`}>
                  <h3 className="font-semibold flex items-center gap-2 break-all"><KindIcon mt={a.media_type} /><Path p={a.logical_path} /></h3>
                  <Muted>{sizeText(a.byte_size)} - {KIND_WORDS[a.media_type] ?? a.media_type} - folder {String(a.logical_path).includes("/") ? <Name raw={String(a.logical_path).split("/").slice(0, -1).join("/")} /> : "(top)"}</Muted>
                  <Tech><Muted>{a.byte_size} bytes - {a.media_type} - role: {a.source_role}</Muted></Tech>
                  <TechField label="artifact id" value={a.artifact_id} />
                  {rowOf(a) && <FileStages row={rowOf(a)!} provider={life?.provider as Rec} />}
                  <div className="flex flex-wrap gap-2 items-center">
                    {rowOf(a) ? <AnalysisButtons ws={ws} row={rowOf(a)!} canRun={s.can("source.import")} busy={starting === a.artifact_id} onStart={() => void analyze(a)} provider={life?.provider as Rec} />
                      : s.can("source.import") && <Button disabled={s.aiOff} title={s.aiOff ? "AI actions are switched off on this server" : undefined} onClick={() => void analyze(a)}>Analyze</Button>}
                    <Button data-testid="artifact-details" aria-pressed={selId === a.artifact_id} onClick={() => setSelId(selId === a.artifact_id ? "" : String(a.artifact_id))}>{selId === a.artifact_id ? "Hide details" : "Details"}</Button>
                    {s.can("evidence.read") && <Button onClick={() => setArt(a)}><Search size={14} aria-hidden="true" />Evidence</Button>}
                    {s.can("evidence.read") && <a role="link" tabIndex={0} data-testid="export-link-source" className="text-primary underline cursor-pointer text-sm"
                      onClick={() => void downloadArtifact(ws, view.revision_id, a, notify.fail)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void downloadArtifact(ws, view.revision_id, a, notify.fail); } }}>Export</a>}
                  </div>
                </article>
              ))}
            </div>
            </div>
          </>
        )}
        {s.can("source.import") && <ImportPanel ws={ws} onDone={load} />}
        {s.can("source.import") && <RefreshPanel ws={ws} onDone={load} />}
        <RepoSync ws={ws} onSynced={load} />
      </div>
      {view && <EvidenceDrawer ws={ws} rev={view.revision_id} art={art} onClose={() => setArt(null)} />}
    </>
  );
}
