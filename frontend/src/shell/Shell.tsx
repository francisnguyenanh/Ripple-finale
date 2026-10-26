import { WindowBanner } from "./WindowBanner";
import * as React from "react";
import { Menu, LayoutDashboard, FileText, Network, HelpCircle, MessageSquare, GitCompare, History, ShieldCheck, Route, LogOut, Briefcase, Wrench, BookOpen, ChevronDown, CircleUser, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useSession, type Session } from "@/session";
import { NAV } from "@/lib/copy";
import { hashFor } from "@/lib/route";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/StatusBadge";
import { Select } from "@/components/bits";
import { useMedia } from "@/lib/useMedia";

const ICONS: Record<string, typeof Menu> = {
  overview: LayoutDashboard, sources: FileText, explorer: Network, questions: HelpCircle,
  changes: MessageSquare, impact: GitCompare, history: History, support: ShieldCheck, harbor: Route, documents: BookOpen,
};

function leaseText(s: Session): { text: string; ok: boolean } | null {
  const st = s.sandbox;
  if (!st) return null;
  const L = st.lease;
  if (st.i_can_write) return { text: "Your demo session is running", ok: true };
  if (L?.active) return { text: `Demo session held by ${L.holder ?? "someone else"}: read-only for you`, ok: false };
  return { text: "No demo session is running: read-only", ok: false };
}

function useWide(): boolean {
  const q = "(min-width: 1024px)";
  const [wide, setWide] = React.useState(() => window.matchMedia(q).matches);
  React.useEffect(() => {
    const m = window.matchMedia(q);
    const f = () => setWide(m.matches);
    m.addEventListener("change", f);
    return () => m.removeEventListener("change", f);
  }, []);
  return wide;
}

// SCN-05 / SCN-14: the side navigation can be an icon rail (64 px). Two inputs, kept apart: the person's own choice (`userRail`, kept for the session in
// sessionStorage) and the automatic starting layout (`autoRail`, a live media query: rail from 1024 to 1439 px, full from 1440 px). Without a choice the layout
// follows the window as it is resized; with one, the choice wins until the session ends. Nothing else (polling, a panel opening) writes either. Storage that is
// unavailable or holds an unknown value reads as "no choice".
const RAIL_KEY = "ripple.nav.rail";
function readRail(): boolean | null {
  try { const v = window.sessionStorage.getItem(RAIL_KEY); return v === "1" ? true : v === "0" ? false : null; } catch { return null; }
}
function writeRail(v: boolean) { try { window.sessionStorage.setItem(RAIL_KEY, v ? "1" : "0"); } catch { /* the choice still applies to this page */ } }

/** Account and sign-out (SCN-05): a disclosure (not an ARIA menu): Enter / Space opens it and moves focus inside, Escape or a click outside closes it and focus
 *  returns to the button. The account name stays visible on the button. */
function AccountMenu({ account, onSignOut }: { account: string; onSignOut: () => void }) {
  const [open, setOpen] = React.useState(false);
  const btn = React.useRef<HTMLButtonElement>(null);
  const box = React.useRef<HTMLDivElement>(null);
  const close = () => { setOpen(false); btn.current?.focus(); };
  React.useEffect(() => { if (open) box.current?.querySelector<HTMLElement>("button, a")?.focus(); }, [open]);
  React.useEffect(() => {
    if (!open) return;
    const f = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", f);
    return () => document.removeEventListener("mousedown", f);
  }, [open]);
  return (
    <div className="relative" onKeyDown={(e) => { if (e.key === "Escape" && open) { e.stopPropagation(); close(); } }}>
      <Button ref={btn} variant="ghost" className="text-header-text hover:bg-header-surface" aria-expanded={open} aria-controls="account-menu" data-testid="account-menu-button" onClick={() => setOpen((v) => !v)}>
        <CircleUser size={18} aria-hidden="true" /><span className="sr-only">Account: </span><strong data-testid="session-account" className="font-medium">{account}</strong>
        <ChevronDown size={14} aria-hidden="true" />
      </Button>
      {open && (
        <div id="account-menu" ref={box} data-testid="account-menu" className="rp-pop absolute right-0 top-full z-40 mt-1 grid min-w-56 gap-2 rounded-md border border-border bg-surface p-3 text-text shadow-pop">
          <p className="text-sm">Signed in as <strong>{account}</strong></p>
          <Button variant="secondary" onClick={onSignOut} data-testid="signout"><LogOut size={16} aria-hidden="true" />Sign out</Button>
        </div>
      )}
    </div>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const s = useSession();
  const [menu, setMenu] = React.useState(false);
  const menuBtn = React.useRef<HTMLButtonElement>(null);
  const sheet = React.useRef<HTMLElement>(null);
  const wide = useWide();
  const autoRail = useMedia("(max-width: 1439px)");
  const [userRail, setUserRail] = React.useState<boolean | null>(readRail);
  const rail = userRail ?? autoRail;
  const setRail = (v: boolean) => { setUserRail(v); writeRail(v); };
  const ws = s.route.ws;
  const mine = s.workspaces.find((w) => w.workspace_id === ws);
  const lease = leaseText(s);

  // Choosing a page or a workspace closes the menu; if focus was inside it, it goes back to the menu button instead of being lost in a hidden sheet.
  React.useEffect(() => {
    if (sheet.current?.contains(document.activeElement)) menuBtn.current?.focus();
    setMenu(false);
  }, [s.route.page, ws]);
  // A menu the person opened takes focus into itself; Escape closes it and gives focus back to the button.
  React.useEffect(() => { if (menu && !wide) sheet.current?.querySelector<HTMLElement>("a")?.focus(); }, [menu, wide]);
  const closeMenu = () => { setMenu(false); menuBtn.current?.focus(); };

  // Workspace, role, revisions, demo session, view and sign-out: in the navy command bar on a wide screen, at the top of the menu sheet (an ordinary surface) on a
  // small one (rendered once). `bar` picks the colours of the place they are in; small uppercase labels sit beside or above their values (SCN-13 command context).
  const bar = wide;
  const label = cn("text-xs font-semibold uppercase tracking-wider", bar ? "text-header-muted" : "text-muted");
  const session = (
    <div className={wide ? "contents" : "grid gap-3 p-3 border-b border-border"} data-testid="session-controls">
        <label className="flex items-center gap-2 text-sm">
          <span className={label}>Workspace</span>
          <Select className={cn("py-1.5 min-h-9", bar ? "bg-header-surface text-header-text border-header-muted hover:border-header-text" : "bg-surface text-text")} data-testid="ws-select" value={ws ?? ""}
            onChange={(e) => { if (e.target.value) location.hash = hashFor(e.target.value, s.route.page || "overview", undefined, { mode: s.mode === "technical" ? "technical" : undefined }); }}>
            <option value="">Select a workspace</option>
            {s.workspaces.map((w) => <option key={w.workspace_id} value={w.workspace_id}>{w.display_name || w.workspace_id}</option>)}
          </Select>
        </label>
        {mine && mine.roles.length > 0 && (
          <span className="grid leading-tight"><span className={label}>Your role</span><span className="text-sm font-medium" data-testid="workspace-roles">{mine.roles.join(", ")}</span></span>
        )}
        {s.mode === "technical" && s.head && (
          <span className={cn("text-xs flex gap-3", bar ? "text-header-muted" : "text-muted")} data-testid="revision-chips">
            <span>Source revision: <code data-testid="head-source-revision">{s.head.source_revision_id ?? "none"}</code></span>
            <span>Knowledge revision: <code data-testid="head-knowledge-revision">{s.head.knowledge_revision_id ?? "none"}</code></span>
          </span>
        )}
        {lease && (
          <a href={ws ? hashFor(ws, "support") : "#"} data-testid="lease-chip" data-state={lease.ok ? "ok" : "read-only"}
            className={cn("text-xs rounded-full border px-2 py-0.5", bar ? "border-header-muted text-header-text hover:bg-header-surface" : "border-border hover:bg-surface-2")}>{lease.text}</a>
        )}
        <div className={cn("flex flex-wrap items-center gap-3", wide && "ml-auto")}>
          {/* SCN-18: on the navy bar the switch is a navy segmented control: the pressed view is a lighter navy segment with an inset ring and bold text (not colour
              alone); the other view is muted text that brightens on hover; no clipping, so the focus outline stays whole. The menu sheet keeps the light version. */}
          <div role="group" aria-label="View: Business or Technical" className={cn("inline-flex rounded-md border", bar ? "border-header-muted bg-header-bg p-0.5 gap-0.5" : "border-border overflow-hidden")}>
            {(["business", "technical"] as const).map((m) => (
              <button key={m} type="button" aria-pressed={s.mode === m} data-testid={`mode-${m}`} onClick={() => s.setMode(m)}
                className={cn("px-3 py-1.5 text-sm cursor-pointer inline-flex items-center gap-1.5",
                  bar ? cn("min-h-8 rounded-sm", s.mode === m ? "bg-header-surface text-header-text font-semibold ring-1 ring-inset ring-header-focus" : "bg-transparent text-header-muted hover:text-header-text hover:bg-header-surface/50")
                    : cn("min-h-9", s.mode === m ? "bg-primary text-on-primary" : "bg-surface text-text hover:bg-surface-2"))}>
                {m === "business" ? <Briefcase size={14} aria-hidden="true" /> : <Wrench size={14} aria-hidden="true" />}
                {m === "business" ? "Business" : "Technical"}
              </button>
            ))}
          </div>
          {wide ? <AccountMenu account={s.account} onSignOut={() => void s.signOut()} /> : (
            <>
              <span className="text-sm">Signed in as <strong data-testid="session-account">{s.account}</strong></span>
              <Button variant="secondary" onClick={() => void s.signOut()} data-testid="signout"><LogOut size={16} aria-hidden="true" />Sign out</Button>
            </>
          )}
        </div>
    </div>
  );

  const asRail = wide && rail;
  const nav = (
    <nav aria-label="Main" id="main-nav" className={cn("grid gap-1", asRail ? "p-2" : "p-3")}>
      {NAV.map((n) => {
        const Icon = ICONS[n.id];
        const active = s.route.page === n.id;
        const link = (
          <a key={n.id} href={ws ? hashFor(ws, n.id, undefined, { mode: s.mode === "technical" ? "technical" : undefined }) : "#"} aria-current={active ? "page" : undefined} data-testid={`nav-${n.id}`}
            aria-label={asRail ? n.label : undefined}
            onClick={(e) => { if (active) { e.preventDefault(); setMenu(false); s.reload(); } }}
            className={cn("flex items-center gap-3 rounded-md border-l-4 py-2 text-sm min-h-10", asRail ? "group relative justify-center px-0" : "px-3",
              active ? "border-primary bg-surface font-semibold text-text shadow-card"
                : wide ? "border-transparent text-muted hover:bg-nav-hover hover:text-text" : "border-transparent text-muted hover:bg-surface-2 hover:text-text",
              !ws && "pointer-events-none opacity-50")}>
            <Icon size={18} aria-hidden="true" />
            {!asRail && n.label}
            {/* the rail's visible name on hover and keyboard focus, inside the nav landmark (the accessible name is aria-label) */}
            {asRail && <span aria-hidden="true" className="pointer-events-none absolute left-full top-1/2 z-50 ml-2 hidden -translate-y-1/2 whitespace-nowrap rounded-md bg-text px-2 py-1 text-xs font-medium text-bg shadow-pop group-hover:block group-focus-visible:block">{n.label}</span>}
          </a>
        );
        return link;
      })}
    </nav>
  );

  return (
    <div className="min-h-full flex flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-surface focus:p-2" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}>Skip to content</a>
      <header className="rp-cmd border-b border-header-border bg-header-bg text-header-text flex flex-wrap items-center gap-3 px-4 py-2">
        {/* The name stays "Menu" (open/closed is aria-expanded); what is inside is a description, so the name never competes with the Workspace control. */}
        {!wide && (
          <Button ref={menuBtn} variant="ghost" className="px-2 text-header-text hover:bg-header-surface" aria-label="Menu" aria-describedby="menu-hint" aria-expanded={menu} aria-controls="menu-sheet" onClick={() => setMenu((v) => !v)} data-testid="menu-button">
            <Menu size={20} aria-hidden="true" />
            <span id="menu-hint" className="sr-only">Workspace, role, view and sign-out are inside.</span>
          </Button>
        )}
        <a href={ws ? hashFor(ws, "overview", undefined, { mode: s.mode === "technical" ? "technical" : undefined }) : "#"} data-testid="brand" className="text-lg font-semibold rounded-sm text-header-text"
          aria-label={ws ? "Ripple: go to Overview" : "Ripple"}>Ripple</a>
        {wide ? session : (
          // Small screens: one row that still says where you are and in which view; the controls themselves are in the menu.
          <span data-testid="header-context" className="min-w-0 flex-1 flex items-center gap-2 text-sm">
            <span className="truncate" title={mine?.display_name || ws || undefined}>{mine?.display_name || ws || "No workspace selected"}</span>
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-header-muted px-2 py-0.5 text-xs" data-testid="header-mode" data-mode={s.mode}>
              {s.mode === "technical" ? <Wrench size={12} aria-hidden="true" /> : <Briefcase size={12} aria-hidden="true" />}{s.mode === "technical" ? "Technical" : "Business"}
            </span>
            {mine && mine.roles.length > 0 && <span className="truncate text-xs text-header-muted" data-testid="header-role">{mine.roles.join(", ")}</span>}
          </span>
        )}
      </header>
      <section aria-label="Environment and session status" data-testid="context-rail" className="border-b border-border bg-surface-2 divide-y divide-border empty:hidden">
        {s.notice && (
          <div role="note" data-testid="ui-notice" className="text-sm px-4 py-1.5 text-sim flex flex-wrap items-center gap-2">
            {s.notice.startsWith("LIVE-LOCAL")
              ? <StatusBadge status="info" label={s.aiOff ? "LIVE DATA - AI OFF" : "LIVE - REAL AI, COSTS MONEY"} />
              : <StatusBadge status="simulated" />} <span>{s.notice}</span>
          </div>
        )}
        <WindowBanner />
        {s.aiOff && (
          <div role="note" data-testid="ai-off-banner" className="text-sm px-4 py-1.5">
            <p className="flex flex-wrap items-center gap-x-2"><strong>AI actions are switched off on this server.</strong><span>Buttons that would call the AI model are disabled; everything already saved stays readable.</span></p>
            <details className="text-muted"><summary className="cursor-pointer w-fit text-primary">Why and for how long</summary>
              <p>They stay off until the operator arms the provider under an approved allocation.</p>
            </details>
          </div>
        )}
        {s.sandbox && !s.sandbox.i_can_write && ws && (
          <div data-testid="lease-bar" data-state={s.sandbox.lease?.active ? "other" : "free"} className="px-4 py-1.5 text-sm">
            <strong>{s.sandbox.lease?.active ? "Read-only for you right now. " : "No demo session is running. "}</strong>
            {s.sandbox.lease?.active ? "Another demo session is running; you can still read everything. " : "Reading works; creating or approving anything needs a demo session. "}
            <a className="text-primary underline" href={hashFor(ws, "support")}>Open Support &amp; Access{s.sandbox.lease?.active ? "" : " to take it"}</a>
          </div>
        )}
      </section>
      <div className="flex flex-1 min-h-0 relative">
        {wide && (
          <div className={cn("shrink-0 border-r border-border bg-nav-bg", asRail ? "w-16" : "w-60")} data-testid="side-nav" data-rail={String(asRail)} data-rail-source={userRail === null ? "auto" : "user"}>
            {/* SCN-14 nav context: one slim strip that names the menu and holds its collapse control (no extra row of chrome). */}
            <div className={cn("flex min-h-11 items-center border-b border-border", asRail ? "justify-center px-1" : "justify-between pl-4 pr-2")} data-testid="nav-context">
              {!asRail && <span className="text-xs font-semibold uppercase tracking-wider text-muted">Navigation</span>}
              <Button variant="ghost" size="sm" className="px-2 hover:bg-nav-hover" aria-expanded={!asRail} aria-controls="main-nav" aria-label={asRail ? "Expand the menu" : "Collapse the menu to icons"} data-testid="nav-rail-toggle" onClick={() => setRail(!asRail)}>
                {asRail ? <PanelLeftOpen size={18} aria-hidden="true" /> : <PanelLeftClose size={18} aria-hidden="true" />}
              </Button>
            </div>
            {nav}
          </div>
        )}
        {!wide && <aside id="menu-sheet" ref={sheet} hidden={!menu} aria-label="Menu" onKeyDown={(e) => { if (e.key === "Escape") closeMenu(); }} className="absolute inset-x-0 top-0 z-40 bg-surface border-b border-border shadow-pop max-h-[85vh] overflow-y-auto" data-testid="menu-sheet">{session}{nav}</aside>}
        <main id="main" tabIndex={-1} className="flex-1 min-w-0 p-4 md:p-6 xl:p-8 outline-none">
          <div className="mx-auto max-w-[1200px]">{children}</div>
        </main>
      </div>
    </div>
  );
}
