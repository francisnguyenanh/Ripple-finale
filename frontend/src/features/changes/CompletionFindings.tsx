// Stage 10, the part about findings. Three separate facts are said separately (the change was applied and checked; a person accepted the result of THIS request; the knowledge
// of the whole workspace is complete) and none is read from another. Findings come from the same read model as the Questions list: a repeat of the same problem is one issue,
// history is not counted as open, and a finding blocks the result only when it concerns a place the change touched (or was never re-checked after the file changed).
import { Button } from "@/components/ui/button";
import { Callout, Muted, Tech } from "@/components/bits";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";
import { ensureCoverage } from "./Coverage";
import { useFlow } from "./flow";
import type { Rec } from "./types";

export const GATE_WORDS: Record<string, string> = {
  critical_gap_open: "a serious finding about a place this change touched is still open",
  findings_not_rechecked: "the changed file was read again but its new readings have not been checked for problems yet",
  knowledge_not_accepted: "the knowledge version has not been accepted yet",
  reanalysis_incomplete: "the files touched by the change are still being read again",
  machine_verification_not_pass: "the system's own check after the change did not pass",
  report_stale: "the source moved after the check; the check is out of date",
  index_not_ready: "the search index for the new knowledge is not ready",
  manual_work_unproven: "work a person has to do by hand is not yet proven",
  items_without_disposition: "each place that may be affected by this change needs your decision: accept the limit, judged unrelated, or leave unhandled",
  disposition_reason_required: "a reason of at least 10 characters is required for every accepted limit or unrelated judgement",
};
export const gateWords = (code: string) => GATE_WORDS[code] ?? code.replace(/_/g, " ");

const AXIS: Record<string, string> = { VERIFIED: "verified", PARTIAL: "partly verified", FAILED: "failed", PENDING: "not finished" };

export type Disp = Record<string, { disposition: string; reason: string }>;
export const MIN_REASON = 10;
export type Group = { key: string; file: string; kind: "finding" | "reading"; ids: string[]; lines: string[] };

// Everything the accepter must decide, grouped by file and kind (items with the same basis share one decision; each id is still stored on its own).
export function dispositionGroups(f: Rec): Group[] {
  const m = new Map<string, Group>();
  const add = (kind: "finding" | "reading", file: string, id: string, line: string) => {
    const key = `${kind}|${file}`;
    const g = m.get(key) ?? { key, file, kind, ids: [], lines: [] };
    g.ids.push(id); g.lines.push(line); m.set(key, g);
  };
  for (const r of (f.possible_effect ?? []) as Rec[]) add("finding", r.file ?? "?", r.finding_id, `${r.rule ?? "a reading"}: ${r.plain}`);
  for (const r of (f.same_concept ?? []) as Rec[]) add("reading", r.logical_path ?? "?", r.item_id, `${r.subject ?? "a reading"} states ${r.value ?? "a value"} (${r.stance === "old_value" ? "still the old value" : "a different value"})`);
  return [...m.values()];
}
export const dispositionsReady = (groups: Group[], d: Disp) => groups.every((g) => g.ids.every((i) => {
  const x = d[i];
  return !!x && (x.disposition === "unhandled" || (["accepted_limit", "judged_unrelated"].includes(x.disposition) && x.reason.trim().length >= MIN_REASON));
}));
export const dispositionPayload = (groups: Group[], d: Disp) => groups.flatMap((g) => g.ids.map((i) => ({ item_id: i, disposition: d[i].disposition, reason: d[i].reason.trim() })));

const CHOICES: [string, string][] = [
  ["accepted_limit", "Accept this limit (reason required)"],
  ["judged_unrelated", "I judge it unrelated (reason required)"],
  ["unhandled", "Leave it unhandled (the result is then accepted in a NARROW scope)"],
];
const humanizeD = (d: string) => (d === "accepted_limit" ? "Limit accepted" : d === "judged_unrelated" ? "Judged unrelated" : "Left unhandled");

export function CompletionFindings({ dto, disp, setDisp }: { dto: Rec; disp: Disp; setDisp: (v: Disp) => void }) {
  const { ws, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const f = (dto.findings ?? {}) as Rec;
  const ax = (dto.status_axes ?? {}) as Rec;
  const blocking = (f.blocking ?? []) as Rec[];
  const unchecked = (f.unchecked_related ?? []) as Rec[];
  const review = (f.needs_review ?? []) as Rec[];
  const kn = (ax.knowledge ?? {}) as Rec;
  const decided = !!dto.completion?.decision;
  const narrow = dto.completion?.decision?.acceptance_scope === "NARROW";
  const groups = dispositionGroups(f);
  const unhandledFiles = groups.filter((g) => g.ids.some((i) => disp[i]?.disposition === "unhandled")).map((g) => g.file);
  if (!dto.findings && !dto.status_axes) return null;
  return (
    <div className="grid gap-3" data-testid="completion-findings">
      <div data-testid="status-axes" className="rounded-md border border-border p-3 text-sm grid gap-1">
        <h3 className="font-semibold">Where this stands: three separate facts</h3>
        <p data-testid="axis-execution" data-status={ax.execution?.status}>The change was applied and checked by the system: <strong>{AXIS[ax.execution?.status as string] ?? "unknown"}</strong>.</p>
        <p data-testid="axis-result" data-status={ax.request_result?.status}>A person accepted the result of this request: <strong>{decided ? (dto.completion.status === "COMPLETED" ? (narrow ? "yes, in a NARROW scope (some items left unhandled)" : "yes") : String(dto.completion.status).toLowerCase().replace(/_/g, " ")) : "not yet"}</strong>.</p>
        <p data-testid="axis-knowledge" data-status={kn.status}>The knowledge of the whole workspace: <strong>{kn.status === "COMPLETE" ? "complete" : "partial"}</strong>
          {kn.status !== "COMPLETE" && <> - {kn.needs_review ?? 0} finding(s) need review, {kn.possible_effect ?? 0} may affect the change, {kn.workspace_open_issues ?? 0} open issue(s) in the workspace, {kn.not_checked ?? 0} reading(s) not checked{kn.version_accepted ? "" : ", knowledge version not accepted yet"}</>}.</p>
        <Muted>None of these three is read from another: a verified change can still leave the knowledge partial, and an accepted result does not make the other files right.</Muted>
      </div>
      {blocking.map((b) => (
        <Callout key={b.finding_id} tone="danger" testid="finding-item">
          <span data-testid="blocking-finding" data-finding-id={b.finding_id}>
            <strong>Blocks this result: {b.rule ?? "a reading"}{b.file ? ` in ${b.file}` : ""}.</strong> {b.plain}{b.not_rechecked ? " It was found on the earlier reading of a place this change touched, and the changed file has not been checked again." : ""}
            <br />What to do: {b.todo} <a className="underline" href={hashFor(ws, "questions", undefined, { finding: String(b.finding_id) })}>Open it in Questions &amp; Decisions</a>.
            <Tech> <code>{b.finding_id}</code>{b.runs > 1 ? ` (found by ${b.runs} runs)` : ""}</Tech>
          </span>
          {b.decision && (
            <div data-testid="decision-card" className="mt-2 grid gap-2 text-sm">
              <p data-testid="decision-question"><strong>Question to answer:</strong> {b.decision.question}</p>
              {((b.decision.evidence ?? []) as Rec[]).length > 0
                ? <ul data-testid="decision-evidence" className="list-disc pl-5">{(b.decision.evidence as Rec[]).map((e, i) => <li key={i}><strong>{e.file}</strong>: &ldquo;{e.quote}&rdquo;{e.locator ? <Tech> <code>{String(e.locator)}</code></Tech> : null}</li>)}</ul>
                : <Muted testid="decision-evidence-hidden">{b.decision.evidence_note ?? "No source passage is stored for this finding."}</Muted>}
              <ul data-testid="decision-options" className="grid gap-1">
                {((b.decision.options ?? []) as Rec[]).map((o) => (
                  <li key={o.key} data-testid="decision-option" data-option={o.key} data-edits-source={String(!!o.edits_source)}>
                    <strong>{o.title}.</strong> {o.effect}
                    <Muted> {o.edits_source ? "Edits a source file." : "Records knowledge only: no source file is edited."} Who may do it: {((o.who ?? []) as string[]).map((r) => r.replace(/_/g, " ")).join(", ") || "nobody holds this right"}. Where: {o.where}.</Muted>
                  </li>
                ))}
              </ul>
              <Muted testid="decision-you">{b.decision.you_can_decide ? "You hold the right to record this decision." : `You do not hold the right to record this decision; it belongs to: ${((b.decision.deciders ?? []) as string[]).map((r) => r.replace(/_/g, " ")).join(", ")}.`}</Muted>
            </div>
          )}
        </Callout>
      ))}
      {unchecked.length > 0 && (
        <Callout tone="warn" testid="unchecked-readings">
          <span>The changed file was read again, but {unchecked.length} new reading(s) of it ({unchecked.slice(0, 3).map((u) => u.rule).join(", ")}{unchecked.length > 3 ? ", ..." : ""}) have not been checked for problems yet, so a serious finding could still be hiding.
            {s.can("source.import")
              ? <> <Button data-testid="action-recheck" disabled={pending} onClick={async () => { if (await run(() => ensureCoverage(ws))) { notify.status("The checks were started (no AI call). Reload in a moment."); await refresh(); } }}>Check them now (no AI call)</Button></>
              : <> A person who can add sources and start checks does this; it uses no AI.</>}</span>
        </Callout>
      )}
      {groups.length > 0 && !decided && (
        <div data-testid="possible-effect" className="rounded-md border border-warning p-3 text-sm grid gap-3">
          <strong>{groups.reduce((n, g) => n + g.ids.length, 0)} place(s) may be affected by this change: decide each one (reading them is not a decision)</strong>
          <Muted>There is evidence that these places depend on what you changed, or they state a limit of the same kind (an AI judgement, a text scan, or the same concept with a number). It is not proof, so they do not block, but "I read them" is not enough: choose for each group. Leaving one unhandled is allowed; the result is then accepted in a NARROW scope and the request is not shown as completely done.</Muted>
          {groups.map((g) => {
            const first = disp[g.ids[0]];
            const set = (patch: Partial<{ disposition: string; reason: string }>) => {
              const n = { ...disp };
              for (const i of g.ids) n[i] = { disposition: first?.disposition ?? "", reason: first?.reason ?? "", ...patch };
              setDisp(n);
            };
            return (
              <fieldset key={g.key} data-testid="disposition-group" data-file={g.file} className="rounded border border-border p-2 grid gap-1">
                <legend className="font-medium">{g.file} ({g.ids.length} {g.kind === "finding" ? "finding(s)" : "reading(s)"})</legend>
                <ul className="list-disc pl-5">{g.lines.map((l, k) => <li key={k} data-testid="possible-effect-item" data-finding-id={g.ids[k]}>{l}<Tech> <code>{g.ids[k]}</code></Tech></li>)}</ul>
                {s.can("change.accept_result") && (<>
                  {CHOICES.map(([v, label]) => <label key={v} className="flex items-center gap-2"><input type="radio" name={`d-${g.key}`} data-testid={`disp-${v}`} checked={first?.disposition === v} onChange={() => set({ disposition: v })} />{label}</label>)}
                  {(first?.disposition === "accepted_limit" || first?.disposition === "judged_unrelated") && <input data-testid="disp-reason" aria-label={`Reason for ${g.file}`} className="border border-border rounded px-2 py-1" placeholder="Reason (at least 10 characters)" value={first.reason} onChange={(e) => set({ reason: e.target.value })} />}
                </>)}
              </fieldset>
            );
          })}
          {unhandledFiles.length > 0 && <Callout tone="warn" testid="narrow-scope-note">Accepted scope: only the change itself. Not handled: {unhandledFiles.join(", ")}. The request will NOT be shown as completely done.</Callout>}
        </div>
      )}
      {decided && ((f.dispositions ?? []) as Rec[]).length > 0 && (
        <div data-testid="recorded-dispositions" className="rounded-md border border-border p-3 text-sm grid gap-1">
          <strong>Decisions recorded with the result ({narrow ? "NARROW scope" : "full scope"})</strong>
          {narrow && <Callout tone="warn" testid="narrow-scope-banner">Only the change itself was accepted. {((f.dispositions as Rec[]).filter((x) => x.disposition === "unhandled")).length} item(s) remain unhandled; this is not a finished request.</Callout>}
          <ul className="list-disc pl-5">{(f.dispositions as Rec[]).map((x) => <li key={x.item_id} data-testid="recorded-disposition" data-disposition={x.disposition}>{humanizeD(x.disposition)}{x.reason ? `: ${x.reason}` : ""}<Tech> <code>{x.item_id}</code></Tech></li>)}</ul>
        </div>
      )}
      {((f.needs_review_serious ?? []) as Rec[]).length > 0 && (
        <div data-testid="needs-review-serious" className="rounded-md border border-warning p-3 text-sm grid gap-1">
          <strong>{(f.needs_review_serious as Rec[]).length} serious finding(s) whose relation to this change is NOT known</strong>
          <Muted>Not blocking and not called unrelated: the analysis found no link either way (a missing link is not proof of no effect). Nobody has decided them. Open each in Questions &amp; Decisions; the knowledge stays partial until they are decided.</Muted>
          <ul className="list-disc pl-5">{(f.needs_review_serious as Rec[]).map((r) => <li key={r.finding_id} data-testid="serious-unknown-item">{r.rule ?? "a reading"}{r.file ? ` in ${r.file}` : ""}: {r.plain} <a className="underline" href={hashFor(ws, "questions", undefined, { finding: String(r.finding_id) })}>Open it</a></li>)}</ul>
        </div>
      )}
      {(f.needs_review_count ?? 0) > 0 && (
        <details data-testid="needs-review" className="rounded-md border border-border p-3 text-sm">
          <summary className="cursor-pointer font-semibold">{f.needs_review_count} finding(s) need review ("Needs review", not blocking this result)</summary>
          <Muted>The analysis cannot say these are unrelated to this change (it never says a rule is "unaffected"), so they are not hidden and they are counted in the knowledge status. They do not stop a person from deciding this result.</Muted>
          <ul className="list-disc pl-5 grid gap-1 mt-2">
            {review.map((r) => <li key={r.finding_id} data-testid="review-finding">{r.rule ?? "a reading"}{r.file ? ` in ${r.file}` : ""}: {r.plain}<Tech> <code>{r.finding_id}</code></Tech></li>)}
          </ul>
          {(f.needs_review_count ?? 0) > review.length && <Muted>... and {(f.needs_review_count as number) - review.length} more.</Muted>}
        </details>
      )}
    </div>
  );
}
