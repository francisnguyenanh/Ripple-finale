import * as React from "react";
import { Callout, Card, Field, Muted, TextInput, Select } from "@/components/bits";
import { Button } from "@/components/ui/button";
import { newId } from "@/lib/fmt";
import { hashFor } from "@/lib/route";
import { useNotify } from "@/lib/notify";
import { useSession } from "@/session";
import { PurposeChat } from "@/features/freetext/PurposeChat";
import { whatIfSurface } from "@/features/freetext/surfaces";
import { useParam } from "@/lib/param";
import { SimulationReport } from "./AnalysisStage";
import { SimulationAnalysisCard } from "@/features/explain/SimulationAnalysisCard";
import { useFlow } from "./flow";
import { ApproveJobButton, JobLine, useCreateStage } from "./StageBits";
import type { Rec } from "./types";
import { isDone } from "./types";

// The values Harbor's approval/matching configuration can really simulate (deterministic CLI, no model).
const WHATIF_KEYS: [string, string, string][] = [
  ["route", "routing_rules.standard_manager_threshold_jpy", "Manager approval threshold (JPY)"],
  ["route", "routing_rules.capex_finance_threshold_jpy", "CAPEX finance threshold (JPY)"],
  ["match", "matching_rules.price_tolerance_fixed_jpy", "Invoice price tolerance, fixed (JPY)"],
  ["match", "matching_rules.price_tolerance_percent", "Invoice price tolerance, percent"],
  ["match", "matching_rules.payment_term_days", "Payment terms (days)"],
];

export function WhatIfStage() {
  const { ws, data, setIds, refresh } = useFlow();
  const s = useSession();
  const notify = useNotify();
  const { create, pending } = useCreateStage();
  const [sel, setSel] = React.useState(`${WHATIF_KEYS[0][0]}|${WHATIF_KEYS[0][1]}`);
  const [val, setVal] = React.useState("");
  const [wi, setWi] = useParam("wi");
  const [wis] = useParam("wis");
  if (!s.can("change.prepare")) return null;
  const w = data?.whatif ?? null;
  const res = (w && isDone(w) ? w.execution_result : null) as Rec | null;
  const k0 = !!w;
  return (
    <Card title="What would you like to explore?" testid="flow-whatif">
      <Callout testid="whatif-note">Simulation only. Nothing is changed, no proposal is created and no document is edited. The AI only helps Ripple understand your request. The result comes from running the Harbor system itself (deterministic, no AI call), and no job starts until you confirm what Ripple understood and press Run.</Callout>
      <div className="rounded-md border border-border p-3" data-testid="whatif-chat">
        <PurposeChat ws={ws} convId={wi} onConvId={setWi} seed={wis || undefined} surface={whatIfSurface(async (c, path, value) => {
          const id = newId("ui-whatif");
          if (await create("simulation", id, { adapter: "harbor_cli", case: c, edits: [{ path, value }] })) { setIds({ whatif: id }); await refresh(); return id; }
          return null;
        })} />
      </div>
      <details data-testid="whatif-advanced" className="rounded-md border border-border p-3">
      <summary className="cursor-pointer font-semibold text-sm">Advanced input: choose the rule and type the value yourself</summary>
      <div className="grid gap-3 mt-3">
      <Field label="Rule">
        <Select data-testid="whatif-key" value={sel} onChange={(e) => setSel(e.target.value)}>
          {WHATIF_KEYS.map(([c, k, label]) => <option key={k} value={`${c}|${k}`}>{label}</option>)}
        </Select>
      </Field>
      <Field label="Proposed value"><TextInput data-testid="whatif-value" placeholder="whole number, for example 750000" value={val} onChange={(e) => setVal(e.target.value)} /></Field>
      <div>
        <Button variant="primary" data-testid="action-run-whatif" disabled={pending} onClick={async () => {
          const [c, k] = sel.split("|");
          const raw = val.trim().replace(/[ ,]/g, "");
          if (!/^\d+$/.test(raw)) { notify.status("Enter a whole number."); return; }
          const id = newId("ui-whatif");
          if (await create("simulation", id, { adapter: "harbor_cli", case: c, edits: [{ path: k, value: parseInt(raw, 10) }] })) { setIds({ whatif: id }); await refresh(); }
        }}>Run what-if simulation</Button>
      </div>
      </div>
      </details>
      {w && <JobLine name="whatif" job={w} usePhase />}
      <ApproveJobButton job={w} label="Approve this run" testid="action-approve-whatif" />
      {w && (
        <section data-testid="whatif-ran" aria-label="What was simulated" className="rounded-md border border-border p-3 grid gap-1 text-sm">
          <h3 className="font-semibold">What was simulated</h3>
          {((w.payload?.edits ?? []) as Rec[]).map((e, i) => {
            const k = WHATIF_KEYS.find(([, key]) => key === e.path);
            return <p key={i}>{k ? k[2] : "A setting"} set to <strong>{Number(e.value).toLocaleString("en-US")}</strong>, on the {w.payload?.case === "match" ? "invoice matching" : "approval routing"} cases of the Harbor program. Nothing was changed in your sources.</p>;
          })}
          {k0 && !res && <Muted>Waiting for the result.</Muted>}
        </section>
      )}
      {res && (res.harbor && res.status === "EXECUTED"
        ? <>
            <SimulationReport h={res.harbor} />
            <p className="text-sm" data-testid="whatif-explorer-link">See where this setting comes from in the <a className="text-primary underline" href={hashFor(ws, "explorer")}>Explorer</a>. Cases the simulation could not cover are listed above as not simulated or unknown: that is never the same as "no impact".</p>
          </>
        : <Callout tone="warn" testid="whatif-status">Simulation {res.status ?? "unknown"}{res.reason ? `: ${res.reason}` : ""}. This is NOT a "no impact" result.</Callout>)}
      {res && res.harbor && res.status === "EXECUTED" && w && <SimulationAnalysisCard ws={ws} simulationJobId={w.job_id} />}
      {res && res.harbor && res.status === "EXECUTED" && (() => {
        const e = ((w?.payload?.edits ?? []) as Rec[])[0];
        const k = e ? WHATIF_KEYS.find(([, key]) => key === e.path) : undefined;
        const from = ((res.harbor.edits ?? []) as Rec[]).find((x) => x.path === e?.path)?.from;
        if (!e || !k) return null;
        const text = `Please change the ${k[2]} ${from !== undefined ? `from ${Number(from).toLocaleString("en-US")} ` : ""}to ${Number(e.value).toLocaleString("en-US")}. Everything else must stay as it is.`;
        return (
          <div data-testid="whatif-request" className="rounded-md border border-border p-3 grid gap-1 text-sm">
            <p>Want this as a real change? It does not create or approve anything: it opens a NEW request with the sentence below proposed. You read what the AI understood and confirm it, and every later step needs its own people.</p>
            <p><em data-verbatim>{text}</em></p>
            <div><a data-testid="action-whatif-to-request" className="text-primary underline" href={hashFor(ws, "changes", undefined, { rqs: text.slice(0, 300) })}>Start a change request from this</a></div>
          </div>
        );
      })()}
      <Muted>Nothing here is saved as knowledge.</Muted>
    </Card>
  );
}
