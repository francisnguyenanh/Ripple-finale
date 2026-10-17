// Harbor try-out: one requisition through Harbor's OWN routing code, under the configuration of two versions of this workspace's sources, side by side.
// Read-only. The code is the pinned public Harbor copy (not a deployed Harbor application); no AI decides anything here.
import * as React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Muted, Tech, TextInput } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { useAction } from "@/lib/useAction";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const money = (n: unknown) => (typeof n === "number" ? n.toLocaleString("en-US") : "not set");
const ROUTE_WORDS: Record<string, string> = { AUTO: "approved automatically", NEEDS_MANAGER: "needs a manager", NEEDS_FINANCE: "needs finance", NEEDS_DIRECTOR: "needs a director", REJECTED: "rejected" };
const routeText = (r: unknown) => (typeof r === "string" ? `${ROUTE_WORDS[r] ?? r}` : "no route");

const BLANK: Record<string, string> = {
  request_id: "REQ-TRY-1", requester_id: "EMP-001", supplier_id: "SUP-002", process: "STANDARD", category: "OPEX", net_amount_jpy: "600000",
  currency: "JPY", requested_at: "2026-09-01T09:01:00", quantity: "10", item_description: "Trial item",
};

function Side({ title, rev, dec, run }: { title: string; rev: Rec; dec: Rec; run: Rec }) {
  return (
    <Card title={title} testid={`harbor-${title.toLowerCase()}`}>
      <p className="text-lg font-semibold" data-testid={`harbor-route-${title.toLowerCase()}`}>{routeText(dec.route)}</p>
      <Muted>Reason: <span data-verbatim>{String(dec.reason_code ?? "")}</span> · amount used: {money(dec.amount_used_jpy)} JPY</Muted>
      <Muted>Configuration: manager limit {money(rev.standard_manager_threshold_jpy)} JPY · finance limit {money(rev.capex_finance_threshold_jpy)} JPY</Muted>
      <Tech>
        <div className="mt-2 text-xs text-muted" data-testid={`harbor-tech-${title.toLowerCase()}`}>
          <div>source revision {String(rev.source_revision_id)}</div>
          <div>approval.yaml sha256 {String(rev.config_sha256)}</div>
          <div>raw decision <span data-verbatim>{JSON.stringify(dec)}</span></div>
          <div>runner: {String(run.duration_seconds)} s, inputs unchanged {String(run.input_hash_unchanged)}</div>
        </div>
      </Tech>
    </Card>
  );
}

export function HarborPage() {
  const s = useSession();
  const ws = s.route.ws!;
  const { pending, run } = useAction();
  const [cfgs, setCfgs] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState("");
  const [form, setForm] = React.useState<Record<string, string>>(BLANK);
  const [before, setBefore] = React.useState("");
  const [after, setAfter] = React.useState("");
  const [res, setRes] = React.useState<Rec | null>(null);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/harbor/configs?workspace_id=${q(ws)}`).then((c) => {
      if (!live) return;
      setCfgs(c);
      const r = (c.revisions ?? []) as Rec[];
      if (r.length) { setBefore(r[0].source_revision_id as string); setAfter(r[r.length - 1].source_revision_id as string); }
    }).catch(() => live && setErr("The configuration versions could not be read."));
    return () => { live = false; };
  }, [ws]);
  const revs = ((cfgs?.revisions ?? []) as Rec[]);
  const label = (r: Rec) => `Version ${r.order}${r.is_head ? " (current)" : ""}: manager limit ${money(r.standard_manager_threshold_jpy)} JPY`;
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  const go = async () => {
    const requisition = { ...form, net_amount_jpy: Number(form.net_amount_jpy), quantity: Number(form.quantity) };
    const r = await run(() => api<Rec>("POST", "/v2/harbor/try", { workspace_id: ws, requisition, before_revision_id: before, after_revision_id: after }));
    if (r) setRes(r);
  };
  const changed = res ? Object.keys((res.changed ?? {}) as Rec).length > 0 : false;
  const rb = res ? (res.before as Rec) : null;
  const ra = res ? (res.after as Rec) : null;
  const emergencyUnmoved = res && form.process === "EMERGENCY" && !changed
    && (res.revisions as Rec).before.standard_manager_threshold_jpy !== (res.revisions as Rec).after.standard_manager_threshold_jpy;
  return (
    <>
      <PageHeader id="harbor" next="Enter a requisition, pick two versions of the sources, and read the route Harbor's own code gives under each." />
      <Callout tone="info" testid="harbor-claim">
        This runs the <strong>pinned public Harbor code</strong> (a verified copy, not a deployed Harbor application). Changing a configuration value changes what that code returns; the AI decides nothing here and no uploaded file is run.
      </Callout>
      {err && <Callout tone="warn" role="alert">{err}</Callout>}
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <Card title="Requisition" testid="harbor-form">
          <div className="grid gap-3 sm:grid-cols-2">
            {([["request_id", "Request id"], ["requester_id", "Requester"], ["supplier_id", "Supplier"], ["net_amount_jpy", "Net amount (JPY)"], ["quantity", "Quantity"], ["requested_at", "Requested at"], ["item_description", "Item"]] as const).map(([k, l]) => (
              <Field key={k} label={l}><TextInput value={form[k]} onChange={set(k)} data-testid={`harbor-in-${k}`} /></Field>
            ))}
            <Field label="Process">
              <select className="h-9 rounded-md border border-border bg-background px-2" value={form.process} onChange={set("process")} data-testid="harbor-in-process">
                <option value="STANDARD">Standard</option><option value="EMERGENCY">Emergency</option>
              </select>
            </Field>
            <Field label="Category">
              <select className="h-9 rounded-md border border-border bg-background px-2" value={form.category} onChange={set("category")} data-testid="harbor-in-category">
                <option value="OPEX">OPEX</option><option value="CAPEX">CAPEX</option>
              </select>
            </Field>
          </div>
        </Card>
        <Card title="Versions to compare" testid="harbor-versions">
          {revs.length === 0 ? <Muted>No version of this workspace holds Harbor's approval.yaml yet.</Muted> : (
            <div className="grid gap-3">
              <Field label="Before">
                <select className="h-9 w-full rounded-md border border-border bg-background px-2" value={before} onChange={(e) => setBefore(e.target.value)} data-testid="harbor-before">
                  {revs.map((r) => <option key={String(r.source_revision_id)} value={String(r.source_revision_id)}>{label(r)}</option>)}
                </select>
              </Field>
              <Field label="After">
                <select className="h-9 w-full rounded-md border border-border bg-background px-2" value={after} onChange={(e) => setAfter(e.target.value)} data-testid="harbor-after">
                  {revs.map((r) => <option key={String(r.source_revision_id)} value={String(r.source_revision_id)}>{label(r)}</option>)}
                </select>
              </Field>
              <Button onClick={go} disabled={pending || !before || !after} data-testid="harbor-run">{pending ? "Running..." : "Run through Harbor"}</Button>
              <Tech><div className="text-xs text-muted" data-testid="harbor-code-rev">code: {Object.entries(((cfgs?.code as Rec)?.files ?? {}) as Rec).map(([f, h]) => `${f} ${String(h).slice(0, 12)}`).join(" · ")}</div></Tech>
            </div>
          )}
        </Card>
      </div>
      {res && rb && ra && (
        <section className="mt-6 grid gap-4" aria-live="polite" data-testid="harbor-result">
          <div className="flex items-center gap-2">
            <StatusBadge status={changed ? "info" : "success"} label={changed ? "The two versions give different results" : "Same result under both versions"} />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Side title="Before" rev={(res.revisions as Rec).before as Rec} dec={rb.decision as Rec} run={rb.run as Rec} />
            <Side title="After" rev={(res.revisions as Rec).after as Rec} dec={ra.decision as Rec} run={ra.run as Rec} />
          </div>
          {emergencyUnmoved && (
            <Callout tone="info" testid="harbor-emergency-note">
              The manager limit differs between the two versions, but this emergency requisition did not move: Harbor's emergency code compares against its own fixed amount (1,000,000 JPY in <span data-verbatim>app/emergency.py</span>) instead of reading the configured limit. That is a difference between code and configuration for a person to review; it is not a proven dependency.
            </Callout>
          )}
        </section>
      )}
    </>
  );
}
