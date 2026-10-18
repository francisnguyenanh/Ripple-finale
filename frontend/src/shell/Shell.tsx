import { WindowBanner } from "./WindowBanner";
import * as React from "react";
import { Menu, LayoutDashboard, FileText, Network, HelpCircle, MessageSquare, GitCompare, History, ShieldCheck, Route, LogOut, Briefcase, Wrench, BookOpen } from "lucide-react";
import { useSession, type Session } from "@/session";
import { NAV } from "@/lib/copy";
import { hashFor } from "@/lib/route";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/StatusBadge";

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

export function Shell({ children }: { children: React.ReactNode }) {
  const s = useSession();
  const [menu, setMenu] = React.useState(false);
  const menuBtn = React.useRef<HTMLButtonElement>(null);
  const sheet = React.useRef<HTMLElement>(null);
  const wide = useWide();
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

  // Workspace, role, revisions, demo session, view and sign-out: in the header on a wide screen, at the top of the menu on a small one (rendered once).
  const session = (
    <div className={wide ? "contents" : "grid gap-3 p-3 border-b border-border"} data-testid="session-controls">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">Workspace</span>
          <select className="border border-border rounded-sm bg-bg px-2 py-1.5 min-h-9" data-testid="ws-select" value={ws ?? ""}
            onChange={(e) => { if (e.target.value) location.hash = hashFor(e.target.value, s.route.page || "overview", undefined, { mode: s.mode === "technical" ? "technical" : undefined }); }}>
            <option value="">Select a workspace</option>
            {s.workspaces.map((w) => <option key={w.workspace_id} value={w.workspace_id}>{w.display_name || w.workspace_id}</option>)}
          </select>
        </label>
        {mine && mine.roles.length > 0 && <span className="text-sm text-muted">Your role: <span data-testid="workspace-roles">{mine.roles.join(", ")}</span></span>}
        {s.mode === "technical" && s.head && (
          <span className="text-xs text-muted flex gap-3" data-testid="revision-chips">
            <span>Source revision: <code data-testid="head-source-revision">{s.head.source_revision_id ?? "none"}</code></span>
            <span>Knowledge revision: <code data-testid="head-knowledge-revision">{s.head.knowledge_revision_id ?? "none"}</code></span>
          </span>
        )}
        {lease && (
          <a href={ws ? hashFor(ws, "support") : "#"} data-testid="lease-chip" data-state={lease.ok ? "ok" : "read-only"} className="text-xs rounded-full border border-border px-2 py-0.5 hover:bg-surface-2">{lease.text}</a>
        )}
        <div className={cn("flex flex-wrap items-center gap-3", wide && "ml-auto")}>
          <div role="group" aria-label="View: Business or Technical" className="inline-flex rounded-md border border-border overflow-hidden">
            {(["business", "technical"] as const).map((m) => (
              <button key={m} type="button" aria-pressed={s.mode === m} data-testid={`mode-${m}`} onClick={() => s.setMode(m)}
                className={cn("px-3 py-1.5 text-sm min-h-9 cursor-pointer inline-flex items-center gap-1.5", s.mode === m ? "bg-primary text-on-primary" : "bg-surface text-text hover:bg-surface-2")}>
                {m === "business" ? <Briefcase size={14} aria-hidden="true" /> : <Wrench size={14} aria-hidden="true" />}
                {m === "business" ? "Business" : "Technical"}
              </button>
            ))}
          </div>
          <span className={wide ? "text-sm hidden sm:inline" : "text-sm"}>Signed in as <strong data-testid="session-account">{s.account}</strong></span>
          <Button variant="secondary" onClick={() => void s.signOut()} data-testid="signout"><LogOut size={16} aria-hidden="true" />Sign out</Button>
        </div>
    </div>
  );

  const nav = (
    <nav aria-label="Main" className="grid gap-1 p-3">
      {NAV.map((n) => {
        const Icon = ICONS[n.id];
        const active = s.route.page === n.id;
        return (
          <a key={n.id} href={ws ? hashFor(ws, n.id, undefined, { mode: s.mode === "technical" ? "technical" : undefined }) : "#"} aria-current={active ? "page" : undefined} data-testid={`nav-${n.id}`}
            onClick={(e) => { if (active) { e.preventDefault(); setMenu(false); s.reload(); } }}
            className={cn("flex items-center gap-3 rounded-md px-3 py-2 text-sm min-h-10", active ? "bg-surface-2 font-semibold text-text" : "text-muted hover:bg-surface-2 hover:text-text", !ws && "pointer-events-none opacity-50")}>
            <Icon size={18} aria-hidden="true" />
            {n.label}
          </a>
        );
      })}
    </nav>
  );

  return (
    <div className="min-h-full flex flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-surface focus:p-2" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}>Skip to content</a>
      <header className="border-b border-border bg-surface flex flex-wrap items-center gap-3 px-4 py-2">
        {/* The name stays "Menu" (open/closed is aria-expanded); what is inside is a description, so the name never competes with the Workspace control. */}
        {!wide && (
          <Button ref={menuBtn} variant="ghost" className="px-2" aria-label="Menu" aria-describedby="menu-hint" aria-expanded={menu} aria-controls="menu-sheet" onClick={() => setMenu((v) => !v)} data-testid="menu-button">
            <Menu size={20} aria-hidden="true" />
            <span id="menu-hint" className="sr-only">Workspace, role, view and sign-out are inside.</span>
          </Button>
        )}
        <span className="text-lg font-semibold">Ripple</span>
        {wide ? session : (
          // Small screens: one row that still says where you are and in which view; the controls themselves are in the menu.
          <span data-testid="header-context" className="min-w-0 flex-1 flex items-center gap-2 text-sm">
            <span className="truncate" title={mine?.display_name || ws || undefined}>{mine?.display_name || ws || "No workspace selected"}</span>
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs" data-testid="header-mode" data-mode={s.mode}>
              {s.mode === "technical" ? <Wrench size={12} aria-hidden="true" /> : <Briefcase size={12} aria-hidden="true" />}{s.mode === "technical" ? "Technical" : "Business"}
            </span>
            {mine && mine.roles.length > 0 && <span className="truncate text-xs text-muted" data-testid="header-role">{mine.roles.join(", ")}</span>}
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
        {wide && <div className="w-60 shrink-0 border-r border-border bg-surface">{nav}</div>}
        {!wide && <aside id="menu-sheet" ref={sheet} hidden={!menu} aria-label="Menu" onKeyDown={(e) => { if (e.key === "Escape") closeMenu(); }} className="absolute inset-x-0 top-0 z-40 bg-surface border-b border-border shadow-pop max-h-[85vh] overflow-y-auto" data-testid="menu-sheet">{session}{nav}</aside>}
        <main id="main" tabIndex={-1} className="flex-1 min-w-0 p-4 md:p-6 xl:p-8 outline-none">
          <div className="mx-auto max-w-[1200px]">{children}</div>
        </main>
      </div>
    </div>
  );
}
