// X3a (read-only): how this workspace's change hints are delivered. Polling is the delivery path in this build; the projection status below says whether a
// hint sink is configured and how far behind it is. Nothing here starts, runs or retries anything; there is no Firestore listener in this build.
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Callout, Muted, Tech, TechField } from "@/components/bits";
import type { Rec } from "@/features/changes/types";

const POLL_MS = 20000;
const SINK_WORDS: Record<string, string> = {
  off: "No hint sink is configured: pages learn about changes by checking the server every few seconds.",
  memory: "Hints are kept in this server's memory only (a local test setup). Pages still check the server by themselves.",
  "firestore-emulator": "Hints are written to a local Firestore emulator (test only). Pages in this build still check the server by themselves.",
};
const q = encodeURIComponent;

function lagText(n: unknown, what: string): string {
  return typeof n === "number" ? `${n} ${what} not yet passed on` : `${what}: unknown`;
}

export function LiveStatus({ ws }: { ws: string }) {
  const [st, setSt] = React.useState<Rec | null>(null);
  const [feeds, setFeeds] = React.useState<Rec | null>(null);
  const [err, setErr] = React.useState<ApiError | null>(null);
  const [loaded, setLoaded] = React.useState(false);
  const load = React.useCallback(async () => {
    try {
      const [a, b] = await Promise.all([api<Rec>("GET", `/v2/projection/status?workspace_id=${q(ws)}`), api<Rec>("GET", `/v2/projection/feeds?workspace_id=${q(ws)}`)]);
      setSt(a); setFeeds(b); setErr(null);
    } catch (e) {
      if (e instanceof ApiError) { setErr(e); setSt(null); setFeeds(null); } else throw e;
    } finally { setLoaded(true); }
  }, [ws]);
  React.useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);

  const delivery = <p data-testid="live-delivery">Delivery in this build: <strong>the page checks the server by itself</strong> (polling). A live listener is not part of this build.</p>;
  if (!loaded) return <div data-testid="live-status" data-state="loading" className="grid gap-2 text-sm">{delivery}<Muted>Reading the status...</Muted></div>;
  if (err) {
    const denied = err.status === 403 || err.status === 404;
    return (
      <div data-testid="live-status" data-state={denied ? "denied" : "unavailable"} className="grid gap-2 text-sm">
        {delivery}
        <Callout tone={denied ? "info" : "warn"} testid="live-unavailable">
          {denied ? "The delivery status is not available to your account." : "The delivery status could not be read right now. This is not the same as everything being up to date."}
          {err.requestId ? <span className="block text-xs text-muted" data-testid="live-request-id">Reference: {err.requestId}</span> : null}
          {!denied && <div><Button onClick={() => void load()} data-testid="live-retry">Try again</Button></div>}
        </Callout>
      </div>
    );
  }
  const sink = String(st?.sink ?? "unknown");
  const lag = (st?.lag ?? {}) as Rec;
  const cp = (st?.checkpoint ?? {}) as Rec;
  const behind = typeof lag.outbox_rows === "number" && typeof lag.documents === "number" ? lag.outbox_rows + lag.documents : null;
  return (
    <div data-testid="live-status" data-state="read" data-sink={sink} className="grid gap-2 text-sm">
      {delivery}
      <p data-testid="live-sink">Hint sink: {SINK_WORDS[sink] ?? `not recognised (${sink}); treat updates as delivered by polling only.`}</p>
      {sink !== "off" && (
        <p data-testid="live-lag" data-behind={behind === null ? "unknown" : String(behind)}>
          {behind === 0 ? "Up to date with the committed state." : behind === null ? "How far the hints are behind is unknown." : `Behind the committed state: ${lagText(lag.outbox_rows, "events")}, ${lagText(lag.documents, "documents")}.`}
        </p>
      )}
      <p data-testid="live-feeds">Hint feeds your role may read: {Array.isArray(feeds?.feeds) ? (feeds!.feeds as unknown[]).length : "unknown"}. A hint never carries data: the page reads the server again, with your own rights.</p>
      <Muted>{String(st?.statement ?? "")}</Muted>
      <Tech>
        <TechField label="sink" value={sink} />
        <TechField label="checkpoint updated" value={cp.updated_at ?? "never"} />
        <TechField label="lag (outbox rows / documents)" value={`${lag.outbox_rows ?? "?"} / ${lag.documents ?? "?"}`} />
        <TechField label="feeds" value={Array.isArray(feeds?.feeds) ? (feeds!.feeds as string[]).join(", ") || "none" : "unknown"} />
        <TechField label="claim" value={feeds?.claim ?? "?"} />
      </Tech>
    </div>
  );
}
