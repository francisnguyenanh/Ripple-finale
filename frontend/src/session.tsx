import * as React from "react";
import { ApiError, Auth, type Head, type SandboxStatus, type WorkspaceInfo } from "@/lib/api";
import { hashFor, parseHash, replaceHash, type Route } from "@/lib/route";
import { kindEnabled, parseAiKinds } from "@/lib/aiKinds";

export type Mode = "business" | "technical";

export interface Session {
  account: string;
  workspaces: WorkspaceInfo[];
  route: Route;
  mode: Mode;
  setMode: (m: Mode) => void;
  head: Head | null;
  sandbox: SandboxStatus | null;
  notice: string;
  /** The server operator switched the AI provider off: AI actions are refused until it is armed under an approved allocation. */
  aiOff: boolean;
  /** Whether an AI job kind can be started on this server now: the provider is on and the kind is in the server's role allowlist. */
  aiKind: (kind: string) => boolean;
  /** Permissions of the signed-in account in the open workspace. */
  can: (permission: string) => boolean;
  roles: string[];
  signOut: () => Promise<void>;
  /** The session ended while a link was open: keep the link, show sign-in again. */
  expire: () => void;
  refresh: () => Promise<void>;
  /** Read the list of workspaces again (after one was created). */
  reloadWorkspaces: () => Promise<void>;
  /** A deliberate reload of the open page (clicking the current page in the menu): the link keeps its parameters. */
  reloadNonce: number;
  reload: () => void;
}

const Ctx = React.createContext<Session | null>(null);
/** Exposed only so the development component catalog can supply a synthetic session; the app itself uses SessionProvider. */
export const SessionContext = Ctx;
export const useSession = () => {
  const s = React.useContext(Ctx);
  if (!s) throw new Error("no session");
  return s;
};

export function useRoute(): Route {
  const [r, setR] = React.useState(() => parseHash(location.hash));
  React.useEffect(() => {
    const f = () => setR(parseHash(location.hash));
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  return r;
}

type Boot = { kind: "booting" } | { kind: "signed-out"; message: string } | { kind: "in"; account: string };

export function SessionProvider({
  children,
  login,
}: {
  children: (s: Session) => React.ReactNode;
  login: (message: string, onDone: () => void) => React.ReactNode;
}) {
  const [boot, setBoot] = React.useState<Boot>({ kind: "booting" });
  const [workspaces, setWorkspaces] = React.useState<WorkspaceInfo[]>([]);
  const [head, setHead] = React.useState<Head | null>(null);
  const [sandbox, setSandbox] = React.useState<SandboxStatus | null>(null);
  const [reloadNonce, setReloadNonce] = React.useState(0);
  const route = useRoute();
  const [mode, setModeState] = React.useState<Mode>(() => (parseHash(location.hash).params.mode === "technical" ? "technical" : "business"));
  React.useEffect(() => { document.body.dataset.mode = mode; }, [mode]);
  // The URL carries the chosen view: a link followed in Technical keeps it, and every internal navigation keeps it.
  React.useEffect(() => {
    const wantsTech = route.params.mode === "technical";
    if (wantsTech && mode !== "technical") setModeState("technical");
    else if (!wantsTech && mode === "technical" && route.ws)
      replaceHash(hashFor(route.ws, route.page, route.sub, { ...route.params, mode: "technical" }));
  }, [route, mode]);
  // The chosen view is kept in the URL (mode=technical) so a reload or a copied link keeps it. No request is made.
  const setMode = React.useCallback((m: Mode) => {
    setModeState(m);
    const r = parseHash(location.hash);
    if (r.ws) replaceHash(hashFor(r.ws, r.page, r.sub, { ...r.params, mode: m === "technical" ? "technical" : undefined }));
  }, []);
  const notice = document.querySelector('meta[name="ripple-notice"]')?.getAttribute("content") ?? "";
  const aiOff = document.querySelector('meta[name="ripple-provider"]')?.getAttribute("content") === "LIVE_DISARMED";
  const aiKinds = React.useMemo(() => parseAiKinds(document.querySelector('meta[name="ripple-ai-kinds"]')?.getAttribute("content")), []);

  const start = React.useCallback(async () => {
    try {
      const me = await Auth.me();
      setWorkspaces(await Auth.workspaces());
      setBoot({ kind: "in", account: me.account_id });
    } catch (e) {
      const unavailable = e instanceof ApiError && e.status !== 401;
      setBoot({ kind: "signed-out", message: unavailable ? "The service is not available right now. Try again in a moment." : "" });
    }
  }, []);
  React.useEffect(() => { void start(); }, [start]);

  // Head and writing-session status follow the selected workspace. Read-only; never a model call.
  const ws = route.ws;
  const refresh = React.useCallback(async () => {
    if (!ws || boot.kind !== "in") { setHead(null); setSandbox(null); return; }
    try { setHead(await Auth.head(ws)); } catch { setHead(null); }
    try { setSandbox(await Auth.sandbox(ws)); } catch { setSandbox(null); }
  }, [ws, boot.kind]);
  React.useEffect(() => { void refresh(); }, [refresh]);

  const reloadWorkspaces = React.useCallback(async () => { try { setWorkspaces(await Auth.workspaces()); } catch { /* the list stays as it was */ } }, []);
  // Permissions come from the server and can change while a page is open (a revoked role, AI use switched off): read them again when a
  // request is refused for permission and when the person comes back to the tab. No timer; the server rechecks every action anyway.
  React.useEffect(() => {
    if (boot.kind !== "in") return;
    const again = () => { void reloadWorkspaces(); void refresh(); };
    const vis = () => { if (document.visibilityState === "visible") again(); };
    window.addEventListener("ripple:permissions", again);
    window.addEventListener("focus", again);
    document.addEventListener("visibilitychange", vis);
    return () => { window.removeEventListener("ripple:permissions", again); window.removeEventListener("focus", again); document.removeEventListener("visibilitychange", vis); };
  }, [boot.kind, reloadWorkspaces, refresh]);

  const signOut = React.useCallback(async () => {
    try { await Auth.logout(); } catch { /* purge regardless */ }
    setWorkspaces([]); setHead(null); setSandbox(null);
    history.replaceState(null, "", location.pathname);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    setBoot({ kind: "signed-out", message: "" });
  }, []);

  const expire = React.useCallback(() => {
    setWorkspaces([]); setHead(null); setSandbox(null);
    setBoot((b) => (b.kind === "in" ? { kind: "signed-out", message: "Your session ended. Sign in again to continue where you were." } : b));
  }, []);

  if (boot.kind === "booting") return <p aria-busy="true" className="p-6 text-muted">Loading...</p>;
  if (boot.kind === "signed-out") return <>{login(boot.message, () => { setBoot({ kind: "booting" }); void start(); })}</>;
  const mine = workspaces.find((w) => w.workspace_id === ws);
  const value: Session = {
    account: boot.account, workspaces, route, mode, setMode, head, sandbox, notice, aiOff,
    aiKind: (k) => !aiOff && kindEnabled(aiKinds, k),
    can: (p) => !!mine && mine.permissions.includes(p),
    roles: mine?.roles ?? [],
    signOut, expire, refresh, reloadWorkspaces, reloadNonce, reload: () => setReloadNonce((n) => n + 1),
  };
  return <Ctx.Provider value={value}>{children(value)}</Ctx.Provider>;
}
