// Code for a business change (ext/1 UI, X1a + X1b). X1a: the recommendations of a CONFIRMED change, read-only: what the code should do, never a diff, nothing
// prepared or changed. X1b: after the knowledge commit, a candidate patch on a disposable clone of a declared repository: build, validate, approve or reject
// bound to both hashes, publish only with the repository's pull_request grant. Every button follows the server's `can` hint (same checks as the action); the
// server checks again when the action is sent. Refusals are worded from stable codes; the server's own text is never shown as the explanation.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Field, Muted, Select, StatePill, Tech, TechField, TextArea } from "@/components/bits";
import { humanize } from "@/lib/fmt";
import { refusalOf, refusalWords } from "@/lib/refusal";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;
const short = (h: unknown) => (typeof h === "string" && h ? `${h.slice(0, 12)}…` : "none");
const VALIDATION_WORDS: Record<string, string> = { PASSED: "Validation passed", FAILED: "Validation failed", NOT_RUN: "Validation not run: the repository declares no command, so this is not a pass", INFRASTRUCTURE_FAILED: "Validation could not run (infrastructure): not a result about the patch" };
const CANDIDATE_WORDS: Record<string, string> = { PROPOSED: "Candidate ready for validation", NOTHING_TO_PATCH: "No supported change found in the code: nothing to patch", pending: "Being prepared", failed: "Could not be prepared" };
const REC_STATUS: Record<string, string> = { change_required: "Change needed", already_matches: "Already matches", not_found: "Not found in the code", manual_review: "A person has to check" };

function Refused({ r, testid }: { r: { code: string | null; words: string; requestId: string | null } | null; testid: string }) {
  if (!r) return null;
  return (
    <Callout tone="warn" testid={testid}>
      <span data-code={r.code ?? ""}>{r.words}</span>
      {r.requestId ? <span className="block text-xs text-muted" data-testid={`${testid}-ref`}>Reference: {r.requestId}</span> : null}
      <Tech><span className="block text-xs text-muted">code {r.code ?? "none"}</span></Tech>
    </Callout>
  );
}

/** A hint from the server's `can`: hidden outside the role, otherwise enabled or disabled with its reason as visible text. */
function hintOf(can: Rec | null, action: string): { show: boolean; allowed: boolean; why: string | null } {
  const h = ((can?.actions ?? {}) as Rec)[action] as Rec | undefined;
  if (!h) return { show: false, allowed: false, why: null };
  if (h.reason_code === "permission_missing" || h.reason_code === "human_actor_required") return { show: false, allowed: false, why: null };
  return { show: true, allowed: !!h.allowed, why: h.allowed ? null : refusalWords(h.reason_code as string) };
}

function Act({ label, testid, hint, busy, onClick, variant }: { label: string; testid: string; hint: { show: boolean; allowed: boolean; why: string | null }; busy: boolean; onClick: () => void; variant?: "primary" }) {
  const id = React.useId();
  if (!hint.show) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button variant={variant} data-testid={testid} disabled={busy || !hint.allowed} aria-describedby={hint.why ? id : undefined} title={hint.why ?? undefined} onClick={onClick}>{label}</Button>
      {hint.why && <span id={id} className="text-sm text-muted" data-testid={`${testid}-why`}>{hint.why}</span>}
    </span>
  );
}

// ---------------------------------------------------------------------------------------------------------------- X1a
export function CodeRecommendations({ ws, cid }: { ws: string; cid: string }) {
  const [rec, setRec] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ReturnType<typeof refusalOf> | null>(null);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/changes/${q(cid)}/code?workspace_id=${q(ws)}`)
      .then((r) => { if (live) { setRec(r); setErr(null); } })
      .catch((e) => { if (live && e instanceof ApiError) setErr(refusalOf(e)); else if (live) throw e; });
    return () => { live = false; };
  }, [ws, cid]);
  return (
    <section data-testid="code-recs" aria-labelledby={`code-recs-${cid}`} className="grid gap-2 rounded-md border border-border p-3 text-sm">
      <h3 id={`code-recs-${cid}`} className="font-medium">Code recommendations <span className="text-muted font-normal">(what the code should do; no code has been changed or prepared)</span></h3>
      {err && <Refused r={err} testid="code-recs-refused" />}
      {!err && !rec && <Muted>Reading the recommendations...</Muted>}
      {rec && (
        <>
          <Muted testid="code-recs-statement">{String(rec.statement ?? "")}</Muted>
          {rec.stale ? <Callout tone="warn" testid="code-recs-stale">These recommendations are out of date: {((rec.stale_reasons ?? []) as string[]).join("; ") || "the knowledge or the files moved"}.</Callout> : null}
          <p data-testid="code-recs-counts">{Object.entries((rec.counts ?? {}) as Record<string, number>).filter(([, n]) => n > 0).map(([k, n]) => `${REC_STATUS[k] ?? humanize(k)}: ${n}`).join(" · ") || "No code location is linked to this change."}</p>
          <ul className="grid gap-2">
            {((rec.items ?? []) as Rec[]).map((i, n) => (
              <li key={n} data-testid="code-rec-item" data-status={String(i.status)} className="rounded-md border border-border p-2 grid gap-1">
                <span><StatePill state={String(i.status)} label={REC_STATUS[String(i.status)] ?? humanize(String(i.status))} /> <strong>{String(i.title)}</strong>{i.path ? <span className="text-muted"> in {String(i.path)}{i.locator ? ` (${String(i.locator)})` : ""}</span> : null}</span>
                <span>{typeof i.desired === "string" ? i.desired : String((i.desired as Rec)?.text ?? "")}</span>
                {i.risk ? <Muted>Risk: {String(i.risk)}</Muted> : null}
                <Tech>{i.observed_logic ? <pre className="text-xs whitespace-pre-wrap break-all">{String(i.observed_logic)}</pre> : null}{(i.desired as Rec)?.pseudocode ? <pre className="text-xs whitespace-pre-wrap break-all">{String((i.desired as Rec).pseudocode)}</pre> : null}</Tech>
              </li>
            ))}
          </ul>
          {rec.withheld ? <Muted testid="code-recs-withheld">Hidden for your role: {String(rec.withheld)}.</Muted> : null}
          <Muted testid="code-recs-apply">{String((rec.apply as Rec)?.reason ?? "")}</Muted>
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------------------------- X1b
export function CodeCandidate({ ws, cid }: { ws: string; cid: string }) {
  const [repos, setRepos] = React.useState<Rec | null>(null);
  const [repoErr, setRepoErr] = React.useState<ReturnType<typeof refusalOf> | null>(null);
  const [repo, setRepo] = React.useState("");
  const [st, setSt] = React.useState<Rec | null>(null);
  const [refused, setRefused] = React.useState<ReturnType<typeof refusalOf> | null>(null);
  const [note, setNote] = React.useState<string | null>(null);
  const [reason, setReason] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [tick, setTick] = React.useState(0);
  const polls = React.useRef(0);

  React.useEffect(() => {
    api<Rec>("GET", `/v2/sources/repositories?workspace_id=${q(ws)}`)
      .then((r) => { setRepos(r); const list = (r.repositories ?? []) as Rec[]; if (list.length === 1) setRepo(String(list[0].repo_id)); })
      .catch((e) => { if (e instanceof ApiError) setRepoErr(refusalOf(e)); else throw e; });
  }, [ws]);
  React.useEffect(() => {
    let live = true;
    let t: number | undefined;
    const read = async () => {
      try {
        const r = await api<Rec>("GET", `/v2/business/changes/${q(cid)}/code/candidate?workspace_id=${q(ws)}${repo ? `&repo_id=${q(repo)}` : ""}`);
        if (!live) return;
        setSt(r);
        const pending = [(r.candidate as Rec | null)?.status, (r.validation as Rec | null)?.status].includes("pending");
        if (pending && polls.current < 60) { polls.current += 1; t = window.setTimeout(() => void read(), 2000); }
      } catch (e) { if (live && e instanceof ApiError) setRefused(refusalOf(e)); else if (live) throw e; }
    };
    void read();
    return () => { live = false; if (t) window.clearTimeout(t); };
  }, [ws, cid, repo, tick]);

  const can = (st?.can ?? null) as Rec | null;
  const create = hintOf(can, "create_candidate"), validate = hintOf(can, "validate"), approve = hintOf(can, "approve"), reject = hintOf(can, "reject"), publish = hintOf(can, "publish");
  const send = async (path: string, body: Rec, done: (r: Rec) => string) => {
    setBusy(true); setRefused(null); setNote(null);
    try {
      const r = await api<Rec>("POST", `/v2/business/changes/${q(cid)}/code/candidate${path}`, { workspace_id: ws, ...body });
      setNote(done(r)); polls.current = 0;
    } catch (e) { if (e instanceof ApiError) setRefused(refusalOf(e)); else throw e; }
    finally { setBusy(false); setTick((x) => x + 1); }
  };
  const c = (st?.candidate ?? null) as Rec | null;
  const v = (st?.validation ?? null) as Rec | null;
  const b = ((can?.binding ?? st?.current_binding ?? {}) as Rec);
  const list = ((repos?.repositories ?? []) as Rec[]);
  const decideBody = (action: string) => ({ candidate_job_id: c?.job_id, action, reason: reason.trim(), patch_hash: b.patch_hash, validation_hash: b.validation_hash });
  const noReason = !reason.trim();

  return (
    <section data-testid="code-candidate" aria-labelledby={`code-cand-${cid}`} className="grid gap-3 rounded-md border border-border p-3 text-sm">
      <h3 id={`code-cand-${cid}`} className="font-medium">Code candidate <span className="text-muted font-normal">(a patch on a disposable copy of a declared repository)</span></h3>
      <Muted testid="code-boundary">{String(st?.boundary ?? "The candidate is built and validated on a disposable clone of a declared repository; nothing is ever merged and nothing is deployed.")}</Muted>
      {repoErr && <Refused r={repoErr} testid="code-repos-refused" />}
      {repos && !repos.configured && <Callout tone="info" testid="code-unavailable">Unavailable: no repository is declared for this workspace. An operator declares repositories; until then no code candidate can be built. The recommendations above still apply.</Callout>}
      {repos && repos.configured && list.length === 0 && <Callout tone="info" testid="code-unavailable">Unavailable: no declared repository is visible to your role.</Callout>}
      {list.length > 0 && (
        <Field label="Repository (declared by the operator)">
          <Select data-testid="code-repo-select" value={repo} onChange={(e) => setRepo(e.target.value)}>
            <option value="">Choose a repository</option>
            {list.map((r) => <option key={String(r.repo_id)} value={String(r.repo_id)}>{String(r.name)}{r.ready ? "" : " (not ready)"}</option>)}
          </Select>
        </Field>
      )}
      {repo && <div className="flex flex-wrap gap-2"><Act label="Build a candidate" testid="code-build" variant="primary" hint={create} busy={busy} onClick={() => void send("", { repo_id: repo }, (r) => r.created ? "A candidate is being built." : "This candidate already exists for the current inputs.")} /></div>}
      {c && (
        <div data-testid="code-candidate-state" data-status={String(c.status)} className="grid gap-1 rounded-md border border-border p-2">
          <p><StatePill state={String(c.status)} label={CANDIDATE_WORDS[String(c.status)] ?? humanize(String(c.status))} />{c.reason ? <span className="text-muted"> ({String(c.reason)})</span> : null}</p>
          {c.patch_hash ? <p data-testid="code-binding">Patch <code>{short(c.patch_hash)}</code> on base <code>{short(c.base_sha)}</code>{v?.validation_hash ? <> · validation <code>{short(v.validation_hash)}</code></> : null}. A decision is bound to exactly these.</p> : null}
          {Array.isArray(c.files) && c.files.length > 0 && <p data-testid="code-files">Files changed: {(c.files as Rec[]).map((f) => f.path ? `${String(f.path)} (${String(f.replacements)} replacement(s))` : `${String(f.replacements)} replacement(s) in a file hidden for your role`).join("; ")}</p>}
          <div data-testid="code-not-implemented">
            <p className="font-medium">Not implemented by this patch</p>
            {((c.not_implemented ?? []) as Rec[]).length === 0 ? <Muted>Nothing listed.</Muted>
              : <ul className="list-disc pl-5">{(c.not_implemented as Rec[]).map((i, n) => <li key={n}>{String(i.ref ?? "")}: {String(i.reason ?? "")}</li>)}</ul>}
          </div>
          <Tech>{c.diff ? <pre className="text-xs whitespace-pre-wrap break-all max-h-80 overflow-auto" data-testid="code-diff">{String(c.diff)}</pre> : null}<TechField label="candidate job" value={c.job_id} /></Tech>
        </div>
      )}
      {c && <div className="flex flex-wrap gap-2"><Act label="Validate" testid="code-validate" hint={validate} busy={busy} onClick={() => void send("/validate", { candidate_job_id: c.job_id }, () => "Validation started on a fresh copy.")} /></div>}
      {v && (
        <div data-testid="code-validation" data-status={String(v.report_status ?? v.status)} className="grid gap-1 rounded-md border border-border p-2">
          <p><StatePill state={String(v.report_status ?? v.status)} label={VALIDATION_WORDS[String(v.report_status)] ?? CANDIDATE_WORDS[String(v.status)] ?? humanize(String(v.status))} />{v.reason ? <span className="text-muted"> ({String(v.reason)})</span> : null}</p>
          {v.isolation ? <Muted>{String(v.isolation)}</Muted> : null}
          <Tech>{((v.commands ?? []) as Rec[]).map((x, n) => <p key={n} className="text-xs"><code>{((x.argv ?? []) as string[]).join(" ")}</code> exit {String(x.exit)} ({String(x.seconds)} s)</p>)}</Tech>
        </div>
      )}
      {(approve.show || reject.show) && c && (
        <div className="grid gap-2" data-testid="code-decide">
          <Field label="Your reason (recorded with your name and both hashes)"><TextArea data-testid="code-reason" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
          <div className="flex flex-wrap gap-2">
            <Act label="Approve this patch" testid="code-approve" variant="primary" busy={busy} hint={noReason && approve.allowed ? { ...approve, allowed: false, why: "Write your reason first." } : approve} onClick={() => void send("/decide", decideBody("approve"), () => "Approved: bound to this patch and this validation.")} />
            <Act label="Reject" testid="code-reject" busy={busy} hint={noReason && reject.allowed ? { ...reject, allowed: false, why: "Write your reason first." } : reject} onClick={() => void send("/decide", decideBody("reject"), () => "Rejected.")} />
          </div>
        </div>
      )}
      {st?.approval ? <p data-testid="code-approval">Approved by {String((st.approval as Rec).actor_id)}: “{String((st.approval as Rec).reason)}”.</p> : null}
      {((st?.decisions ?? []) as Rec[]).filter((d) => d.kind === "reject").slice(-1).map((d, n) => <p key={n} data-testid="code-rejected">Rejected by {String(d.actor_id)}: “{String(d.reason)}”.</p>)}
      {c && <div className="flex flex-wrap gap-2"><Act label="Open a pull request" testid="code-publish" hint={publish} busy={busy} onClick={() => void send("/publish", { candidate_job_id: c.job_id }, (r) => `A pull request was requested on branch ${String(r.branch)}; it is opened by a job and never merged.`)} /></div>}
      {st?.pr_intent ? <p data-testid="code-pr-intent" data-status={String((st.pr_intent as Rec).status)}>Publish request recorded and refused: {refusalWords(String((st.pr_intent as Rec).reason))} Nothing was pushed.</p> : null}
      {st?.pull_request ? (
        <p data-testid="code-pull-request" data-state={String((st.pull_request as Rec).pr_state)}>
          Pull request #{String((st.pull_request as Rec).pr_number)} ({String((st.pull_request as Rec).pr_state)}, merged: {(st.pull_request as Rec).merged ? "yes" : "no"}) on branch {String((st.pull_request as Rec).branch)}.
          {(st.pull_request as Rec).pr_url ? <> <a className="text-primary underline" href={String((st.pull_request as Rec).pr_url)} target="_blank" rel="noopener noreferrer">Open it</a></> : null}
        </p>
      ) : null}
      {note && <Callout tone="info" testid="code-note">{note}</Callout>}
      <Refused r={refused} testid="code-refused" />
    </section>
  );
}
