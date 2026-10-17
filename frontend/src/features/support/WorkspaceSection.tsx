// What a workspace is, who may do what in it, and (for an administrator) how to start another one. Everything shown here is read from
// the server (`/v2/workspace/profile`, `/v2/workspace/create-options`); the server also decides every action, so nothing on this page grants a right.
import * as React from "react";
import { Check, Minus } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Card, Field, Muted, Tech, TechField, TextArea, TextInput } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { newId } from "@/lib/fmt";
import { useAction } from "@/lib/useAction";
import { useNotify } from "@/lib/notify";
import { hashFor } from "@/lib/route";
import { useSession } from "@/session";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;

function Terms() {
  return (
    <Card title="Organization, project, workspace and demo session" testid="workspace-terms">
      <dl className="grid gap-2 text-sm">
        <div><dt className="font-semibold inline">Organization.</dt> <dd className="inline">Keeps the data of one company or business unit apart from every other. Nothing is shared across organizations.</dd></div>
        <div><dt className="font-semibold inline">Project.</dt> <dd className="inline">Sets the policy and the AI budget boundary for the workspaces in it. Creating a workspace does not create a budget.</dd></div>
        <div><dt className="font-semibold inline">Workspace.</dt> <dd className="inline">The sources, knowledge, decisions and change history of one system or business process, with its own members. It is not one file, one question or one writing session.</dd></div>
        <div><dt className="font-semibold inline">Demo session.</dt> <dd className="inline">A time-limited slot for a group trying the product together. It only coordinates who may change things; it gives nobody extra rights and no budget.</dd></div>
      </dl>
      <Muted>Keep related policy, configuration, code and workbooks in the same workspace so they can be compared. Use a separate workspace when the sources, the members or the history must stay independent.</Muted>
    </Card>
  );
}

function Create({ onCreated }: { onCreated: () => void }) {
  const s = useSession();
  const notify = useNotify();
  const { run, pending } = useAction();
  const [opts, setOpts] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState("");
  const [desc, setDesc] = React.useState("");
  const [project, setProject] = React.useState("");
  const [picked, setPicked] = React.useState<Record<string, string>>({});
  const [made, setMade] = React.useState<Rec | null>(null);
  const key = React.useRef(newId("ws"));
  React.useEffect(() => {
    if (!open || opts) return;
    api<Rec>("GET", "/v2/workspace/create-options").then((o) => { setOpts(o); setProject((o.projects?.[0]?.project_id as string) ?? ""); }).catch((e) => { if (e instanceof ApiError) setErr(e); else throw e; });
  }, [open, opts]);
  const proj = ((opts?.projects ?? []) as Rec[]).find((p) => p.project_id === project);
  const roles = ((opts?.roles ?? []) as Rec[]);
  const people = ((proj?.accounts ?? []) as string[]).filter((a) => a !== s.account);
  const ok = name.trim().length >= 3 && name.trim().length <= 80 && !!project;
  return (
    <Card title="Start another workspace" testid="workspace-create">
      <Muted>Only an administrator of a project can do this. The new workspace starts empty: you add its source files next. You become its administrator and choose which existing people get which role.</Muted>
      {!open && <div><Button data-testid="workspace-create-open" onClick={() => setOpen(true)}>Create a workspace</Button></div>}
      {open && err && <Callout tone="danger" testid="workspace-create-denied">{err.status === 403 ? "Your account does not administer any project, so it cannot create a workspace." : `The options could not be read (HTTP ${err.status}).`}</Callout>}
      {open && !opts && !err && <StatusBadge status="loading" />}
      {open && opts && !made && (
        <form className="grid gap-3" onSubmit={async (e) => {
          e.preventDefault();
          const members = Object.entries(picked).map(([account_id, role]) => ({ account_id, roles: [role] }));
          const r = await run(() => api<Rec>("POST", "/v2/workspaces", { project_id: project, name: name.trim(), description: desc.trim(), members, idempotency_key: key.current }));
          if (r) { setMade(r); notify.status(`Workspace "${r.display_name}" created.`); onCreated(); }
        }}>
          <Field label="Workspace name" hint="3 to 80 characters, for example Harbor Treasury"><TextInput data-testid="workspace-name" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="What is it for?" hint="Optional: the system or process, and which sources belong here"><TextArea data-testid="workspace-description" value={desc} maxLength={500} rows={3} onChange={(e) => setDesc(e.target.value)} /></Field>
          {((opts.projects ?? []) as Rec[]).length > 1 ? (
            <Field label="Project"><select data-testid="workspace-project" className="border border-border rounded-sm bg-bg px-2 py-1.5 min-h-9" value={project} onChange={(e) => { setProject(e.target.value); setPicked({}); }}>
              {((opts.projects ?? []) as Rec[]).map((p) => <option key={p.project_id} value={p.project_id}>{p.project_id}</option>)}
            </select></Field>
          ) : <Muted>Project: <strong>{project}</strong></Muted>}
          <fieldset className="grid gap-2">
            <legend className="text-sm font-semibold">Who else gets access</legend>
            <Muted>Only people who already belong to this project are listed. Each person gets one role bundle; what it allows is explained above.</Muted>
            {people.length === 0 && <Muted>Nobody else belongs to this project yet.</Muted>}
            {people.map((a) => (
              <div key={a} className="flex flex-wrap items-center gap-3 text-sm">
                <label className="flex items-center gap-2"><input type="checkbox" data-testid="workspace-member" data-account={a} checked={a in picked} onChange={(e) => setPicked((p) => { const n = { ...p }; if (e.target.checked) n[a] = roles[0]?.role as string; else delete n[a]; return n; })} />{a}</label>
                {a in picked && (
                  <select aria-label={`Role for ${a}`} data-testid="workspace-member-role" className="border border-border rounded-sm bg-bg px-2 py-1 min-h-9" value={picked[a]} onChange={(e) => setPicked((p) => ({ ...p, [a]: e.target.value }))}>
                    {roles.map((r) => <option key={r.role} value={r.role}>{r.label}</option>)}
                  </select>
                )}
              </div>
            ))}
          </fieldset>
          <div><Button type="submit" variant="primary" data-testid="workspace-create-submit" disabled={!ok || pending}>Create workspace</Button></div>
        </form>
      )}
      {made && (
        <Callout tone="info" testid="workspace-created">
          <span><strong>{made.display_name}</strong> was created with {((made.members ?? []) as Rec[]).length} other member(s). It has no sources yet.</span>
          <div><Button variant="primary" asChild><a data-testid="workspace-created-open" href={hashFor(made.workspace_id as string, "sources")}>Open it and add source files</a></Button></div>
        </Callout>
      )}
    </Card>
  );
}

/** Members and the per-member AI switch. Switching AI use off only adds the marker role `ai_suspended`; every other right stays. The server decides. */
function Members({ ws, members, roleName, me, onChanged }: { ws: string; members: Rec[]; roleName: (id: string) => string; me: string; onChanged: () => void }) {
  const { run, pending } = useAction();
  const notify = useNotify();
  const toggle = async (m: Rec, off: boolean) => {
    const cur = (m.roles as string[]).filter((r) => r !== "ai_suspended");
    const r = await run(() => api<Rec>("POST", "/v2/membership/grant", { workspace_id: ws, account_id: m.account_id, roles: off ? [...cur, "ai_suspended"] : cur }));
    if (r) { notify.status(off ? `AI use is switched off for ${m.account_id}.` : `AI use is switched on for ${m.account_id}.`); onChanged(); }
  };
  return (
    <Card title="Members" testid="workspace-members">
      <ul className="text-sm grid gap-2">
        {members.map((m) => {
          const roles = m.roles as string[];
          const off = roles.includes("ai_suspended");
          const fixed = roles.includes("admin") || m.account_id === me;      // an administrator's own bundle is set up by the operator, not here
          return (
            <li key={m.account_id as string} data-testid="workspace-member-row" data-account={m.account_id as string} data-ai={off ? "off" : "on"} className="flex flex-wrap items-center gap-3">
              <span><strong>{m.account_id as string}</strong> - {roles.filter((r) => r !== "ai_suspended").map(roleName).join(", ")}</span>
              <span className="text-muted">AI use: {off ? "switched off" : "allowed by role"}</span>
              {!fixed && <Button data-testid={off ? "member-ai-on" : "member-ai-off"} disabled={pending} onClick={() => void toggle(m, !off)}>{off ? "Switch AI use on" : "Switch AI use off"}</Button>}
            </li>
          );
        })}
      </ul>
      <Muted>AI use is part of a role (requester, reviewer, approver). Switching it off for one person stops their AI chat, drafts and questions and nothing else; saved results stay readable.</Muted>
    </Card>
  );
}

export function WorkspaceSection({ ws }: { ws: string }) {
  const s = useSession();
  const [p, setP] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const load = React.useCallback(() => {
    api<Rec>("GET", `/v2/workspace/profile?workspace_id=${q(ws)}`).then((x) => { setP(x); setErr(null); }).catch((e) => { if (e instanceof ApiError) setErr(e); else throw e; });
  }, [ws]);
  React.useEffect(() => { setP(null); load(); }, [load]);
  if (err) return <Callout tone="danger" testid="state-error">The workspace details could not be read (HTTP {err.status}). <Button onClick={load}>Try again</Button></Callout>;
  if (!p) return <StatusBadge status="loading" />;
  const roles = (p.roles ?? []) as Rec[];
  const steps = (p.steps ?? []) as Rec[];
  const members = (p.members ?? null) as Rec[] | null;
  const title = (p.display_name as string) || (p.workspace_id as string);
  const roleName = (id: string) => (roles.find((r) => r.role === id)?.label as string) ?? id;
  return (
    <div className="grid gap-4">
      <Card title="About this workspace" testid="workspace-about">
        <p className="text-lg font-semibold" data-testid="workspace-name-shown">{title}</p>
        {p.description ? <p className="text-sm" data-testid="workspace-description-shown">{p.description}</p> : <Muted>No description was written for this workspace.</Muted>}
        <ul className="text-sm grid gap-1">
          <li><span className="text-muted">Project:</span> {p.project_id}</li>
          <li data-testid="workspace-source-count"><span className="text-muted">Source files imported:</span> {p.source_files}</li>
          <li><span className="text-muted">Your role here:</span> {(p.my_roles as string[]).length ? (p.my_roles as string[]).map(roleName).join(", ") : "none"}</li>
          {p.created_by ? <li><span className="text-muted">Created by:</span> {p.created_by}</li> : null}
          {(p.same_project as Rec[]).length > 0 && <li><span className="text-muted">Other workspaces in this project:</span> {(p.same_project as Rec[]).map((w) => (w.display_name as string) || (w.workspace_id as string)).join(", ")}</li>}
        </ul>
        <Tech><TechField label="workspace id" value={p.workspace_id} /></Tech>
      </Card>
      <Terms />
      <Card title="Who can do what in this workspace" testid="workspace-steps">
        <ul className="grid gap-1 text-sm" aria-label="Your rights for each step">
          {steps.map((st) => (
            <li key={st.id as string} data-testid="workspace-step" data-step={st.id as string} data-allowed={String(st.allowed)} className="flex items-center gap-2">
              {st.allowed ? <Check size={16} aria-hidden="true" className="text-success" /> : <Minus size={16} aria-hidden="true" className="text-muted" />}
              <span>{st.label as string}: <strong>{st.allowed ? "you can" : "you cannot"}</strong></span>
            </li>
          ))}
        </ul>
        <Muted>The person who asks for a change is never the person who approves it. Roles are what an administrator granted; a request, a link or a chat message that claims a role gives none.</Muted>
        <details className="text-sm">
          <summary className="cursor-pointer font-semibold">What each role allows</summary>
          <dl className="grid gap-2 mt-2">
            {roles.map((r) => (
              <div key={r.role as string} data-testid="workspace-role" data-role={r.role as string}>
                <dt className="font-semibold">{r.label as string}{r.grantable ? "" : " (set up by the operator, not grantable here)"}</dt>
                <dd className="text-muted">{(r.can as string[]).join("; ")}</dd>
              </div>
            ))}
          </dl>
        </details>
      </Card>
      {members && <Members ws={ws} members={members} roleName={roleName} me={s.account} onChanged={load} />}
      {s.can("membership.manage") && <Create onCreated={() => { void s.reloadWorkspaces(); }} />}
    </div>
  );
}
