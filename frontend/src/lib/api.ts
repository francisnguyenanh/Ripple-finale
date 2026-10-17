// Typed same-origin API client. Credentials stay in the httpOnly session cookie (never in
// JS-readable storage). The browser sets Origin itself. Mutations are never retried automatically.

export class ApiError extends Error {
  constructor(public status: number, public body: Record<string, unknown>, public network = false, public retryAfter: number | null = null) {
    super("api");
  }
}

export type Method = "GET" | "POST";

// Called when a signed-in session turns out to be dead (401 on anything but the sign-in probe itself).
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

export async function api<T>(
  method: Method,
  path: string,
  body?: unknown,
  opts: { signal?: AbortSignal; idempotencyKey?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = { "X-Ripple-Judge": "1" };
  const init: RequestInit = { method, credentials: "same-origin", headers, signal: opts.signal };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let resp: Response;
  try {
    resp = await fetch(path, init);
  } catch {
    throw new ApiError(0, {}, true);
  }
  let data: unknown = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (!resp.ok) {
    if (resp.status === 401 && !path.startsWith("/v2/auth/") && onUnauthorized) onUnauthorized();
    const ra = parseInt(resp.headers.get("Retry-After") ?? "", 10);
    throw new ApiError(resp.status, (data && typeof data === "object" ? data : {}) as Record<string, unknown>, false, Number.isFinite(ra) ? ra : null);
  }
  return data as T;
}

/** Plain-English message for an API failure. */
export function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return "Something went wrong in the page. Nothing was changed.";
  const b = e.body;
  if (e.network) return "The server could not be reached. Nothing was changed.";
  const wait = e.retryAfter ? ` Try again in ${e.retryAfter} second${e.retryAfter === 1 ? "" : "s"}.` : "";
  if (e.status === 429) return `Too many requests from this session.${wait || " Wait a moment and try again."} Nothing was lost and nothing was changed.`;
  if (e.status === 503) return `The service is busy or paused a safety counter for a moment.${wait || " Try again shortly."} Nothing was changed.`;
  if (["provider_disarmed", "allocation_exhausted", "budget_exceeded", "concurrency_limit", "ai_switched_off", "member_ai_off", "person_quota"].includes(String(b.code)) && typeof b.error === "string")
    return `${b.error}${typeof b.next_action === "string" ? " " + b.next_action : ""}`;
  if (e.status >= 500) return `The server could not complete the request (HTTP ${e.status}). Nothing was changed.`;
  if (b.code === "sandbox_lease_required")
    return (
      "You cannot change anything right now: " +
      (b.held_by_other ? "another demo session is running." : "no demo session has been started yet.") +
      " Nothing was changed."
    );
  if ((b.code === "provider_disarmed" || b.code === "allocation_exhausted") && typeof b.error === "string")
    return `${b.error}${typeof b.next_action === "string" ? " " + b.next_action : ""}`;
  if (e.status === 403) return "Your account is not allowed to do this (HTTP 403). Nothing was changed.";
  if (e.status === 401) return "Your session has ended. Sign in again.";
  const msg = typeof b.error === "string" ? b.error : "The request was refused.";
  // The server's own reasons are shown as it sent them (they name exactly what blocked the request).
  const extra: string[] = [];
  if (Array.isArray(b.reasons) && b.reasons.length) extra.push(b.reasons.map(String).join(", "));
  if (Array.isArray(b.stale_fields) && b.stale_fields.length) extra.push(b.stale_fields.map(String).join(", "));
  if (typeof b.reason === "string" && b.reason) extra.push(b.reason);
  return `${msg}${extra.length ? `: ${extra.join("; ")}` : ""} (HTTP ${e.status}). Nothing was changed.`;
}

// ---- response types (checked against what the legacy shell reads; extra fields are tolerated)
export interface Me { account_id: string }
export interface WorkspaceInfo { workspace_id: string; roles: string[]; permissions: string[]; display_name?: string | null; description?: string | null; project_id?: string }
export interface Head {
  source_revision_id?: string | null;
  knowledge_revision_id?: string | null;
  decision_epoch?: number;
  version?: number;
}
export interface SandboxStatus {
  i_can_write?: boolean;
  lease?: { active?: boolean; holder?: string; expires_at?: string } | null;
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object";
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export function parseMe(v: unknown): Me {
  if (!isObj(v) || typeof v.account_id !== "string") throw new ApiError(502, { error: "unexpected session response" });
  return { account_id: v.account_id };
}

export function parseWorkspaces(v: unknown): WorkspaceInfo[] {
  if (!isObj(v) || !Array.isArray(v.workspaces)) throw new ApiError(502, { error: "unexpected workspace response" });
  return v.workspaces
    .filter((w): w is Record<string, unknown> => isObj(w) && typeof w.workspace_id === "string")
    .map((w) => ({
      workspace_id: w.workspace_id as string, roles: strings(w.roles), permissions: strings(w.permissions),
      display_name: typeof w.display_name === "string" ? w.display_name : null, description: typeof w.description === "string" ? w.description : null,
      project_id: typeof w.project_id === "string" ? w.project_id : undefined,
    }));
}

export const Auth = {
  me: async () => parseMe(await api("GET", "/v2/auth/me")),
  login: (account_id: string, credential: string) => api<unknown>("POST", "/v2/auth/login", { account_id, credential }),
  logout: () => api<unknown>("POST", "/v2/auth/logout", {}),
  workspaces: async () => parseWorkspaces(await api("GET", "/v2/workspaces")),
  head: (ws: string) => api<Head>("GET", `/v2/workspaces/${encodeURIComponent(ws)}/head`),
  sandbox: (ws: string) => api<SandboxStatus>("GET", `/v2/sandbox/status?workspace_id=${encodeURIComponent(ws)}`),
};
