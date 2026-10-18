// Say a change to a business rule in words. The server shows what it understood (never a model's raw text); confirming records the request only and changes no rule.
// All text from the model, the sources and people is rendered as plain text (React escapes it); nothing here sets HTML.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, StatePill, Tech, TextArea } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useParam } from "@/lib/param";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";
import { BusinessChangeReview } from "./BusinessChangeReview";

const q = encodeURIComponent;
const STATUS_WORDS: Record<string, string> = { READY: "Ready to confirm", CLARIFY: "More detail needed", UNSUPPORTED: "Not supported" };
const JOB_WORDS: Record<string, string> = { waiting_for_approval: "Waiting to start", running: "Working on it", failed: "Could not understand", cancelled: "Cancelled" };
const REASON_WORDS: Record<string, string> = {
  authority_revoked: "The person who asked no longer has permission to ask.",
  snapshot_moved: "The business knowledge changed before the request was understood. Ask again to use the latest version.",
  budget_exceeded: "The request and its context are too large.",
};

function reasonText(reason: string | null): string {
  const key = reason ? Object.keys(REASON_WORDS).find((k) => reason.startsWith(k)) : undefined;
  return key ? REASON_WORDS[key] : "The request could not be understood.";
}

export function BusinessChange({ ws }: { ws: string }) {
  const s = useSession();
  const { run, pending } = useAction();
  const [cid, setCid] = useParam("chg");
  const [text, setText] = React.useState("");
  const [data, setData] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [done, setDone] = React.useState<Rec | null>(null);
  const submitWhy = React.useId();
  const mode = s.mode;
  React.useEffect(() => {
    if (!cid) { setData(null); return; }
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const r = await api<Rec>("GET", `/v2/business/changes/${q(cid)}?workspace_id=${q(ws)}&mode=${mode}`);
        if (!live) return;
        setData(r); setErr(null);
        if (r.status === "running" || r.status === "waiting_for_approval") timer = setTimeout(() => void tick(), 1500);
      } catch (e) {
        if (!live) return;
        if (e instanceof ApiError) setErr(e.status === 404 || e.status === 403 ? "This request is not available to your account." : "The request could not be loaded.");
        else throw e;
      }
    };
    void tick();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [cid, ws, mode, done]);
  const canAsk = s.can("intent.create") && s.can("ai.use");
  const u = (data?.understanding ?? null) as Rec | null;
  const submit = async () => {
    const r = await run(() => api<Rec>("POST", "/v2/business/changes", { workspace_id: ws, request: text.trim() }));
    if (r) { setCid(r.change_id as string); setText(""); setDone(null); }
  };
  const confirm = async () => {
    if (!cid || !u) return;
    const r = await run(() => api<Rec>("POST", `/v2/business/changes/${q(cid)}/confirm`, { workspace_id: ws, preview_hash: u.preview_hash }));
    if (r) setDone(r);
  };
  const tech = u?.technical as Rec | undefined;
  return (
    <div className="grid gap-4" data-testid="business-change">
      {canAsk ? (
        <div className="rounded-lg border border-border bg-surface p-4 grid gap-3">
          <Field label="Describe a change to a business rule" hint="For example who approves, when it applies, an exception, where it applies, or a value. Nothing changes until you confirm, and confirming only records your request.">
            <TextArea data-testid="chg-input" rows={3} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
          </Field>
          <div><Button variant="primary" data-testid="chg-submit" title={!text.trim() ? "Describe the change first." : pending ? "The request is being sent." : undefined} disabled={pending || !text.trim()} aria-describedby={!text.trim() ? submitWhy : undefined} onClick={() => void submit()}>Check my request</Button>{!text.trim() && <span id={submitWhy} data-testid="chg-submit-why" className="ml-3 text-sm text-muted">Describe the change first.</span>}</div>
        </div>
      ) : <Muted testid="chg-no-ask">You can read change requests, but your role cannot make new ones.</Muted>}
      {err && <Callout tone="danger" testid="chg-error">{err}</Callout>}
      {data && data.status !== "done" && data.status !== "failed" && data.status !== "cancelled" && <Callout tone="wait" testid="chg-pending">{JOB_WORDS[data.status as string] ?? "Working on it"}...</Callout>}
      {data && (data.status === "failed" || data.status === "cancelled") && <Callout tone="danger" testid="chg-failed">{JOB_WORDS[data.status as string]}: {reasonText(data.reason as string | null)}</Callout>}
      {u && (
        <article data-testid="chg-understanding" data-status={u.status as string} className="rounded-lg border border-border bg-surface p-4 grid gap-3">
          <Muted testid="chg-request">Your request: {String(data?.request)}</Muted>
          <div className="flex flex-wrap items-center gap-3"><StatePill state={u.status as string} label={STATUS_WORDS[u.status as string] ?? (u.status as string)} testid="chg-status" /></div>
          <p data-testid="chg-message">{u.message as string}</p>
          {u.knowledge_moved && <Callout tone="warn" testid="chg-moved">The business knowledge has changed since this was understood. Ask again to use the latest version.</Callout>}
          {((u.descriptions ?? []) as string[]).length > 0 && <div><p className="text-sm font-medium">What I understood:</p><ul className="list-disc pl-5" data-testid="chg-descriptions">{(u.descriptions as string[]).map((d, i) => <li key={i}>{d}</li>)}</ul></div>}
          {((u.refused ?? []) as Rec[]).map((r, i) => <Callout key={i} tone="warn" testid="chg-refused">“{r.quote as string}”: {r.why as string}</Callout>)}
          {((u.already_the_case ?? []) as string[]).length > 0 && <Muted testid="chg-noop">Already the case: {(u.already_the_case as string[]).join("; ")}</Muted>}
          {((u.questions ?? []) as string[]).length > 0 && <div data-testid="chg-questions"><p className="text-sm font-medium">To go on, please say:</p><ul className="list-disc pl-5 text-sm">{(u.questions as string[]).map((t, i) => <li key={i}>{t}</li>)}</ul></div>}
          {((u.also_affected_to_check ?? []) as string[]).length > 0 && <Muted testid="chg-affected">Related rules to check: {(u.also_affected_to_check as string[]).join("; ")}</Muted>}
          <Muted>{u.nothing_applied as string}</Muted>
          {tech && <Tech><div data-testid="chg-technical" className="rounded-md border border-border p-3 text-sm grid gap-1">
            <p>snapshot <code>{String((tech.pin as Rec)?.snapshot_id)}</code>, business revision {String((tech.pin as Rec)?.business_revision)}; write scope {((tech.write_scope ?? []) as string[]).join(", ") || "none"}</p>
            {((tech.operations ?? []) as Rec[]).map((o) => <p key={o.op_id as string}><code>{o.kind as string}</code> on <code>{o.rule_id as string}</code>: {JSON.stringify(o.before)} → {JSON.stringify(o.desired)}</p>)}
            {((tech.rejected ?? []) as Rec[]).map((r, i) => <p key={i} className="text-muted">rejected: {r.reason as string}</p>)}
          </div></Tech>}
          {done || data?.confirmed ? <Callout tone="info" testid="chg-confirmed">Your request is recorded. No rule has been changed.</Callout>
            : u.can_confirm && <div><Button variant="primary" data-testid="chg-confirm" disabled={pending} onClick={() => void confirm()}>Confirm this is what I mean</Button></div>}
        </article>
      )}
      {cid && (done || data?.confirmed) ? <BusinessChangeReview ws={ws} cid={cid} /> : null}
    </div>
  );
}
