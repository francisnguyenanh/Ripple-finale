// Support & Access: who may do what here, the writing session of a shared sandbox, the bounded job
// runner and spend, and what the Harbor demo supports. Read-only except the lease/pump controls,
// which the server enforces on every write.
import * as React from "react";
import { Ban, CircleHelp, Hammer, Pencil, ShieldCheck, X } from "lucide-react";
import { api, ApiError, type SandboxStatus } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Muted, Tech, TextInput } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { humanize } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { useParam } from "@/lib/param";
import { SectionNav } from "@/components/SectionNav";
import type { Rec } from "@/features/changes/types";
import { WorkspaceSection } from "./WorkspaceSection";
import { OperatorSwitches } from "./OperatorSwitches";
import { LiveStatus } from "./LiveStatus";

const q = encodeURIComponent;

const PERMISSION_WORDS: Record<string, string> = {
  "project.read": "See everything in this workspace",
  "source.import": "Add sources, and ask for AI runs and analyses",
  "change.prepare": "Prepare a change up to a proposal (draft, impact, simulation)",
  "intent.create": "Describe a change in your own words",
  "knowledge.confirm": "Confirm requests and what the AI read",
  "knowledge.answer": "Answer open questions",
  "conflict.resolve": "Decide which side of a conflict is authoritative",
  "change.approve": "Approve or reject a prepared change",
  "change.accept_result": "Accept, or send back, the result of an applied change",
  "verification.contract.set": "State the expected result a change is checked against",
  "source.restore": "Restore an earlier version of the sources",
  "evidence.read": "Read the original quotes from source files",
  "membership.manage": "Manage who has access to this workspace",
};

const ICON = { supported: ShieldCheck, manual: Pencil, final: Hammer, "not-controlled": Ban, "not-supported": X, unknown: CircleHelp } as const;
const LABEL: Record<string, string> = {
  supported: "supported", manual: "manual", final: "needs a code change (final product)", "not-controlled": "the configuration does not control this",
  "not-supported": "not supported", unknown: "unknown / not rehearsed",
};
const CAPS: [string, string][] = [["view", "View"], ["simulate", "Simulate (before/after)"], ["apply", "Apply (typed edit)"], ["analyse", "Analyse only"]];

function Lease({ st, ws, onChanged }: { st: SandboxStatus & Rec; ws: string; onChanged: (s: SandboxStatus & Rec) => void }) {
  const { run, pending } = useAction();
  const [participants, setParticipants] = React.useState("");
  const s = useSession();
  const [people, setPeople] = React.useState<string[]>([]);
  const canList = s.can("membership.manage");
  React.useEffect(() => {
    if (!canList) return;
    api<Rec>("GET", `/v2/workspace/profile?workspace_id=${q(ws)}`).then((p) => setPeople(((p.members ?? []) as Rec[]).map((m) => m.account_id as string).filter((a) => a !== s.account))).catch(() => setPeople([]));
  }, [ws, canList, s.account]);
  const chosen = participants.split(",").map((x) => x.trim()).filter(Boolean);
  const toggle = (a: string) => setParticipants((chosen.includes(a) ? chosen.filter((x) => x !== a) : [...chosen, a]).join(", "));
  const L = st.lease as Rec | null | undefined;
  const held = !!L?.active;
  const mine = !!st.i_can_write;
  const act = async (action: string) => {
    const parts = participants.split(",").map((x) => x.trim()).filter(Boolean);
    const r = await run(() => api<SandboxStatus & Rec>("POST", "/v2/sandbox/lease", { workspace_id: ws, action, participants: parts }));
    if (r) onChanged(r);
  };
  const until = L?.expires_at ? new Date(L.expires_at).toLocaleString() : "";
  return (
    <Card title="Demo session" testid="sandbox-lease">
      <p data-testid="sandbox-lease-state" data-state={!held ? "FREE" : mine ? "MINE" : "OTHER"} className="text-sm">
        {!held ? "No demo session is running: nobody may change anything." : mine ? `Your demo session runs until ${until}. You and your participants may change things.` : `Another demo session is running until ${until}; you can read only.`}
      </p>
      {held && <Muted testid="sandbox-lease-holder">Held by {L?.holder}; participants: {(L?.participants ?? []).length ? (L!.participants as string[]).join(", ") : "none"}; ends {until}.</Muted>}
      <Muted>A demo session is a time-limited slot for a group trying the product together. Creating or approving anything needs it, and only one runs at a time; everyone with access can read. It coordinates the demo only: it gives nobody extra rights, no approval and no budget, and the person who asks for a change still cannot approve it.</Muted>
      {(!held || mine) && (
        <div className="grid gap-2">
          {people.length > 0 && (
            <fieldset className="grid gap-1">
              <legend className="text-sm font-semibold">Who takes part with you</legend>
              <div className="flex flex-wrap gap-2">
                {people.map((a) => <Button key={a} type="button" data-testid="sandbox-person" aria-pressed={chosen.includes(a)} variant={chosen.includes(a) ? "primary" : "secondary"} onClick={() => toggle(a)}>{a}</Button>)}
              </div>
            </fieldset>
          )}
          <Field label={people.length ? "Or type account IDs (comma separated)" : "Other accounts in your session (comma separated, for example your approver)"}>
            <TextInput data-testid="sandbox-participants" value={participants} onChange={(e) => setParticipants(e.target.value)} />
          </Field>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {!held && <Button variant="primary" data-testid="sandbox-acquire" disabled={pending} onClick={() => act("acquire")}>Start a demo session</Button>}
        {mine && <Button data-testid="sandbox-renew" disabled={pending} onClick={() => act("renew")}>Extend the session</Button>}
        {mine && <Button data-testid="sandbox-release" disabled={pending} onClick={() => act("release")}>End the session</Button>}
      </div>
    </Card>
  );
}

function SandboxSection({ ws }: { ws: string }) {
  const notify = useNotify();
  const { run, pending } = useAction();
  const s = useSession();
  const [st, setSt] = React.useState<(SandboxStatus & Rec) | null>(null);
  const [off, setOff] = React.useState(false);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const load = React.useCallback(async () => {
    try { setSt(await api<SandboxStatus & Rec>("GET", `/v2/sandbox/status?workspace_id=${q(ws)}`)); setOff(false); setErr(null); }
    catch (e) { if (e instanceof ApiError && e.status === 404) setOff(true); else if (e instanceof ApiError) setErr(e); else throw e; }
  }, [ws]);
  React.useEffect(() => { void load(); }, [load]);
  const sessionRefresh = s.refresh;
  const changed = (r: SandboxStatus & Rec) => { setSt(r); void sessionRefresh(); };
  // While the writer has queued jobs the status is looked at again, a bounded number of times.
  const queued = st?.jobs?.queued ?? 0;
  React.useEffect(() => {
    if (!st?.i_can_write || queued === 0) return;
    const t = window.setTimeout(() => void load(), 4000);
    return () => window.clearTimeout(t);
  }, [st, queued, load]);

  if (off) return <Muted testid="sandbox-off">This workspace does not use demo sessions: ordinary permissions apply and none is needed.</Muted>;
  if (err) return <Callout tone="danger" testid="state-error">The writing-session status could not be read (HTTP {err.status}). <Button onClick={() => void load()}>Try again</Button></Callout>;
  if (!st) return <StatusBadge status="loading" />;
  const mine = !!st.i_can_write;
  const sp = (st.spend ?? {}) as Rec;
  const jobs = (st.jobs ?? {}) as Rec;
  const lim = (st.limits ?? {}) as Rec;
  return (
    <div className="grid gap-4">
      <Lease st={st} ws={ws} onChanged={changed} />
      <Card title="Jobs" testid="sandbox-jobs">
        <p className="text-sm">{jobs.queued} queued, {jobs.running} running, {jobs.waiting_for_approval} waiting for a different person to approve. Jobs used {jobs.used_in_lease} of {jobs.limit_per_lease} in this session; AI-calling jobs {jobs.provider_used_in_lease} of {jobs.provider_limit_per_lease} (the re-analysis after an apply counts).</p>
        <div className="flex flex-wrap gap-2">
          <Button data-testid="sandbox-pump" disabled={!mine || jobs.queued === 0 || pending} onClick={async () => {
            const r = await run(() => api<Rec>("POST", "/v2/sandbox/pump", { workspace_id: ws }));
            if (r) { notify.status(r.busy ? "Another run is in progress; try again in a moment." : `${r.processed} job(s) run (at most ${r.bounded_by?.max_jobs} per click).`); await load(); }
          }}>Run queued jobs (bounded)</Button>
          <Button onClick={() => void load()}>Refresh</Button>
        </div>
        <Muted>Runs at most {lim.pump_max_jobs} queued jobs (about {lim.pump_seconds} s) per click. It never approves a job and never raises a budget.</Muted>
      </Card>
      <Card title="Spend" testid="sandbox-spend">
        <p className="text-sm">Settled {sp.settled_usd} USD{sp.cap_usd !== null && sp.cap_usd !== undefined ? ` of ${sp.cap_usd} USD (remaining ${sp.remaining_usd})` : "; cap not available"}</p>
        <Muted>{sp.note}. Viewing the graph calls no AI model; an AI job shows its cost when it settles. Jobs stop when the remaining budget is below {lim.min_remaining_usd} USD.</Muted>
      </Card>
      <Card title="Reset to the seed" testid="sandbox-reset">
        <p data-testid="sandbox-at-seed" className="text-sm">{st.seed_revision_id ? (st.at_seed ? "The sources are at the starting version." : "The sources have moved on from the starting version.") : "No starting version is recorded."}</p>
        <Tech><Muted>seed {st.seed_revision_id ?? "none"}; head {st.head_source_revision_id ?? "none"}</Muted></Tech>
        <Muted>A reset is the existing history restore toward the starting version: a preview, then approval by a different person. History, audit and spend are kept.</Muted>
        {st.seed_revision_id && !st.at_seed && s.can("source.restore") && (
          <div><Button data-testid="sandbox-prepare-reset" onClick={() => { location.hash = `#/ws/${q(ws)}/history?restore=${q(st.seed_revision_id as string)}`; }}>Prepare reset in History</Button></div>
        )}
      </Card>
    </div>
  );
}

function Access() {
  const s = useSession();
  const perms = s.workspaces.find((w) => w.workspace_id === s.route.ws)?.permissions ?? [];
  return (
    <Card title="Your account's access" testid="access">
      <p className="text-sm">You are signed in as <strong>{s.account}</strong>{s.roles.length ? <> with the role <strong>{s.roles.join(", ")}</strong></> : null}.</p>
      <Muted>The server decides on every action. A request, a link or a chat message that claims a role gives no rights. These are the things your account can do here:</Muted>
      <ul className="list-disc pl-5 text-sm grid gap-1">
        {perms.map((p) => <li key={p}>{PERMISSION_WORDS[p] ?? humanize(p)}<Tech> <code>{p}</code></Tech></li>)}
      </ul>
      <Callout testid="handoff-rules">
        <strong>Steps that need another person</strong>
        <span>Allowing the AI to reply (an administrator) - confirming a request (an administrator) - allowing an AI draft (a second person) - stating the expected result (an approver who did not request the change) - approving a change (an approver). Nobody can do these for themselves. Copy the request link on the Changes page to hand a step over; the link carries no password or rights.</span>
      </Callout>
    </Card>
  );
}

function Matrix({ ws }: { ws: string }) {
  const [m, setM] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    api<Rec>("GET", `/v2/support/matrix?workspace_id=${q(ws)}`).then(setM).catch((e) => { if (e instanceof ApiError) setErr(e); });
  }, [ws]);
  if (err) return <Callout tone="danger" testid="state-error">The support matrix could not be read (HTTP {err.status}).</Callout>;
  if (!m) return <StatusBadge status="loading" />;
  const cell = (c: Rec) => {
    const Icon = ICON[c.state as keyof typeof ICON] ?? CircleHelp;
    return (
      <td key={c.state + String(c.note)} data-state={c.state} className="p-2 align-top">
        <span className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs"><Icon size={12} aria-hidden="true" />{LABEL[c.state] ?? c.state}</span>
        {c.note && <div className="text-xs text-muted mt-1">{c.note}</div>}
      </td>
    );
  };
  return (
    <div className="grid gap-3">
      <Muted>What you can view, simulate, apply or only analyse. Nothing outside this table is promised.</Muted>
      <Muted testid="support-biz-note">Where a cell says the configuration does not control the behaviour, changing the setting would not change what the system does: the behaviour is written in the program itself, so changing it needs a code change, planned for the final product. The Technical view shows the code lines that prove each statement.</Muted>
      <Muted testid="support-ai">{m.ai}</Muted>
      <Muted testid="support-isolation">{m.isolation}</Muted>
      <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="What each Harbor case supports (scrolls sideways on a small screen)">
        <table data-testid="support-matrix" className="text-sm border border-border w-full">
          <caption className="sr-only">What each Harbor case supports</caption>
          <thead><tr className="bg-surface-2 text-left"><th className="p-2">Case</th>{CAPS.map(([, t]) => <th key={t} className="p-2">{t}</th>)}<TechTh /></tr></thead>
          <tbody>
            {((m.rows ?? []) as Rec[]).map((r) => (
              <tr key={r.id} data-testid="support-row" data-row={r.id} className="border-t border-border">
                <td className="p-2 align-top"><strong>{r.title}</strong><Tech><div className="text-xs text-muted">{((r.where ?? []) as string[]).join("; ")}</div></Tech></td>
                {CAPS.map(([k]) => cell(r[k]))}
                <TechTd r={r} />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Card title="Legend">
        <ul className="list-disc pl-5 text-sm">{Object.entries((m.legend ?? {}) as Rec).map(([k, v]) => <li key={k}>{LABEL[k] ?? k} - {String(v)}</li>)}</ul>
      </Card>
    </div>
  );
}
const TechTh = () => <Tech><th className="p-2">Code evidence and proof</th></Tech>;
const TechTd = ({ r }: { r: Rec }) => (
  <Tech>
    <td className="p-2 align-top text-xs text-muted">
      {((r.code_evidence ?? []) as Rec[]).map((c, i) => <div key={i}>{c.file}:{c.line} {c.text}</div>)}
      {((r.traces ?? []) as Rec[]).map((t, i) => <div key={i} data-testid="support-trace">trace: {t.claim} ({t.matches} match{t.matches === 1 ? "" : "es"})</div>)}
      {((r.tests ?? []) as string[]).map((t, i) => <div key={i}>test: {t.split("/").pop()}</div>)}
    </td>
  </Tech>
);

const SUPPORT_SECTIONS = [
  { id: "ws", label: "This workspace" }, { id: "access", label: "Access" }, { id: "sandbox", label: "Demo session" }, { id: "matrix", label: "What this demo supports" },
  { id: "live", label: "Live updates" },
];

export function SupportPage() {
  const s = useSession();
  const ws = s.route.ws!;
  const [uiSection, setUiSection] = useParam("ui_section");
  const sel = SUPPORT_SECTIONS.some((x) => x.id === uiSection) ? uiSection : null;
  const show = (id: string) => sel === null || sel === id;
  return (
    <>
      <PageHeader id="support" next="Start a demo session before creating or approving anything in a shared sandbox." />
      <SectionNav ariaLabel="Support sections" testid="support-sections" prefix="ssec" sections={SUPPORT_SECTIONS} selected={sel} onSelect={setUiSection} />
      <div className="grid gap-8">
        <section aria-labelledby="sup-ws" hidden={!show("ws")} data-testid="ssection-ws" className="grid gap-4"><h2 id="sup-ws" className="text-lg font-semibold">This workspace</h2><WorkspaceSection ws={ws} /><OperatorSwitches /></section>
        <section aria-labelledby="sup-access" hidden={!show("access")} data-testid="ssection-access" className="grid gap-4"><h2 id="sup-access" className="text-lg font-semibold">Access</h2><Access /></section>
        <section aria-labelledby="sup-sandbox" hidden={!show("sandbox")} data-testid="ssection-sandbox" className="grid gap-4"><h2 id="sup-sandbox" className="text-lg font-semibold">Demo session</h2><SandboxSection ws={ws} /></section>
        <section aria-labelledby="sup-matrix" hidden={!show("matrix")} data-testid="ssection-matrix" className="grid gap-4"><h2 id="sup-matrix" className="text-lg font-semibold">What this demo supports</h2><Matrix ws={ws} /></section>
        <section aria-labelledby="sup-live" hidden={!show("live")} data-testid="ssection-live" className="grid gap-4"><h2 id="sup-live" className="text-lg font-semibold">Live updates</h2><LiveStatus ws={ws} /></section>
      </div>
    </>
  );
}
