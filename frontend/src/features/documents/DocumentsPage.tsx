// Knowledge Documents (DOC-04): read the Business or Technical document of the accepted business knowledge, generated lazily on first view (one artifact per
// snapshot x mode x scope), never on import or acceptance. The Markdown is rendered by the safe renderer (no raw HTML, allowlisted link schemes); a document of
// an older knowledge version is labelled stale and only an explicit action generates one for the current version. Selection lives in the URL (reload-safe).
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Muted, StatePill, Tech } from "@/components/bits";
import { Markdown } from "@/lib/markdown";
import { useParam } from "@/lib/param";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const FRESH: Record<string, string> = { available: "Current", stale: "Older version", generating: "Being generated", failed: "Could not be generated", not_requested: "Not generated yet" };

type Topic = { key: string; title: string; scope: Rec | null; authority?: string };

async function readContent(ws: string, aid: string, signal: AbortSignal): Promise<{ text: string; hash: string | null; freshness: string | null; snapshot: string | null }> {
  const resp = await fetch(`/v2/business/documents/${q(aid)}/content?workspace_id=${q(ws)}`, { credentials: "same-origin", headers: { "X-Ripple-Judge": "1" }, signal });
  if (!resp.ok) {
    let body: Record<string, unknown> = {};
    try { body = await resp.json(); } catch { /* not json */ }
    throw new ApiError(resp.status, body);
  }
  return { text: await resp.text(), hash: resp.headers.get("X-Content-Hash"), freshness: resp.headers.get("X-Freshness"), snapshot: resp.headers.get("X-Snapshot-Id") };
}

export function DocumentsPage() {
  const s = useSession();
  const ws = s.route.ws!;
  const mode = s.mode === "technical" ? "technical" : "business";
  const [doc, setDoc] = useParam("doc");
  const [topics, setTopics] = React.useState<Topic[] | null>(null);
  const [status, setStatus] = React.useState<Rec | null>(null);
  const [listed, setListed] = React.useState<Rec[]>([]);
  const [state, setState] = React.useState<Rec | null>(null);
  const [content, setContent] = React.useState<{ text: string; hash: string | null; freshness: string | null; snapshot: string | null } | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [tick, setTick] = React.useState(0);

  React.useEffect(() => {                                                         // topics and the status summary: metadata only, nothing generated
    let live = true;
    Promise.all([api<Rec>("GET", `/v2/business/graph?workspace_id=${q(ws)}`), api<Rec>("GET", `/v2/business/knowledge?workspace_id=${q(ws)}`), api<Rec>("GET", `/v2/business/documents?workspace_id=${q(ws)}`)])
      .then(([g, k, l]) => {
        if (!live) return;
        const rules = ((g.nodes ?? []) as Rec[]).filter((n) => n.type === "rule");
        setTopics([{ key: "all", title: "The whole business knowledge", scope: null }, ...rules.map((r) => ({ key: String(r.id), title: String(r.title), scope: { rule_ids: [String(r.id)] }, authority: String(r.authority) }))]);
        setStatus(k); setListed((l.documents ?? []) as Rec[]); setErr(null);
      })
      .catch((e) => { if (live) setErr(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "This page is not available to your account." : errorText(e)); });
    return () => { live = false; };
  }, [ws, tick]);

  React.useEffect(() => {                                                         // the selected artifact: its state, then its bytes once available; polled while generating
    if (!doc) { setState(null); setContent(null); return; }
    const ctl = new AbortController();
    let live = true;
    let timer: number | undefined;
    const load = async () => {
      try {
        const st = await api<Rec>("GET", `/v2/business/documents/${q(doc)}?workspace_id=${q(ws)}`, undefined, { signal: ctl.signal });
        if (!live) return;
        setState(st); setErr(null);
        if (st.freshness === "available" || st.freshness === "stale") {
          const c = await readContent(ws, doc, ctl.signal);
          if (live) setContent(c);
        } else {
          setContent(null);
          if (st.freshness === "generating") timer = window.setTimeout(() => void load(), 1500);
        }
      } catch (e) {
        if (!live || (e instanceof DOMException && e.name === "AbortError")) return;
        setContent(null);
        setErr(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "This document is not available to your account. Nothing is shown." : errorText(e));
      }
    };
    void load();
    return () => { live = false; ctl.abort(); if (timer) window.clearTimeout(timer); };
  }, [ws, doc, tick]);

  const request = async (topic: Topic) => {
    setBusy(true); setErr(null);
    try {
      const r = await api<Rec>("POST", "/v2/business/documents", { workspace_id: ws, modes: [mode], ...(topic.scope ? { scope: topic.scope } : {}) });
      const d = ((r.documents ?? []) as Rec[])[0];
      if (d) setDoc(String(d.artifact_id));
      setTick((t) => t + 1);
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 403 ? `The ${mode} document needs rights your account does not have. Nothing was generated.` : errorText(e));
    } finally { setBusy(false); }
  };

  const saveBlob = (blob: Blob, name: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.append(a); a.click(); a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 5000);
  };
  const nameFrom = (resp: Response, fallback: string) => /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") ?? "")?.[1] ?? fallback;
  const download = async () => {                                                  // the bytes being viewed (same hash as the preview), never the latest by surprise
    if (!doc) return;
    setBusy(true); setErr(null);
    try {
      const resp = await fetch(`/v2/business/documents/${q(doc)}/download?workspace_id=${q(ws)}`, { credentials: "same-origin", headers: { "X-Ripple-Judge": "1" } });
      if (!resp.ok) throw new ApiError(resp.status, {});
      saveBlob(await resp.blob(), nameFrom(resp, "knowledge.md"));
    } catch (e) { setErr(e instanceof ApiError && e.status === 403 ? "This document is not available to your account." : errorText(e)); }
    finally { setBusy(false); }
  };
  const exportBundle = async () => {                                              // both modes of the version being viewed; waits while a missing one is generated
    if (!state) return;
    setBusy(true); setErr(null);
    const modes = s.can("evidence.read") ? ["business", "technical"] : ["business"];
    const body = { workspace_id: ws, modes, snapshot_id: state.snapshot_id, ...((state.scope as Rec)?.rule_ids ? { scope: state.scope } : {}) };
    try {
      for (let attempt = 0; attempt < 20; attempt += 1) {
        const resp = await fetch("/v2/business/documents/export", { method: "POST", credentials: "same-origin", headers: { "X-Ripple-Judge": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (resp.ok) { saveBlob(await resp.blob(), nameFrom(resp, "knowledge.zip")); setTick((t) => t + 1); return; }
        let data: Record<string, unknown> = {};
        try { data = await resp.json(); } catch { /* not json */ }
        if (resp.status === 409 && data.code === "generating") { await new Promise((r) => window.setTimeout(r, 1500)); continue; }
        throw new ApiError(resp.status, data);
      }
      setErr("The bundle is still being generated; try again in a moment.");
    } catch (e) { setErr(e instanceof ApiError && e.status === 403 ? "The bundle needs rights your account does not have. Nothing was exported." : errorText(e)); }
    finally { setBusy(false); }
  };

  if (err && !topics) return (<><PageHeader id="documents" /><Callout tone="danger" testid="docs-error">{err}</Callout></>);
  if (!topics) return (<><PageHeader id="documents" /><p role="status" className="text-muted">Loading...</p></>);
  const head = (status?.business_head ?? {}) as Rec;
  const summary = (status?.documents ?? {}) as Rec;
  const accepted = !!head.accepted_snapshot_id;
  const currentTopic = state ? topics.find((t) => (t.scope === null && !((state.scope as Rec)?.rule_ids)) || (t.scope && JSON.stringify(t.scope) === JSON.stringify(state.scope))) : null;
  const canRefresh = state?.freshness === "stale" && currentTopic;
  return (
    <>
      <PageHeader id="documents" next={accepted ? "Pick a topic. The document is generated the first time it is opened and kept for that knowledge version." : "Accept a business knowledge version first; documents describe accepted knowledge only."} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="grid gap-4 content-start">
          <Card title="Topics" testid="docs-topics">
            <Muted testid="docs-summary">Knowledge version {String(head.business_revision ?? "-")}. Documents: {FRESH[String(summary.freshness)] ?? String(summary.freshness ?? "-")}. Mode: {mode} (switch with the Business/Technical control).</Muted>
            <ul className="grid gap-1 mt-2">
              {topics.map((t) => (
                <li key={t.key}>
                  <Button data-testid="docs-topic" data-topic={t.key} className="w-full justify-start text-left" disabled={!accepted || busy} onClick={() => void request(t)}>
                    {t.title}{t.authority === "TARGET" ? " (decided by people)" : ""}
                  </Button>
                </li>
              ))}
            </ul>
          </Card>
          {listed.length > 0 && (
            <Card title="Generated so far" testid="docs-list">
              <ul className="grid gap-1 text-sm">
                {listed.map((d) => (
                  <li key={d.artifact_id as string} className="flex flex-wrap items-center gap-2">
                    <button type="button" data-testid="docs-listed" data-freshness={d.freshness as string} className="underline text-left" onClick={() => setDoc(String(d.artifact_id))}>
                      {String(d.mode)} {((d.scope as Rec)?.rule_ids as string[] | undefined)?.join(", ") ?? "whole knowledge"}
                    </button>
                    <StatePill state={String(d.freshness)} label={FRESH[String(d.freshness)] ?? String(d.freshness)} />
                  </li>
                ))}
              </ul>
              {status && (summary.statement as string | undefined) ? <Muted>{String(summary.statement)}</Muted> : null}
            </Card>
          )}
        </div>
        <div className="grid gap-3 content-start">
          {err && <Callout tone="danger" testid="docs-error">{err}</Callout>}
          {!doc && <Muted testid="docs-empty">No document is open. Pick a topic on the left.</Muted>}
          {state && (
            <section data-testid="docs-reader" data-freshness={String(state.freshness)} data-mode={String(state.mode)} className="rounded-lg border border-border bg-surface p-4 grid gap-3">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <StatePill state={String(state.freshness)} label={FRESH[String(state.freshness)] ?? String(state.freshness)} testid="docs-freshness" />
                <span data-testid="docs-version">{String(state.mode)} document of {state.freshness === "stale" ? `an older knowledge version (the current version is ${String(head.business_revision)})` : `knowledge version ${String(head.business_revision)}`}</span>
                {canRefresh && <Button variant="primary" data-testid="docs-refresh" disabled={busy} onClick={() => void request(currentTopic!)}>Generate for the current version</Button>}
                {content && <Button data-testid="docs-download" disabled={busy} onClick={() => void download()}>Download this document (.md)</Button>}
                {content && <Button data-testid="docs-export" disabled={busy} onClick={() => void exportBundle()}>Export bundle of this version (.zip)</Button>}
              </div>
              {state.freshness === "generating" && <Callout tone="wait" testid="docs-generating">The document is being generated from the accepted knowledge. Nothing is read from your files.</Callout>}
              {state.freshness === "failed" && <Callout tone="danger" testid="docs-failed">The document could not be generated{state.failure_reason ? `: ${String(state.failure_reason)}` : ""}. Nothing is shown in its place.</Callout>}
              {content && <Markdown source={content.text} testid="docs-content" />}
              <Muted>{String(state.statement ?? "")}</Muted>
              <Tech>
                <div data-testid="docs-technical" className="text-xs grid gap-1">
                  <span>artifact <code>{String(state.artifact_id)}</code> snapshot <code>{String(state.snapshot_id)}</code></span>
                  <span>content hash <code>{String(content?.hash ?? state.content_hash ?? "")}</code> template <code>{String(state.template_version)}</code> redaction <code>{String(state.redaction_scope)}</code></span>
                </div>
              </Tech>
            </section>
          )}
        </div>
      </div>
    </>
  );
}
