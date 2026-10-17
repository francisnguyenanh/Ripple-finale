// Page-level feedback: a status live region, an error banner and a stale-state prompt.
// Polling never writes here and no notice ever moves focus.
import * as React from "react";
import { errorText, ApiError, setUnauthorizedHandler } from "./api";

interface Notice { text: string; code: string }
interface Notify {
  status: (text: string) => void;
  fail: (e: unknown, opts?: { stale?: boolean }) => void;
  clear: () => void;
  stale: (text: string) => void;
  staleText: string;
  dismissStale: () => void;
}

const Ctx = React.createContext<Notify | null>(null);
export const useNotify = () => {
  const n = React.useContext(Ctx);
  if (!n) throw new Error("no notify");
  return n;
};

export function NotifyProvider({ children, onSignedOut }: { children: React.ReactNode; onSignedOut: () => void }) {
  const [error, setError] = React.useState<Notice | null>(null);
  const [staleText, setStale] = React.useState("");

  React.useEffect(() => { setUnauthorizedHandler(onSignedOut); return () => setUnauthorizedHandler(null); }, [onSignedOut]);

  const value = React.useMemo<Notify>(() => ({
    status: (t) => {
      const el = document.getElementById("status");
      if (!el) return;
      el.textContent = "";
      window.setTimeout(() => { el.textContent = t; }, 30);
    },
    fail: (e, opts) => {
      if (e instanceof ApiError && e.status === 401) { onSignedOut(); return; }
      // A refusal for lack of permission means what the page offers may be out of date: read the member's permissions again (no timer).
      if (e instanceof ApiError && e.status === 403) window.dispatchEvent(new Event("ripple:permissions"));
      const code = e instanceof ApiError ? String(e.body.code ?? (e.status || "network")) : "client";
      setError({ text: errorText(e), code });
      if (opts?.stale && e instanceof ApiError && e.status === 409)
        setStale("The server refused this because the state you were looking at is out of date. Nothing was changed. Reload to see the current state.");
    },
    clear: () => setError(null),
    stale: (t) => setStale(t),
    staleText,
    dismissStale: () => setStale(""),
  }), [onSignedOut, staleText]);

  return (
    <Ctx.Provider value={value}>
      {error && (
        <div role="alert" data-testid="error-banner" data-error-code={error.code} className="mx-4 mt-3 rounded-md border border-danger bg-surface px-3 py-2 text-sm text-danger flex items-start gap-3">
          <span className="flex-1">{error.text}</span>
          <button type="button" className="underline cursor-pointer" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}
      {staleText && (
        <div data-testid="notice-stale" className="mx-4 mt-3 rounded-md border border-warning bg-surface px-3 py-2 text-sm flex items-start gap-3">
          <span className="flex-1">{staleText}</span>
          <button type="button" className="underline cursor-pointer" onClick={() => { setStale(""); window.dispatchEvent(new Event("ripple:reload")); }}>Reload state</button>
        </div>
      )}
      {children}
    </Ctx.Provider>
  );
}
