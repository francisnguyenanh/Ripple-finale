// History: what happened, who did it, every version of the sources, a before/after comparison and
// the guarded restore (preview -> approval by one person -> apply by a DIFFERENT restore operator).
// A decision appears here but changes no file; only an applied change or a restore makes a new version.
import * as React from "react";
import { ArrowLeftRight, Clock, FilePlus2, FileX2, FileDiff } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Denied, Callout, Card, Field, Fingerprint, Muted, StatePill, Tech, TextArea, TextInput, Select } from "@/components/bits";
import { actorsOf, filterEvents } from "./timelineFilter";
import { StatusBadge } from "@/components/StatusBadge";
import { basename, newId } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { ChangeResult } from "@/features/changes/ApplyStage";
import { FlowProvider, useFlow } from "@/features/changes/flow";
import type { Rec } from "@/features/changes/types";
import { replaceHash, hashFor } from "@/lib/route";

const q = encodeURIComponent;
const RESERVED = ["approval"];

const ACT: Record<string, string> = {
  SOURCE_IMPORT_PREPARED: "prepared an import of files", SOURCE_PUBLISHED: "made a new version of the files current", INTENT_PARSED: "saved a change request",
  INTENT_CONFIRMED: "confirmed a change request", DECISION_RECORDED: "recorded a decision (no file was changed)", SANDBOX_LEASE_ACQUIRED: "started a demo session",
  SANDBOX_LEASE_RELEASED: "ended the demo session", SANDBOX_LEASE_RENEWED: "extended the demo session", RESTORE_APPROVED: "approved a restore to an earlier version",
  HITL_ANSWER_RECORDED: "wrote a comment or answer (not a decision)", "job.approved": "allowed a job to run",
  CHAT_AI_ALLOWED: "allowed the AI to reply in a conversation", CHAT_AI_REVOKED: "withdrew the AI permission", CHAT_UNDERSTANDING_CONFIRMED: "confirmed what the AI understood",
  CHAT_REOPENED: "reopened a confirmed request to change it", CHAT_REQUEST_FLAGGED: "wrote a message that tried to claim rights or skip a control (nothing was granted)",
  CHAT_AI_ALLOWED_BY_POLICY: "let the AI reply in a conversation under the workspace AI policy", CHAT_PREVIEW_CONFIRMED: "confirmed a preview in a conversation",
  CHAT_PREVIEW_ACTION_DONE: "recorded the action of a confirmed preview", NOTIFICATION_DISPATCHED: "sent a notification",
  NOTIFICATION_INTENT_STALE: "dropped a notification that was no longer actionable", NOTIFICATION_TOKEN_REGISTERED: "registered a device for notifications",
  NOTIFICATION_TOKEN_REVOKED: "removed a device from notifications",
};
// An event without a wording is named neutrally in the Business view; its code stays in the Technical view (never shown as prose).
const UNNAMED = "recorded an event";
const TRIGGER: Record<string, string> = { import: "created by an import", apply: "created by an approved change", restore: "created by a restore", refresh: "created by a refresh" };

const TIMELINE_LIMIT = 40;
const eventLabel = (e: Rec) => ACT[e.event_type] ?? UNNAMED;

function Timeline({ events, onRetry }: { events: Rec[] | null; onRetry: () => void }) {
  // `null` = the activity list could not be read. That is never shown as "nothing has happened".
  const [actor, setActor] = React.useState("");
  const [text, setText] = React.useState("");
  const all = (events ?? []).filter((e) => e.event_type !== "JOB_QUEUED" && e.event_type !== "membership.grant");
  const windowed = all.slice(-TIMELINE_LIMIT);
  const ev = filterEvents(windowed, { actor, text }, eventLabel).reverse();
  const filtering = !!actor || !!text.trim();
  return (
    <Card title="What happened, and who did it" testid="history-activity">
      <Muted>Most recent first. A decision is recorded here but changes no file; only an applied change makes a new version of the files.</Muted>
      {events !== null && all.length > TIMELINE_LIMIT && <Muted testid="history-window">Showing the latest {TIMELINE_LIMIT} of the {all.length} events loaded. Older events exist but are not listed on this page; this is not a full audit search.</Muted>}
      {events !== null && windowed.length > 8 && (
        <div className="grid gap-2 md:grid-cols-2" data-testid="history-filter">
          <Field label="Person" hint="Only people who appear in the events listed below."><Select data-testid="history-filter-actor" value={actor} onChange={(e) => setActor(e.target.value)}>
            <option value="">Everyone</option>{actorsOf(windowed).map((a) => <option key={a} value={a}>{a}</option>)}</Select></Field>
          <Field label="What happened" hint="Words of the event; nothing is requested."><TextInput data-testid="history-filter-text" value={text} onChange={(e) => setText(e.target.value)} /></Field>
        </div>
      )}
      {filtering && <Muted testid="history-filter-count">Showing {ev.length} of the {windowed.length} events listed here. Filtering never searches events that were not loaded.</Muted>}
      {events === null ? (
        <Callout tone="warn" testid="history-unavailable" role="status">
          <span>The activity list could not be read, so nothing is shown here. This is not the same as nothing having happened.</span>
          <div><Button onClick={onRetry} data-testid="history-retry">Try again</Button></div>
        </Callout>
      ) : ev.length ? (
        <ol className="grid gap-3 border-l border-border pl-4">
          {ev.map((e, i) => (
            <li key={i} data-event={e.event_type} className="text-sm relative">
              <Clock size={12} aria-hidden="true" className="absolute -left-[1.45rem] top-1 bg-surface text-muted" />
              <strong>{e.actor_id || "the system"}</strong> {eventLabel(e)}{" "}
              <span className="text-muted">{e.created_at ? new Date(e.created_at).toLocaleString() : ""}</span>
              <Tech><span className="block text-xs text-muted break-all">{e.event_type} {JSON.stringify(e.payload ?? {}).slice(0, 300)}</span></Tech>
            </li>
          ))}
        </ol>
      ) : filtering ? <Muted testid="history-filter-none">No listed event matches the filter.</Muted> : <Muted>Nothing has happened yet.</Muted>}
    </Card>
  );
}

function Compare({ ws, revs }: { ws: string; revs: Rec[] }) {
  const notify = useNotify();
  const [from, setFrom] = React.useState("");
  const [to, setTo] = React.useState("");
  const [diff, setDiff] = React.useState<Rec | null>(null);
  const [names, setNames] = React.useState<Record<string, string>>({});
  React.useEffect(() => {
    setDiff(null);
    if (!from || !to || from === to) return;
    let live = true;
    (async () => {
      try {
        const d = await api<Rec>("GET", `/v2/history/${q(ws)}/diff?from_revision_id=${q(from)}&to_revision_id=${q(to)}`);
        const map: Record<string, string> = {};
        for (const r of [from, to]) {
          try { for (const a of ((await api<Rec>("GET", `/v2/sources/revisions/${q(r)}?workspace_id=${q(ws)}`)).artifacts ?? []) as Rec[]) map[a.artifact_id] = a.logical_path; } catch { /* names are a convenience */ }
        }
        if (live) { setDiff(d); setNames(map); }
      } catch (e) { notify.fail(e); }
    })();
    return () => { live = false; };
  }, [from, to, ws]); // eslint-disable-line react-hooks/exhaustive-deps
  if (revs.length < 2)
    return <Card title="Compare versions" testid="history-no-compare"><Muted>There is only one version of the files so far, so there is nothing to compare yet. After a change is applied, the before and after appear here.</Muted></Card>;
  const sel = (id: string, v: string, set: (x: string) => void, testid: string) => (
    <Select id={id} data-testid={testid} value={v} onChange={(e) => set(e.target.value)}>
      <option value="">Select a version</option>
      {revs.map((r) => <option key={r.revision_id} value={r.revision_id}>{`Version ${r.sequence} - ${r.revision_id}`}</option>)}
    </Select>
  );
  const KIND = { added: { icon: FilePlus2, text: "added" }, removed: { icon: FileX2, text: "removed" }, changed: { icon: FileDiff, text: "changed" } } as const;
  return (
    <Card title="Compare versions">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Compare from">{sel("hist-from", from, setFrom, "history-diff-from")}</Field>
        <Field label="Compare to">{sel("hist-to", to, setTo, "history-diff-to")}</Field>
      </div>
      <div data-testid="history-diff" className="grid gap-1">
        {diff ? (
          <>
            {(["added", "removed", "changed"] as const).flatMap((kind) => ((diff[kind] ?? []) as Rec[]).map((it) => {
              const K = KIND[kind];
              return (
                <div key={kind + it.artifact_id} data-testid={`diff-${kind}`} data-artifact-id={it.artifact_id} className="text-sm flex items-center gap-2">
                  <K.icon size={14} aria-hidden="true" /> {K.text}: <strong>{basename(names[it.artifact_id] ?? it.artifact_id)}</strong>
                  <Tech><code>{it.artifact_id}</code></Tech>
                </div>
              );
            }))}
            <Muted>Unchanged files: {diff.unchanged_count}{diff.truncated ? " (list truncated)" : ""}</Muted>
          </>
        ) : <Muted>Choose two different versions to see what changed.</Muted>}
      </div>
    </Card>
  );
}

function RestoreCard({ ws, preview, approved, onPreview, onChanged }: { ws: string; preview: Rec | null; approved: Rec | null; onPreview: (target: string) => void; onChanged: () => Promise<void> }) {
  const s = useSession();
  const { ids, setIds } = useFlow();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [reason, setReason] = React.useState("");
  void onPreview;
  if (!preview && !approved) return null;
  return (
    <Card title="Restore" testid="flow-restore">
      {preview && !approved && (
        <>
          <Callout><span>Restoring brings the files back to an earlier version. Nothing changes until a different person applies it.</span></Callout>
          <p className="text-sm">Current version: <code data-testid="restore-current">{preview.current.source_revision_id}</code></p>
          <p className="text-sm">Target version: <code data-testid="restore-target">{preview.target.source_revision_id}</code></p>
          <p className="text-sm break-all">Preview fingerprint: {preview.preview_digest ? <Fingerprint value={preview.preview_digest} testid="restore-digest" /> : <code data-testid="restore-digest">none (nothing to restore)</code>}</p>
          <h3 className="font-semibold">What restoring changes</h3>
          <div data-testid="restore-delta" className="grid gap-1">
            {(["added", "removed", "changed"] as const).flatMap((kind) => (((preview.delta ?? {})[kind] ?? []) as Rec[]).map((it) => (
              <div key={kind + it.artifact_id} data-testid="delta-item" data-kind={kind} data-artifact-id={it.artifact_id} className="text-sm">{kind} <code>{it.artifact_id}</code></div>
            )))}
          </div>
          <Muted>Unchanged files: {preview.delta?.unchanged_count ?? 0}</Muted>
          {((preview.affected_decision_ids ?? []) as string[]).map((id) => <Muted key={id} testid="affected-decision-item">Decision kept in the ledger: {id}</Muted>)}
          {((preview.stale_knowledge_revision_ids ?? []) as string[]).map((id) => <Muted key={id} testid="stale-km-item">Knowledge version that will be out of date: {id}</Muted>)}
          <Callout tone="danger" testid="restore-loss-warning">{preview.loss_warning || "Restoring can discard work; review the changes above."}</Callout>
          <p className="text-sm">Status: <StatePill state="PREVIEWED" label="Previewed - not approved" testid="restore-status" /></p>
          {s.can("source.restore") && !preview.noop && (
            <div className="grid gap-2">
              <Field label="Reason for the restore"><TextArea data-testid="field-restore-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
              <div>
                <Button variant="primary" data-testid="action-restore-approve" disabled={pending} onClick={async () => {
                  const text = reason.trim(); if (!text) { notify.status("A reason is required to approve a restore."); return; }
                  const r = await run(() => api<Rec>("POST", "/v2/restore/approve", { workspace_id: ws, target_revision_id: preview.target.source_revision_id, expected_preview_digest: preview.preview_digest, reason: text, idempotency_key: newId("rst") }));
                  if (!r) return;
                  setReason("");
                  const loc = s.route;
                  replaceHash(hashFor(ws, "history", loc.sub, { ...loc.params, restore: preview.target.source_revision_id, approval: r.restore_approval_id }));
                  notify.status("Restore approved. A different restore operator applies it from this link.");
                  await onChanged();
                }}>Approve restore</Button>
              </div>
            </div>
          )}
        </>
      )}
      {approved && (
        <>
          <p className="text-sm">Target version: <code data-testid="restore-target">{approved.target}</code></p>
          <Muted>Approved by {approved.actor}.<Tech> approval {approved.id} (fingerprint {approved.digest})</Tech></Muted>
          <p className="text-sm">Status: <StatePill state="APPROVED" label="Approved - waiting to be applied" testid="restore-status" /></p>
          {s.can("source.restore") && !ids.apply && (
            <div>
              <Button variant="primary" data-testid="action-restore-apply" disabled={pending} onClick={async () => {
                const id = newId("ui-restore");
                const r = await run(() => api("POST", "/v2/jobs", { workspace_id: ws, kind: "apply", job_id: id, payload: { restore_approval_id: approved.id, expected_approval_digest: approved.digest } }), { stale: true });
                if (r) { setIds({ apply: id }); await onChanged(); }
              }}>Apply restore</Button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

function Body() {
  const s = useSession();
  const ws = s.route.ws!;
  const { data, ids, refresh: flowRefresh } = useFlow();
  const [hist, setHist] = React.useState<Rec | null>(null);
  const [activity, setActivity] = React.useState<Rec[] | null>([]);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [preview, setPreview] = React.useState<Rec | null>(null);
  const [approved, setApproved] = React.useState<Rec | null>(null);
  const { run } = useAction();
  const asked = React.useRef("");
  const approvalId = s.route.params.approval ?? "";
  const restoreId = s.route.params.restore ?? "";
  const approvalRef = React.useRef(approvalId);
  approvalRef.current = approvalId;

  const load = React.useCallback(async () => {
    try {
      const h = await api<Rec>("GET", `/v2/history/${q(ws)}`);
      const act = await api<{ events?: Rec[] }>("GET", `/v2/audit/${q(ws)}`).then((a) => a.events ?? []).catch((): Rec[] | null => null);
      setHist(h); setActivity(act); setErr(null);
      const aid = approvalRef.current;      // read at completion time: an older load must not overwrite a newer one
      if (aid) {
        const ev = (act ?? []).filter((e) => e.event_type === "RESTORE_APPROVED" && e.payload?.restore_approval_id === aid).pop();
        setApproved(ev ? { id: aid, digest: ev.payload.approval_digest, target: ev.payload.target_revision_id, actor: ev.actor_id } : null);
      } else setApproved(null);
    } catch (e) { if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws]);
  React.useEffect(() => { void load(); }, [load, approvalId]);

  const doPreview = React.useCallback(async (target: string) => {
    const r = await run(() => api<Rec>("POST", "/v2/restore/preview", { workspace_id: ws, target_revision_id: target }));
    if (!r) return;
    setPreview(r); setApproved(null);
    const loc = s.route;
    replaceHash(hashFor(ws, "history", loc.sub, { ...loc.params, restore: target, approval: undefined }));
  }, [run, ws, s.route]);
  React.useEffect(() => {
    if (restoreId && !approvalId && !preview && s.can("source.restore") && asked.current !== restoreId) { asked.current = restoreId; void doPreview(restoreId); }
  }, [restoreId, approvalId, preview, s, doPreview]);

  if (err && (err.status === 403 || err.status === 404)) return <Denied err={err} />;
  if (err) return <Callout tone="danger" testid={err.status === 403 || err.status === 404 ? "link-unavailable" : "state-error"}>{err.status === 403 || err.status === 404 ? "This page is not available to your account. Nothing was changed and nothing from that workspace is shown." : "The server could not complete the request."} <Button onClick={() => void load()}>Try again</Button></Callout>;
  if (!hist) return <StatusBadge status="loading" />;
  const revs = ((hist.revisions ?? []) as Rec[]).slice().reverse();
  const onChanged = async () => { await load(); await flowRefresh(); await s.refresh(); };
  return (
    <div className="grid gap-4">
      <Timeline events={activity} onRetry={() => void load()} />
      <section aria-labelledby="h-rev" className="grid gap-3">
        <h2 id="h-rev" className="text-lg font-semibold">Versions of the files</h2>
        {revs.length === 0 && <div data-testid="state-empty" className="rounded-lg border border-border bg-surface p-4">This workspace has no versions yet.</div>}
        {revs.map((rev) => (
          <div key={rev.revision_id} data-testid="revision-row" data-revision-id={rev.revision_id} data-trigger={rev.trigger || "None"} data-current={String(!!rev.is_current)} data-restored-from={rev.restored_from || ""}
            className="rounded-lg border border-border bg-surface p-4 flex flex-wrap items-center gap-3">
            <strong>Version {rev.sequence}</strong>
            <span className="text-sm">{TRIGGER[rev.trigger] ?? "created"}</span>
            {rev.is_current && <span className="rounded-full border border-border px-2 text-xs">current</span>}
            {rev.restored_from && <span className="text-sm text-muted">restored from an earlier version</span>}
            <span className="text-sm text-muted">{rev.created_at ? new Date(rev.created_at).toLocaleString() : ""}</span>
            <Tech><code className="text-xs">{rev.revision_id}{rev.restored_from ? ` restored from ${rev.restored_from}` : ""} [{rev.trigger}]</code></Tech>
            {s.can("source.restore") && !rev.is_current && <Button data-testid="action-restore-preview" onClick={() => void doPreview(rev.revision_id)}><ArrowLeftRight size={14} aria-hidden="true" />Preview restore</Button>}
          </div>
        ))}
      </section>
      <Compare ws={ws} revs={(hist.revisions ?? []) as Rec[]} />
      <RestoreCard ws={ws} preview={preview} approved={approved} onPreview={doPreview} onChanged={onChanged} />
      {data?.change ? <ChangeResult cs={data.change} /> : ids.apply ? <Muted testid="state-outcome">The state of this restore has not been read from the server yet. Nothing is shown as finished.</Muted> : null}
    </div>
  );
}

export function HistoryPage() {
  const s = useSession();
  const ws = s.route.ws!;
  return (
    <>
      <PageHeader id="history" next="Choose a version to compare, or preview a restore." />
      <FlowProvider ws={ws} page="history" reserved={RESERVED} key={ws}><Body /></FlowProvider>
    </>
  );
}
