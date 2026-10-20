// Proposed correspondence of a source refresh (SUR-05). A model proposes where the content and obligations of deleted / changed files continue on the new side; the server
// keeps only proposals whose quotes are verbatim in the excerpts and adds the facts the model cannot claim (bytes identical, which proof the refresh would accept). Every
// proposal is PROPOSED and never a rename proof: it links nothing and marks nothing verified. Old files no proposal covers stay listed as unknown. Only a person links two
// files, by reviewing the refresh again with their own human-confirmed proof and a reason (before publishing). The proposal is requested explicitly (one paid model call).
import * as React from "react";
import { useSession } from "@/session";
import { api, ApiError, errorText } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Tech, TextArea } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { aiJobBadge, aiJobPending, contribution } from "@/lib/aiJob";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const RELATION: Record<string, string> = { moved_same_content: "Moved, same content", moved_rewritten: "Moved and rewritten", split: "Split into several files",
  merged: "Merged with another file", obligation_changed: "The obligation changed", no_counterpart: "No counterpart on the new side" };
const PROOF: Record<string, string> = { content_hash: "Bytes are identical: the files can be linked by that identity.",
  human_confirmed_with_reason: "Bytes differ: only a person's decision with a reason can link these files.", none_needed: "Same path: no identity link is needed." };

export type HumanProof = { from_path: string; to_path: string; kind: "human_confirmed"; reason: string };

export function RefreshCorrespondence({ ws, refreshId, open, canRequest, canRead, proofs, onAdjudicate }: {
  ws: string; refreshId: string; open: boolean; canRequest: boolean; canRead: boolean; proofs: HumanProof[];
  onAdjudicate: (proof: HumanProof) => Promise<void>;
}) {
  const sess = useSession();
  const [st, setSt] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [tick, setTick] = React.useState(0);
  const [reasons, setReasons] = React.useState<Record<string, string>>({});

  React.useEffect(() => {                                                          // read only (no model call); polled while the proposal is being written
    if (!canRead) return;
    let live = true;
    let timer: number | undefined;
    api<Rec>("GET", `/v2/sources/refresh/${q(refreshId)}/correspondence?workspace_id=${q(ws)}`)
      .then((r) => { if (!live) return; setSt(r); setErr(null); if (aiJobPending(r.status)) timer = window.setTimeout(() => setTick((t) => t + 1), 1500); })
      .catch((e) => { if (live) setErr(errorText(e)); });
    return () => { live = false; if (timer) window.clearTimeout(timer); };
  }, [ws, refreshId, tick, canRead]);

  if (!canRead) return <Muted testid="corr-denied">The proposed correspondence quotes the sources, so it needs the evidence permission.</Muted>;

  const request = async () => {
    const used = Number(st?.attempt ?? 0);
    setBusy(true); setErr(null);
    try { await api<Rec>("POST", `/v2/sources/refresh/${q(refreshId)}/correspondence`, { workspace_id: ws, attempt: st?.status === "failed" ? Math.min(3, used + 1) : Math.max(1, used) }); setTick((t) => t + 1); }
    catch (e) { setErr(e instanceof ApiError && e.status === 403 ? "A proposal needs the source import, AI and evidence permissions. Nothing was requested." : errorText(e)); }
    finally { setBusy(false); }
  };

  const status = String(st?.status ?? "not_requested");
  const badge = aiJobBadge(status);
  const pairs = (st?.proposals ?? []) as Rec[];
  const unmatched = (st?.unmatched_old ?? []) as Rec[];
  const rejected = (st?.rejected ?? []) as Rec[];
  const linked = (p: Rec) => proofs.some((x) => x.from_path === p.old_path && x.to_path === p.new_path);
  return (
    <section className="grid gap-2 rounded-md border border-border p-3" data-testid="corr" data-status={status} aria-label="Proposed correspondence">
      <h3 className="font-semibold">Where did the old content go? (proposed by a model)</h3>
      <Muted>{sess.mode === "technical" ? String(st?.label ?? "") : "Proposed by a model from excerpts of the changed files. It is not proof that a file was renamed and it links nothing. Files with byte-for-byte identical content can be linked by that identity; any other link needs a person's decision with a reason."}</Muted>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge status={badge.status} label={badge.label} />
        {status === "done" && <StatusBadge {...contribution(st?.ai_outcome)} />}
      </div>
      {err && <Callout tone="danger" testid="corr-error">{err}</Callout>}
      {(status === "failed" || status === "cancelled" || status === "running" || status === "not_requested") && st?.reason ? <Muted testid="corr-reason">{String(st.reason)}</Muted> : null}
      {(status === "not_requested" || status === "failed") && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" data-testid="corr-request" disabled={busy || !canRequest} onClick={() => void request()}>{busy ? "Requesting..." : status === "failed" ? "Ask again" : "Propose a correspondence"}</Button>
          <span className="text-xs text-muted">{canRequest ? "One paid model call; excerpts of the changed files are sent to the model." : "Needs the source import, AI and evidence permissions."}</span>
        </div>
      )}
      {status === "done" && (
        <>
          {!pairs.length && <Muted testid="corr-none">The model proposed no correspondence ({String(st?.model_status ?? "none")}). Nothing is concluded about where the content went.</Muted>}
          <ol className="grid gap-3">
            {pairs.map((p, i) => {
              const key = `${String(p.old_path)}->${String(p.new_path ?? "")}`;
              const canLink = open && p.proof_possible === "human_confirmed_with_reason" && !!p.new_path;
              return (
                <li key={i} data-testid="corr-pair" data-relation={String(p.relation)} data-rename-proof={String(p.is_rename_proof)} className="grid gap-1 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status="info" label="Proposed (not a decision)" />
                    <strong>{RELATION[String(p.relation)] ?? String(p.relation)}</strong>
                    <span className="text-muted">confidence stated by the model: {String(p.confidence)}</span>
                  </div>
                  <div className="grid gap-1 md:grid-cols-2">
                    <div data-testid="corr-before"><span className="text-muted">Before: </span><code>{String(p.old_path)}</code><blockquote className="border-l-2 border-border pl-2">{String(p.old_quote)}</blockquote></div>
                    <div data-testid="corr-after"><span className="text-muted">After: </span>{p.new_path ? <><code>{String(p.new_path)}</code><blockquote className="border-l-2 border-border pl-2">{String(p.new_quote ?? "")}</blockquote></> : <em>no counterpart proposed</em>}</div>
                  </div>
                  <Muted>Why the model thinks so: {String(p.rationale)}</Muted>
                  {((p.rule_ids ?? []) as string[]).length > 0 && <Muted>Rules bound to the old file: {((p.rule_ids ?? []) as string[]).join(", ")}</Muted>}
                  <p data-testid="corr-proof" data-proof={String(p.proof_possible)}>{PROOF[String(p.proof_possible)] ?? String(p.proof_possible)} This proposal is not a rename proof and does not mark any implementation verified.</p>
                  {linked(p) ? <Muted testid="corr-linked">You decided to link these files; the refresh was reviewed again with your reason.</Muted> : canLink ? (
                    <div className="grid gap-1" data-testid="corr-adjudicate">
                      <Field label="Your reason for linking these two files (your decision, recorded as a human-confirmed proof)">
                        <TextArea rows={2} maxLength={500} data-testid="corr-reason-input" value={reasons[key] ?? ""} onChange={(e) => setReasons({ ...reasons, [key]: e.target.value })} />
                      </Field>
                      <div><Button size="sm" data-testid="corr-link" disabled={busy || !(reasons[key] ?? "").trim()} onClick={async () => {
                        setBusy(true); setErr(null);
                        try { await onAdjudicate({ from_path: String(p.old_path), to_path: String(p.new_path), kind: "human_confirmed", reason: (reasons[key] ?? "").trim() }); }
                        catch (e) { setErr(errorText(e)); } finally { setBusy(false); }
                      }}>Link these files and review the changes again</Button></div>
                      <Muted>Leaving it unlinked keeps the refresh as shown (deleted and added files). Nothing is published until you publish.</Muted>
                    </div>
                  ) : !open && p.proof_possible === "human_confirmed_with_reason" ? <Muted>The refresh is published; files can no longer be linked for this refresh.</Muted> : null}
                </li>
              );
            })}
          </ol>
          {unmatched.length > 0 && (
            <div data-testid="corr-unmatched" className="grid gap-1 text-sm">
              <strong>Unknown: no proposal covers these old files</strong>
              <ul className="list-disc pl-5">{unmatched.map((u, i) => <li key={i}><code>{String(u.path)}</code>{((u.rule_ids ?? []) as string[]).length ? ` (bound rules: ${((u.rule_ids ?? []) as string[]).join(", ")})` : ""}</li>)}</ul>
              <Muted>Unknown is not "deleted on purpose": where this content went was not established.</Muted>
            </div>
          )}
          {rejected.length > 0 && <Muted testid="corr-rejected">{rejected.length} proposal(s) were refused by the server checks (for example a quote not found in the excerpts).</Muted>}
          {((st?.notes ?? []) as string[]).length > 0 && <Muted>Model notes: {((st?.notes ?? []) as string[]).join(" ")}</Muted>}
        </>
      )}
      <Tech><span className="text-xs">job <code>{String(st?.job_id ?? "-")}</code>{rejected.length ? ` refused: ${rejected.map((r) => String(r.reason)).join(", ")}` : ""}</span></Tech>
    </section>
  );
}
