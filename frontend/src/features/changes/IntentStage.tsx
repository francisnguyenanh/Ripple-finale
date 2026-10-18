import { stageTitle } from "./registry";
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Muted, StatePill, TechField, TextArea, TextInput, Select } from "@/components/bits";
import { basename, intentPhrase, ruleTitle, scopeText, valueText } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { replaceHash } from "@/lib/route";
import { useSession } from "@/session";
import { ChatPanel } from "./ChatPanel";
import { useFlow } from "./flow";
import type { Intent, Rec } from "./types";

function AdvancedInput() {
  const { ws, data, setIds, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const claims = data?.claims ?? [];
  const ruleId = s.route.params.rule;
  const rc = claims.find((c) => c.candidate_id === ruleId);
  const [open, setOpen] = React.useState(!!ruleId);
  const [f, setF] = React.useState({ subject: "", scopeKey: "", scopeVal: "", unit: "", currency: "", old: "", neu: "", applies: "" });
  const prefilled = React.useRef("");
  React.useEffect(() => {
    if (rc && prefilled.current !== rc.candidate_id) {
      prefilled.current = rc.candidate_id;
      const [sk, sv] = Object.entries((rc.scope ?? {}) as Rec)[0] ?? ["", ""];
      const cur = /^[A-Z]{3}$/.test(rc.unit ?? "");
      setF((p) => ({ ...p, subject: rc.subject ?? "", scopeKey: sk, scopeVal: String(sv ?? ""), unit: cur ? "" : rc.unit ?? "", currency: cur ? rc.unit : "", old: rc.value === null || rc.value === undefined ? "" : String(rc.value) }));
      setOpen(true);
    }
  }, [rc]);
  const [text, setText] = React.useState("");
  const [target, setTarget] = React.useState("");
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF((p) => ({ ...p, [k]: e.target.value }));

  const saveStructured = async () => {
    const form: Rec = {
      subject: f.subject.trim(), unit: f.unit.trim(), currency: f.currency.trim(), old_value: f.old.trim(), new_value: f.neu.trim(),
      applies_to: f.applies.split(",").map((x) => x.trim()).filter(Boolean),
    };
    if (f.scopeKey.trim() && f.scopeVal.trim()) form.scope = { [f.scopeKey.trim()]: f.scopeVal.trim() };
    if (rc && prefilled.current === rc.candidate_id) form.target_candidate_id = rc.candidate_id;
    const r = await run(() => api<Intent>("POST", "/v2/intent/parse", { workspace_id: ws, structured_form: form }), { stale: true });
    if (!r) return;
    setIds({ intent: r.intent_id });
    await refresh();
  };
  const parseFree = async () => {
    const raw = text.trim();
    if (!raw) { notify.status("Describe the change first."); return; }
    const r = await run(() => api<Intent>("POST", "/v2/intent/parse", { workspace_id: ws, raw_request: raw, ...(target ? { target_hint: target } : {}) }), { stale: true });
    if (!r) return;
    setText(""); setTarget("");
    setIds({ intent: r.intent_id });
    await refresh();
  };

  return (
    <details data-testid="advanced-input" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium">Advanced input: fill in the form yourself</summary>
      <div className="grid gap-4 mt-3">
        <Muted>The same checks apply. Use this if you prefer exact fields, or to start from a rule.</Muted>
        {rc && <Callout testid="prefill-note">Prefilled from the rule you picked in the Explorer ({basename(rc.logical_path)}). Check every field and type the new value yourself; nothing is submitted or confirmed until you save the request and a person with confirm rights confirms it.</Callout>}
        {rc && (
          <div data-testid="prefill-summary" className="rounded-md border border-border bg-surface-2 p-3 grid gap-1">
            <p className="text-base font-semibold">{ruleTitle(rc.subject, rc.predicate)}: {valueText(rc.value, rc.unit, rc.unit)}</p>
            <Muted>Applies to: {scopeText(rc.scope)}. Source: {basename(rc.logical_path)}.</Muted>
          </div>
        )}
        {!rc && claims.length > 0 && (
          <Field label="Start from a rule" hint="The form below is then filled in for you. You only type the new value and check the rest.">
            <Select data-testid="start-rule" value="" onChange={(e) => { if (e.target.value) replaceHash(location.hash + (location.hash.includes("?") ? "&" : "?") + "rule=" + encodeURIComponent(e.target.value)); }}>
              <option value="">Choose a rule to change...</option>
              {claims.map((c) => <option key={c.candidate_id} value={c.candidate_id}>{ruleTitle(c.subject, c.predicate)} - {valueText(c.value, c.unit, c.unit)} ({basename(c.logical_path)})</option>)}
            </Select>
          </Field>
        )}
        <h4 className="font-semibold">Your request (exact values are kept as typed)</h4>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="What is changing"><TextInput data-testid="sf-subject" placeholder="for example: Manager approval limit" value={f.subject} onChange={set("subject")} /></Field>
          <Field label="What the amount measures"><TextInput data-testid="sf-unit" placeholder="for example: net goods amount" value={f.unit} onChange={set("unit")} /></Field>
          <Field label="Applies to - kind of scope"><TextInput data-testid="sf-scope-key" placeholder="for example: order_type" value={f.scopeKey} onChange={set("scopeKey")} /></Field>
          <Field label="Applies to - which one"><TextInput data-testid="sf-scope-val" placeholder="for example: standard" value={f.scopeVal} onChange={set("scopeVal")} /></Field>
          <Field label="Currency"><TextInput data-testid="sf-currency" placeholder="JPY" value={f.currency} onChange={set("currency")} /></Field>
          <Field label="Current value (as you know it)"><TextInput data-testid="sf-old" placeholder="1,000,000" value={f.old} onChange={set("old")} /></Field>
          <Field label="New value you want"><TextInput data-testid="sf-new" placeholder="500,000" value={f.neu} onChange={set("neu")} /></Field>
          <Field label="Which orders you expect this to cover (comma separated)"><TextInput data-testid="sf-applies" placeholder="standard, emergency" value={f.applies} onChange={set("applies")} /></Field>
        </div>
        <div><Button variant="primary" data-testid="action-parse-structured" disabled={pending} onClick={saveStructured}>Save the request</Button></div>
        <h4 className="font-semibold">Or describe the change in free text (older path)</h4>
        <Field label="Describe the change"><TextArea id="fld-intent-text" rows={3} value={text} onChange={(e) => setText(e.target.value)} /></Field>
        <Field label="Rule this change applies to">
          <Select data-testid="field-intent-target" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Select the rule to change</option>
            {claims.map((c) => <option key={c.candidate_id} value={c.candidate_id}>{c.subject} / {c.predicate}{c.value !== null && c.value !== undefined ? ` = ${c.value}` : ""}</option>)}
          </Select>
        </Field>
        {claims.length === 0 && <Muted>No extracted rule exists yet. Analyse a source and confirm a rule first.</Muted>}
        <div><Button data-testid="action-parse-intent" title={s.aiOff ? "AI actions are switched off on this server" : undefined} disabled={pending || s.aiOff} onClick={parseFree}>Parse intent</Button></div>
      </div>
    </details>
  );
}

export function IntentStage() {
  const { ws, ids, data, refresh } = useFlow();
  const s = useSession();
  const { run, pending } = useAction();
  const it = data?.intent ?? null;
  if (!ids.intent || !it) {
    if (!s.can("intent.create")) return <Card title={stageTitle("request")} testid="flow-intent"><Muted>You cannot start a change in this workspace.</Muted></Card>;
    return (
      <Card title={stageTitle("request")} testid="flow-intent">
        <ChatPanel />
        <AdvancedInput />
      </Card>
    );
  }
  const shown = it.status === "PARSED" ? "DRAFT" : it.status;
  // The person who made the request confirms it (server-checked: owner only); a knowledge confirmer may confirm any.
  const canConfirm = s.can("knowledge.confirm") || (s.can("intent.confirm_own") && !!it.created_by && it.created_by === s.account);
  const revising = !!data?.chat && !data.chat.unavailable && data.chat.status === "OPEN" && !!data.chat.intent_id;
  return (
    <Card title={stageTitle("request")} testid="flow-intent">
      {revising && <ChatPanel />}
      <p className="text-sm">Status: <StatePill state={shown} label={intentPhrase(shown)} testid="state-intent" data={{ "server-status": it.status }} /></p>
      <TechField label="request text" value={it.raw_request ?? ""} />
      <TechField label="target rule" value={it.target ?? "none grounded"} />
      {(it.questions ?? []).map((q, i) => <Callout key={i} tone="warn">{String(q)}</Callout>)}
      {it.status === "PARSED" && canConfirm && (
        <div>
          <Button variant="primary" data-testid="action-confirm-intent" disabled={pending}
            onClick={async () => { if (await run(() => api("POST", "/v2/intent/confirm", { workspace_id: ws, intent_id: it.intent_id }), { stale: true })) await refresh(); }}>Confirm intent</Button>
        </div>
      )}
      {it.status === "PARSED" && !canConfirm && <Muted testid="intent-owner-only">Only {it.created_by ?? "the person who made this request"} can confirm this request before analysis.</Muted>}
    </Card>
  );
}
