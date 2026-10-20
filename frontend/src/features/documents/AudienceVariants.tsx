// Audience versions (AIX-D1 / LHA-D1) of the document being read: executive, SOP and audit wording written by a pinned model and checked by the server against the facts.
// Opening the page only READS existing variants; a variant is generated only by an explicit request (one paid model call, stated next to the button). Job state, the model's
// contribution and the server's checks are shown as separate facts; a fallback carries no model wording and is labelled so; nothing here changes the knowledge.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, Tech } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { Markdown } from "@/lib/markdown";
import { aiJobBadge, aiJobPending, reasonsCount } from "@/lib/aiJob";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const AUDIENCES: { key: string; title: string; purpose: string }[] = [
  { key: "executive", title: "Executive summary", purpose: "decisions, exposure and open questions for a manager" },
  { key: "sop", title: "Operating procedure", purpose: "the checks a performer makes; the order is a reading aid, not a verified sequence" },
  { key: "audit", title: "Audit view", purpose: "rules, authority, evidence references and limits for a reviewer" },
];
const MAX_ATTEMPT = 3;

function latestFor(variants: Rec[], audience: string): Rec | null {
  const mine = variants.filter((v) => v.audience === audience);
  return mine.length ? mine[mine.length - 1] : null;
}

function attemptsUsed(variants: Rec[], audience: string): number {
  return variants.filter((v) => v.audience === audience).length;
}

async function readVariant(ws: string, vid: string, signal: AbortSignal): Promise<{ text: string; hash: string | null }> {
  const resp = await fetch(`/v2/business/documents/variants/${q(vid)}/content?workspace_id=${q(ws)}`, { credentials: "same-origin", headers: { "X-Ripple-Judge": "1" }, signal });
  if (!resp.ok) throw new ApiError(resp.status, {});
  return { text: await resp.text(), hash: resp.headers.get("X-Content-Hash") };
}

export function AudienceVariants({ ws, artifactId, baseAvailable, canUseAi }: { ws: string; artifactId: string; baseAvailable: boolean; canUseAi: boolean }) {
  const [variants, setVariants] = React.useState<Rec[] | null>(null);
  const [open, setOpen] = React.useState<string | null>(null);
  const [content, setContent] = React.useState<{ text: string; hash: string | null } | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [tick, setTick] = React.useState(0);

  React.useEffect(() => {                                                          // read-only list; polled only while a variant is still being written
    let live = true;
    let timer: number | undefined;
    api<Rec>("GET", `/v2/business/documents/${q(artifactId)}/variants?workspace_id=${q(ws)}`)
      .then((r) => {
        if (!live) return;
        const vs = (r.variants ?? []) as Rec[];
        setVariants(vs); setErr(null);
        if (vs.some((v) => aiJobPending(v.freshness))) timer = window.setTimeout(() => setTick((t) => t + 1), 1500);
      })
      .catch((e) => { if (live) setErr(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "Audience versions are not available to your account." : errorText(e)); });
    return () => { live = false; if (timer) window.clearTimeout(timer); };
  }, [ws, artifactId, tick]);

  React.useEffect(() => {                                                          // the opened variant's bytes, once it exists
    const v = open && variants ? variants.find((x) => x.variant_id === open) : null;
    if (!v || (v.freshness !== "available" && v.freshness !== "stale")) { setContent(null); return; }
    const ctl = new AbortController();
    readVariant(ws, String(v.variant_id), ctl.signal).then(setContent).catch((e) => {
      if (!(e instanceof DOMException && e.name === "AbortError")) setErr(e instanceof ApiError && e.status === 403 ? "This version is not available to your account." : errorText(e));
    });
    return () => ctl.abort();
  }, [ws, open, variants]);

  const generate = async (audience: string) => {
    const used = attemptsUsed(variants ?? [], audience);
    setBusy(audience); setErr(null);
    try {
      const r = await api<Rec>("POST", `/v2/business/documents/${q(artifactId)}/variants`, { workspace_id: ws, audience, attempt: Math.min(MAX_ATTEMPT, used + 1) });
      setOpen(String(r.variant_id));
      setTick((t) => t + 1);
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 403 ? "Generating an audience version needs the AI permission. Nothing was generated." : errorText(e));
    } finally { setBusy(null); }
  };

  const opened = open && variants ? variants.find((v) => v.variant_id === open) ?? null : null;
  const fc = (opened?.fact_coverage ?? {}) as Rec;
  const validation = (fc.validation ?? {}) as Rec;
  return (
    <section data-testid="variants" aria-labelledby="variants-title" className="rounded-lg border border-border bg-surface p-4 grid gap-3">
      <h2 id="variants-title" className="text-base font-semibold">Audience versions</h2>
      <Muted>The same knowledge version worded for one audience by a model. The server checks every normative paragraph against the facts and states what it leaves out. A version is generated only when you ask for it.</Muted>
      {err && <Callout tone="danger" testid="variants-error">{err}</Callout>}
      {variants === null && !err && <p role="status" className="text-muted text-sm">Loading audience versions...</p>}
      {variants !== null && (
        <ul className="grid gap-2">
          {AUDIENCES.map((a) => {
            const v = latestFor(variants, a.key);
            const used = attemptsUsed(variants, a.key);
            const badge = aiJobBadge(v ? v.freshness : "not_requested");
            const canRetry = !v || v.freshness === "failed" || v.status === "fallback";
            const reason = !baseAvailable ? "Open a current document first." : !canUseAi ? "Your account cannot use AI in this workspace." : used >= MAX_ATTEMPT && canRetry ? "All three attempts are used." : "";
            return (
              <li key={a.key} data-testid="variant-row" data-audience={a.key} data-freshness={String(v?.freshness ?? "not_requested")} data-status={String(v?.status ?? "")}
                  className="grid gap-1 rounded-md border border-border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <strong>{a.title}</strong>
                  <StatusBadge status={badge.status} label={badge.label} />
                  {v?.status === "generated" && <StatusBadge status="info" label="Model wording, checked by the server" />}
                  {v?.status === "fallback" && <StatusBadge status="partial" label="Deterministic fallback, no model wording" />}
                </div>
                <Muted>For {a.purpose}.</Muted>
                {v?.freshness === "failed" && <Callout tone="danger" testid="variant-failed">This version could not be written{v.failure_reason ? `: ${String(v.failure_reason)}` : ""}. Nothing is shown in its place.</Callout>}
                <div className="flex flex-wrap items-center gap-2">
                  {v && (v.freshness === "available" || v.freshness === "stale") && (
                    <Button size="sm" data-testid="variant-open" onClick={() => setOpen(String(v.variant_id))} aria-pressed={open === v.variant_id}>Read this version</Button>
                  )}
                  {canRetry && (
                    <Button size="sm" variant={v ? "secondary" : "primary"} data-testid="variant-generate" disabled={!!busy || !!reason}
                            onClick={() => void generate(a.key)}>
                      {busy === a.key ? "Requesting..." : v ? "Try again (new attempt)" : "Generate this version"}
                    </Button>
                  )}
                  {canRetry && <span className="text-xs text-muted" data-testid="variant-consent">{reason || "Uses one paid model call within your workspace allowance."}</span>}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {opened && (
        <article data-testid="variant-reader" data-status={String(opened.status ?? "")} className="grid gap-2 border-t border-border pt-3">
          <h3 className="font-semibold">{AUDIENCES.find((a) => a.key === opened.audience)?.title ?? String(opened.audience)}</h3>
          {opened.status === "fallback" && <Callout tone="warn" testid="variant-fallback">The model's wording did not pass the checks, so this is the server's deterministic text with the same facts. It is not an AI result.</Callout>}
          <Callout tone="info" testid="variant-review-note">No reviewer, human or model, has checked the meaning of the free wording. The facts table, unknowns and limits in this version are written by the server.</Callout>
          {content ? <Markdown source={content.text} testid="variant-content" /> : aiJobPending(opened.freshness) ? <p role="status" className="text-muted text-sm">The version is being written...</p> : null}
          {Array.isArray(opened.rejected) && opened.rejected.length > 0 && (
            <div data-testid="variant-rejected" className="text-sm">
              <p>{opened.rejected.length} paragraph(s) were left out by the server checks:</p>
              <ul className="list-disc pl-5">{reasonsCount(opened.rejected).map(([r, n]) => <li key={r}><code>{r}</code>: {n}</li>)}</ul>
            </div>
          )}
          <Tech>
            <div data-testid="variant-technical" className="text-xs grid gap-1">
              <span>variant <code>{String(opened.variant_id)}</code> model pinned <code>{String(opened.model_pinned ?? "")}</code> served <code>{String(opened.model_served ?? "")}</code></span>
              <span>clause checks <code>{String(validation.clause_checks ?? validation.version ?? "")}</code> model review <code>{String(validation.model_review ?? "not_performed")}</code> content hash <code>{String(content?.hash ?? opened.content_hash ?? "")}</code></span>
            </div>
          </Tech>
        </article>
      )}
    </section>
  );
}
