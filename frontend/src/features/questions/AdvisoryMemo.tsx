// Advisory memo panel (SUR-06 risk / trade-off memo of a confirmed change, SUR-08 synthesis memo of a review-board report). Reads the latest memo state from `readPath`;
// a memo is written only by an explicit request (one paid model call, stated next to the button). Kept statements show the severity the server applied (never lower than
// the algorithm's or the board's); every item the memo did not address is listed by the server and stays visible. The memo changes no severity, verdict or change.
import * as React from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Muted, Tech } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { aiJobBadge, aiJobPending, contribution, reasonsCount } from "@/lib/aiJob";
import type { Rec } from "@/features/changes/types";

const KIND_WORDS: Record<string, string> = { risk: "Risk", trade_off: "Trade-off", missing_control: "Missing control", counterevidence: "Counterevidence", agreement: "Agreement",
  disagreement: "Disagreement", minority: "Minority view", blocker: "Blocker" };

/** EUI-07: mark the impact rows a statement cites through the server's opaque `ref_keys` (never by matching text); focus the first one. No row = a clear note. */
function showRows(keys: string[]): string | null {
  const all = Array.from(document.querySelectorAll<HTMLElement>("[data-ref-key]"));
  all.forEach((el) => el.removeAttribute("data-highlight"));
  const hit = all.filter((el) => keys.includes(el.dataset.refKey ?? ""));
  if (!hit.length) return "The rules this statement cites are not shown in the impact list on this page.";
  hit.forEach((el) => el.setAttribute("data-highlight", "true"));
  hit[0].scrollIntoView({ block: "center" });
  hit[0].focus();
  return hit.length < keys.length ? `${hit.length} of ${keys.length} cited rules are shown in the impact list; the others are not on this page.` : null;
}

export function AdvisoryMemo({ title, purpose, readPath, requestPath, requestBody, canRequest, cannotReason, testid }: {
  title: string; purpose: string; readPath: string; requestPath: string; requestBody: Rec; canRequest: boolean; cannotReason: string; testid: string;
}) {
  const [st, setSt] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [tick, setTick] = React.useState(0);
  const [jump, setJump] = React.useState<string | null>(null);

  React.useEffect(() => {                                                          // read only; polled while the memo is being written
    let live = true;
    let timer: number | undefined;
    api<Rec>("GET", readPath)
      .then((r) => { if (!live) return; setSt(r); setErr(null); if (aiJobPending(r.status)) timer = window.setTimeout(() => setTick((t) => t + 1), 1500); })
      .catch((e) => { if (live) setErr(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "This memo is not available to your account." : errorText(e)); });
    return () => { live = false; if (timer) window.clearTimeout(timer); };
  }, [readPath, tick]);

  const request = async () => {
    const used = Number(st?.attempt ?? 0);
    setBusy(true); setErr(null);
    try {
      await api<Rec>("POST", requestPath, { ...requestBody, attempt: st?.status === "failed" ? Math.min(3, used + 1) : Math.max(1, used) });
      setTick((t) => t + 1);
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 403 ? "Writing a memo needs the AI permission. Nothing was requested." : errorText(e));
    } finally { setBusy(false); }
  };

  const memo = (st?.memo ?? null) as Rec | null;
  const badge = aiJobBadge(st?.status ?? "not_requested");
  const canAsk = canRequest && (st?.status === "not_requested" || (st?.status === "failed" && Number(st?.attempt ?? 1) < 3));
  return (
    <Card title={title} testid={testid} data={{ state: String(st?.status ?? "loading") }}>
      <Muted>{purpose}</Muted>
      {err && <Callout tone="danger" testid={`${testid}-error`}>{err}</Callout>}
      {st && (
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={badge.status} label={badge.label} />
          {st.status === "done" && <StatusBadge {...contribution(st.ai_outcome)} />}
        </div>
      )}
      {st?.reason && st.status !== "done" ? <Muted testid={`${testid}-reason`}>{String(st.reason)}</Muted> : null}
      {(canAsk || (!canRequest && st?.status === "not_requested")) && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="primary" data-testid={`${testid}-request`} disabled={busy || !canRequest} onClick={() => void request()}>
            {busy ? "Requesting..." : st?.status === "failed" ? "Try again (new attempt)" : "Write the memo"}
          </Button>
          <span className="text-xs text-muted">{canRequest ? "Uses one paid model call within your workspace allowance." : cannotReason}</span>
        </div>
      )}
      {memo && (
        <div className="grid gap-3" data-testid={`${testid}-memo`} data-complete={String(memo.complete)}>
          <Callout tone="info">{String(memo.label ?? "Advisory memo. It changes no severity, verdict or change; a person decides.")}</Callout>
          {Array.isArray(memo.not_addressed) && memo.not_addressed.length > 0 && (
            <Callout tone="warn" role="note" testid={`${testid}-not-addressed`}>
              <p className="font-medium">Not addressed by the memo ({memo.not_addressed.length}); listed by the server so nothing is hidden:</p>
              <ul className="list-disc pl-5">
                {(memo.not_addressed as Rec[]).map((n) => <li key={String(n.ref)} data-ref={String(n.ref)}>{String(n.text)} <span className="text-muted">({String(n.reason)})</span></li>)}
              </ul>
            </Callout>
          )}
          {Array.isArray(memo.statements) && memo.statements.length === 0 && <Muted>The memo kept no statement after the server checks.</Muted>}
          <ul className="grid gap-2">
            {((memo.statements ?? []) as Rec[]).map((m, i) => (
              <li key={i} data-testid={`${testid}-statement`} data-floor={String(m.floor_applied)} className="rounded-md border border-border p-3 grid gap-1">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <strong>{KIND_WORDS[String(m.kind)] ?? String(m.kind)}</strong>
                  <span>materiality {String(m.materiality)}{m.floor_applied ? ` (raised from ${String(m.materiality_proposed)} to the algorithm's severity)` : ""}</span>
                </div>
                <p>{String(m.statement)}</p>
                <Muted>{String(m.rationale ?? "")}</Muted>
                <Tech><span className="text-xs">refs <code>{((m.refs ?? []) as string[]).join(", ")}</code></span></Tech>
                {((m.ref_keys ?? []) as string[]).length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    <Button size="sm" variant="ghost" data-testid={`${testid}-show-impact`} onClick={() => setJump(showRows((m.ref_keys ?? []) as string[]))}>
                      Show in impact ({((m.ref_keys ?? []) as string[]).length})</Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
          {jump && <Muted testid={`${testid}-jump-note`}>{jump}</Muted>}
          {Array.isArray(memo.open_questions) && memo.open_questions.length > 0 && (
            <div className="text-sm"><p className="font-medium">Open questions</p><ul className="list-disc pl-5">{(memo.open_questions as string[]).map((x) => <li key={x}>{x}</li>)}</ul></div>
          )}
          {Array.isArray(memo.rejected) && memo.rejected.length > 0 && (
            <div className="text-sm" data-testid={`${testid}-rejected`}>
              <p>{memo.rejected.length} statement(s) refused by the server checks:</p>
              <ul className="list-disc pl-5">{reasonsCount(memo.rejected).map(([r, n]) => <li key={r}><code>{r}</code>: {n}</li>)}</ul>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
