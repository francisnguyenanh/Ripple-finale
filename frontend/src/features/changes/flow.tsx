// The Changes flow state: ids live in the URL (so links work), data is read from the server and
// polled in a bounded way. Polling replaces `data` only: it never touches inputs, selection,
// scroll or focus (inputs are local state of stable components).
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { useSession } from "@/session";
import { hashFor, replaceHash } from "@/lib/route";
import { isLive, loadFlow } from "./load";
import { nextAction } from "./next";
import { STAGES, type FlowData, type Ids, type StageKey } from "./types";

const PARAM_KEYS: StageKey[] = ["intent", "draft", "query", "whatif", "chat", ...STAGES];
const SHARE_KEYS: StageKey[] = ["intent", "draft", "impact", "simulation", "proposal", "review", "approval", "apply", "query", "chat"];
const WAIT_POLL_MAX = 20;
const NO_RESERVED: string[] = [];   // a stable default: a fresh [] on every render would re-create the loader and re-run it in a loop

export interface FlowCtx {
  ws: string;
  ids: Ids;
  data: FlowData | null;
  failed: boolean;
  loadError: ApiError | null;
  waitExhausted: boolean;
  setIds: (patch: Ids) => void;
  refresh: () => Promise<void>;
  checkNow: () => void;
  shareLink: () => string;
  superseded: Superseded | null;
  setSuperseded: (s: Superseded | null) => void;
}
export interface Superseded { steps: { name: string; state: string }[]; applied: boolean }

const Ctx = React.createContext<FlowCtx | null>(null);
export const useFlow = () => {
  const c = React.useContext(Ctx);
  if (!c) throw new Error("no flow");
  return c;
};

/** `reserved` params belong to the page itself (History uses `approval` for a restore approval, not a job): the flow leaves them alone. */
export function FlowProvider({ ws, page = "changes", reserved = NO_RESERVED, children }: { ws: string; page?: string; reserved?: string[]; children: React.ReactNode }) {
  const s = useSession();
  const [ids, setIdsState] = React.useState<Ids>(() => {
    const out: Ids = {};
    for (const k of PARAM_KEYS) { const v = s.route.params[k]; if (v && !reserved.includes(k)) out[k] = v; }
    return out;
  });
  const [data, setData] = React.useState<FlowData | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [loadError, setLoadError] = React.useState<ApiError | null>(null);
  const [waitPolls, setWaitPolls] = React.useState(0);
  const [superseded, setSuperseded] = React.useState<Superseded | null>(null);
  const dataRef = React.useRef<FlowData | null>(null);
  const idsRef = React.useRef(ids);
  idsRef.current = ids;
  const sRef = React.useRef(s);
  sRef.current = s;
  const timer = React.useRef<number | null>(null);
  const lastSig = React.useRef("");
  const waits = React.useRef(0);
  const gen = React.useRef(0);
  const lastPump = React.useRef(0);

  const writeUrl = React.useCallback((next: Ids) => {
    const params: Record<string, string | undefined> = {};
    for (const k of SHARE_KEYS.concat(["whatif"])) if (!reserved.includes(k)) params[k] = next[k];
    const r = sRef.current.route;
    for (const k of Object.keys(r.params)) if (!PARAM_KEYS.includes(k as StageKey) || reserved.includes(k)) params[k] = r.params[k];
    replaceHash(hashFor(ws, page, r.sub, params));
  }, [ws, page, reserved]);

  const setIds = React.useCallback((patch: Ids) => {
    const next = { ...idsRef.current, ...patch };
    for (const k of Object.keys(next) as StageKey[]) if (!next[k]) delete next[k];
    idsRef.current = next;
    setIdsState(next);
    writeUrl(next);
  }, [writeUrl]);

  const run = React.useCallback(async (first: boolean) => {
    const my = ++gen.current;
    if (timer.current) { window.clearTimeout(timer.current); timer.current = null; }
    let d: FlowData;
    try {
      d = await loadFlow(ws, idsRef.current, sRef.current.can);
    } catch (e) {
      if (my !== gen.current) return;
      if (!(e instanceof ApiError)) throw e;
      if (first || !dataRef.current) { setLoadError(e); return; }
      setFailed(true);
      return;
    }
    if (my !== gen.current) return;
    // The requester confirmed the chat: everybody who opens it continues with the saved request.
    if (d.chat && !d.chat.unavailable && d.chat.intent_id && !idsRef.current.intent) {
      setIds({ intent: d.chat.intent_id });
      try { d = await loadFlow(ws, idsRef.current, sRef.current.can); } catch { /* the next poll reports it */ }
      if (my !== gen.current) return;
    }
    if (d.proposalAppliedJob && !idsRef.current.apply) {          // the proposal was applied: show what the apply did, whichever link was opened
      setIds({ apply: d.proposalAppliedJob });
      try { d = await loadFlow(ws, idsRef.current, sRef.current.can); } catch { /* the next poll reports it */ }
      if (my !== gen.current) return;
    }
    // A link with only part of the ids (the proposal, but not its review / approval / apply): the server names the rest of that ONE proposal's chain. Only ids that are missing are
    // filled, nothing the person typed or opened is replaced, and a failure leaves the page as it is.
    const cur = idsRef.current;
    if (cur.proposal && d.proposal && (!cur.review || !cur.approval || !cur.apply || !cur.impact || !cur.simulation || !cur.draft || !cur.intent)) {
      try {
        const ch = await api<{ params?: Record<string, string> }>("GET", `/v2/work/chain?workspace_id=${encodeURIComponent(ws)}&proposal_job_id=${encodeURIComponent(cur.proposal)}`);
        const fill: Ids = {};
        for (const k of SHARE_KEYS) if (!cur[k] && ch.params?.[k] && k !== "query" && k !== "chat") fill[k] = ch.params[k];
        if (Object.keys(fill).length && my === gen.current) {
          setIds(fill);
          try { d = await loadFlow(ws, idsRef.current, sRef.current.can); } catch { /* the next poll reports it */ }
          if (my !== gen.current) return;
        }
      } catch { /* the chain could not be read: keep what the link says */ }
    }
    dataRef.current = d;
    const sh = sRef.current.head;
    if (!sh || sh.source_revision_id !== d.head.source_revision_id || sh.knowledge_revision_id !== d.head.knowledge_revision_id || sh.decision_epoch !== d.head.decision_epoch)
      void sRef.current.refresh();
    setData(d); setFailed(false); setLoadError(null);
    const me = { account: sRef.current.account, can: sRef.current.can };
    const nx = nextAction(d, me);
    const sig = `${nx.kind}|${nx.who}|${nx.text}`;
    if (sig !== lastSig.current) { lastSig.current = sig; waits.current = 0; setWaitPolls(0); }
    if (isLive(d)) {
      // In a sandbox workspace a page with live jobs asks the bounded pump to run what is runnable (server enforces lease, hold, budget).
      const now = Date.now();
      if (sRef.current.sandbox && now - lastPump.current > 3000) {
        lastPump.current = now;
        api("POST", "/v2/sandbox/pump", { workspace_id: ws }).catch(() => undefined);
      }
      timer.current = window.setTimeout(() => void run(false), 2000);
    } else if (nx.kind === "wait" && waits.current < WAIT_POLL_MAX) {
      waits.current += 1; setWaitPolls(waits.current);
      timer.current = window.setTimeout(() => void run(false), 6000);
    }
  }, [ws, setIds]);


  React.useEffect(() => {
    void run(true);
    return () => { gen.current += 1; if (timer.current) window.clearTimeout(timer.current); };
  }, [run]);

  // Back / forward / a link clicked while this page is open: the ids in the address are the truth. Without this the page kept the ids it was opened with and showed another change.
  const urlSig = PARAM_KEYS.map((k) => (reserved.includes(k) ? "" : s.route.params[k] ?? "")).join("|");
  React.useEffect(() => {
    const fromUrl: Ids = {};
    for (const k of PARAM_KEYS) { const v = sRef.current.route.params[k]; if (v && !reserved.includes(k)) fromUrl[k] = v; }
    const same = PARAM_KEYS.every((k) => (idsRef.current[k] ?? "") === (fromUrl[k] ?? ""));
    if (same) return;
    // The address was cleared or points elsewhere: follow it (an empty address is the chooser, not a trap).
    idsRef.current = fromUrl; setIdsState(fromUrl); dataRef.current = null; setData(null); setLoadError(null);
    void run(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSig]);

  // "Reload state" after a stale refusal: a deliberate act; follow the newest approval of this proposal.
  React.useEffect(() => {
    const f = async () => {
      const cur = idsRef.current;
      if (cur.proposal && cur.approval) {
        try {
          const latest = await api<{ latest_job_id?: string }>("GET", `/v2/proposals/${encodeURIComponent(cur.proposal)}/approvals?workspace_id=${encodeURIComponent(ws)}`);
          if (latest.latest_job_id && latest.latest_job_id !== cur.approval) setIds({ approval: latest.latest_job_id });
        } catch { /* keep the current approval; the read below reports a real failure */ }
      }
      void run(false);
    };
    window.addEventListener("ripple:reload", f);
    return () => window.removeEventListener("ripple:reload", f);
  }, [ws, run, setIds]);

  const shareLink = React.useCallback(() => {
    const params: Record<string, string | undefined> = {};
    for (const k of SHARE_KEYS) params[k] = idsRef.current[k];
    return location.origin + location.pathname + hashFor(ws, "changes", undefined, params);
  }, [ws]);

  const value: FlowCtx = {
    ws, ids, data, failed, loadError, waitExhausted: waitPolls >= WAIT_POLL_MAX,
    setIds, refresh: () => run(false), checkNow: () => { waits.current = 0; setWaitPolls(0); void run(false); },
    shareLink, superseded, setSuperseded,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
