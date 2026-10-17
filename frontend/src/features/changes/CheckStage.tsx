// "Check after the change": what must be true in the file once the change is applied. The SERVER proposes it from validated data
// (where: the typed proposal edit; what: the CONFIRMED REQUEST, never the proposal's own value). An authorised person who did not ask for the change
// reads it in plain words and confirms it with one button. Typing the file, the key and the value by hand is still possible, under Advanced.
// A confirmed check is mechanical (the stored file holds the value); it does not prove the change does what the business wanted, and the page says so.
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Tech, TextInput } from "@/components/bits";
import { basename, parseValue } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { fmtForm } from "./fmtForm";
import { useFlow } from "./flow";
import { stageNo, stageTitle } from "./registry";
import { StageCard } from "./StageCard";
import type { Rec } from "./types";
import { isDone } from "./types";

interface Item {
  kind: string; logical_path: string; locator: string; expected: unknown; needs_human_value: boolean; agrees_with_proposal: boolean | null;
  proposal_shows: { before: unknown; after: unknown }; sentence: string; basis: string; note: string;
}
interface Suggestion { items: Item[]; stale: boolean; stale_reason?: string | null; complete: boolean; missing: string[]; suggestion_sha256?: string; limits: string[] }

function ManualForm() {
  const { ws, ids, refresh } = useFlow();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [kind, setKind] = React.useState("yaml_value_equals");
  const [f, setF] = React.useState({ path: "", locator: "", expected: "", note: "" });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF((p) => ({ ...p, [k]: e.target.value }));
  return (
    <details data-testid="contract-advanced" className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium">Advanced: state the check yourself (file, key and value)</summary>
      <div className="grid gap-3 mt-3">
        <Muted>Only if the suggested check is wrong or missing. The form is blank on purpose: nothing is copied from the proposal.</Muted>
        <Field label="Kind">
          <select data-testid="field-contract-kind" className="border border-border rounded-sm bg-bg px-3 py-2 min-h-10" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="yaml_value_equals">YAML value equals</option><option value="xlsx_cell_equals">Workbook cell equals</option>
          </select>
        </Field>
        <Field label="File (path as shown in Sources)"><TextInput data-testid="field-contract-path" value={f.path} onChange={set("path")} /></Field>
        <Field label="Where in the file (key, or Sheet!Cell for a workbook)"><TextInput data-testid="field-contract-locator" value={f.locator} onChange={set("locator")} /></Field>
        <Field label="Value that must be there after the change"><TextInput data-testid="field-contract-expected" value={f.expected} onChange={set("expected")} /></Field>
        <Field label="Where this expectation comes from (for the record)"><TextInput data-testid="field-contract-note" value={f.note} onChange={set("note")} /></Field>
        <div>
          <Button variant="primary" data-testid="action-set-contract" disabled={pending} onClick={async () => {
            const body: Rec = { workspace_id: ws, proposal_job_id: ids.proposal, kind, logical_path: f.path.trim(), locator: f.locator.trim(), expected: parseValue(f.expected) };
            if (f.note.trim()) body.provenance = { note: f.note.trim() };
            if (!body.logical_path || !body.locator || !f.expected.trim()) { notify.status("Fill in the path, the key or cell and the expected value."); return; }
            if (await run(() => api("POST", "/v2/verification/contracts", body))) {
              setF({ path: "", locator: "", expected: "", note: "" });
              notify.status("Check saved. The requester can now run the review.");
              await refresh();
            }
          }}>Save this check</Button>
        </div>
      </div>
    </details>
  );
}

export function CheckStage() {
  const { ws, ids, data, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [sug, setSug] = React.useState<Suggestion | null>(null);
  const [typed, setTyped] = React.useState<Record<string, string>>({});
  const d = data;
  const proposalDone = !!d && isDone(d.proposal);
  const have = (d?.contracts ?? []).length;
  React.useEffect(() => {
    let live = true;
    if (!proposalDone || !ids.proposal) { setSug(null); return; }
    api<Suggestion>("GET", `/v2/verification/suggestion?workspace_id=${encodeURIComponent(ws)}&proposal_job_id=${encodeURIComponent(ids.proposal)}`)
      .then((x) => { if (live) setSug(x); }).catch(() => { if (live) setSug(null); });
    return () => { live = false; };
  }, [ws, ids.proposal, proposalDone, have, d?.head.source_revision_id]);
  if (!d) return null;
  const it = d.intent;
  const requester = !!d.proposal?.requested_by && d.proposal.requested_by === s.account;
  const mayConfirm = s.can("verification.contract.set") && !requester;
  const list = d.contracts ?? [];
  const gone = d.contractsStale;
  const done = list.length > 0 && !gone;
  const stated = (i: Item) => list.some((c) => c.logical_path === i.logical_path && c.locator === i.locator && !c.stale);
  const todo = (sug?.items ?? []).filter((i) => !stated(i));
  const blocked = !sug || sug.stale || todo.some((i) => i.agrees_with_proposal === false) || todo.some((i) => i.needs_human_value && !(typed[i.locator] ?? "").trim());
  const why = !sug ? "The suggested check is not loaded yet."
    : sug.stale ? `The suggestion is out of date: ${sug.stale_reason ?? "the files changed"}. Prepare a new proposal.`
    : todo.some((i) => i.agrees_with_proposal === false) ? "The proposal writes a different value than the confirmed request. Look at the red line first; the requester has to fix the request or the proposal."
    : todo.some((i) => i.needs_human_value && !(typed[i.locator] ?? "").trim()) ? "Type the value that must be there for the line marked 'you state this value'."
    : null;
  const confirm = async () => {
    if (!sug || !ids.proposal) return;
    for (const i of todo) {
      const body: Rec = { workspace_id: ws, proposal_job_id: ids.proposal, kind: i.kind, logical_path: i.logical_path, locator: i.locator,
        expected: i.needs_human_value ? parseValue(typed[i.locator]) : i.expected, provenance: { note: i.needs_human_value ? "Value stated by the approver" : i.note } };
      if (!i.needs_human_value) body.suggestion_sha256 = sug.suggestion_sha256;
      if (!(await run(() => api("POST", "/v2/verification/contracts", body)))) return;
    }
    notify.status("Checks confirmed. The requester can now run the review.");
    await refresh();
  };
  return (
    <StageCard title={stageTitle("check")} testid="flow-check" done={done}
      summary={done ? <>Check confirmed by {Array.from(new Set(list.map((c) => c.author_account_id))).join(", ")} ({list.length} {list.length === 1 ? "check" : "checks"}); open this card to read the expected value.</> : undefined}>
      <Muted>After the change is applied, Ripple reads the file again and compares it with the value below. The server proposes the check from the confirmed request; a person who did not ask for the change confirms it.</Muted>
      {it && (() => { const q = fmtForm(it); return (
        <Callout testid="confirmed-request"><strong>The confirmed request</strong><span>{q.title}: from {q.from} to {q.to}. Applies to: {q.scope}.</span></Callout>
      ); })()}
      {!proposalDone && <Muted testid="check-wait">Available once the proposal is prepared (stage {stageNo("proposal")}).</Muted>}
      {gone && <Callout tone="warn" testid="check-stale"><strong>This check no longer applies.</strong><span>It was stated for an earlier revision ({gone}). Confirm the check again for the current proposal.</span></Callout>}
      {list.map((c) => (
        <p key={c.contract_id} data-testid="contract-row" data-contract-id={c.contract_id} data-stale={String(!!c.stale)} className="text-sm">
          <strong>{basename(c.logical_path)}</strong> must say <strong>{String(c.expected)}</strong>
          <span className="text-muted"> - confirmed by {c.author_account_id}{c.stale ? " (no longer applies)" : ""}</span>
          <Tech> <code>{c.logical_path} @ {c.locator}</code> sha256 {String(c.contract_sha256 ?? "").slice(0, 12)}</Tech>
        </p>
      ))}
      {proposalDone && sug && todo.length > 0 && (
        <div data-testid="suggested-checks" className="grid gap-2 rounded-md border border-border p-3">
          <h3 className="font-semibold">Suggested check{todo.length > 1 ? "s" : ""} {mayConfirm ? "(read, then confirm)" : "(waiting for an approver)"}</h3>
          {todo.map((i) => (
            <div key={i.locator} data-testid="suggested-check" data-agrees={String(i.agrees_with_proposal)} className="grid gap-1 text-sm">
              <p><strong>{i.sentence}</strong></p>
              <Muted>{i.needs_human_value ? "There is no single confirmed value for this setting, so you state it." : "The value comes from the confirmed request, not from the proposal."}</Muted>
              {i.needs_human_value && mayConfirm && (
                <Field label="You state this value: what must be there after the change"><TextInput data-testid="field-check-value" value={typed[i.locator] ?? ""} onChange={(e) => setTyped((p) => ({ ...p, [i.locator]: e.target.value }))} /></Field>
              )}
              {i.agrees_with_proposal === false && <Callout tone="danger" testid="check-disagrees"><span>The proposal writes <strong>{String(i.proposal_shows.after)}</strong>, but the confirmed request says <strong>{String(i.expected)}</strong>.</span></Callout>}
              <Tech><code>{i.kind} · {i.logical_path} @ {i.locator}</code></Tech>
            </div>
          ))}
          <Callout testid="check-limits"><strong>What this check can and cannot show</strong>{(sug.limits ?? []).map((t, k) => <span key={k}>{t}</span>)}</Callout>
          {mayConfirm ? (
            <div className="grid gap-1">
              <div><Button variant="primary" data-testid="action-confirm-checks" disabled={pending || blocked} title={why ?? undefined} onClick={confirm}>Confirm these checks</Button></div>
              {why && <Muted testid="check-why">{why}</Muted>}
              <Muted>Confirming records your name against the check. It does not approve the change and applies nothing.</Muted>
            </div>
          ) : (
            <Muted testid="check-waiting">{requester ? "You asked for this change, so you cannot confirm its check: an independent approver does. It is in their My work list." : s.can("verification.contract.set") ? "" : "Only an approver or administrator who did not ask for this change confirms the check."}</Muted>
          )}
        </div>
      )}
      {proposalDone && sug && todo.length === 0 && list.length === 0 && <Muted testid="check-none">Ripple cannot build a check for this kind of edit automatically ({(sug.missing ?? []).join("; ") || "no edit to check"}). An approver states it under Advanced.</Muted>}
      {mayConfirm && proposalDone && <ManualForm />}
    </StageCard>
  );
}
