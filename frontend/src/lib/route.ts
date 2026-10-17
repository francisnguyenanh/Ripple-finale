// Hash routes identical in meaning to the legacy app: #/ws/<id>/<page>[/<sub>][?k=v]
export interface Route { ws: string | null; page: string; sub: string; params: Record<string, string> }

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#/, "");
  const q = raw.indexOf("?");
  const path = q >= 0 ? raw.slice(0, q) : raw;
  const params: Record<string, string> = {};
  if (q >= 0) new URLSearchParams(raw.slice(q + 1)).forEach((v, k) => { params[k] = v; });
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "ws" && parts[1]) {
    return { ws: decodeURIComponent(parts[1]), page: parts[2] || "overview", sub: parts[3] ? decodeURIComponent(parts[3]) : "", params };
  }
  return { ws: null, page: "", sub: "", params };
}

export function hashFor(ws: string, page: string, sub?: string, params?: Record<string, string | undefined>): string {
  let h = `#/ws/${encodeURIComponent(ws)}/${page}${sub ? "/" + encodeURIComponent(sub) : ""}`;
  const pairs = Object.entries(params ?? {}).filter(([, v]) => v !== undefined && v !== "");
  if (pairs.length) h += "?" + pairs.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v as string)}`).join("&");
  return h;
}

/** Change the URL without adding a history entry, and tell the app (replaceState alone fires no event). */
export function replaceHash(hash: string): void {
  history.replaceState(null, "", hash);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}
