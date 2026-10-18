import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";

function respond(status: number, headers: Record<string, string>) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "refused" }), { status, headers: { "Content-Type": "application/json", ...headers } })));
}

describe("X-Request-Id on a failed request", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("keeps the server's id so a page can show it", async () => {
    respond(409, { "X-Request-Id": "0123456789abcdef0123456789abcdef" });
    const e = await api("POST", "/v2/x", {}).catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect((e as ApiError).requestId).toBe("0123456789abcdef0123456789abcdef");
  });
  it("is null without the header and ignores a value that is not a server id", async () => {
    respond(500, {});
    expect(((await api("GET", "/v2/x").catch((x) => x)) as ApiError).requestId).toBeNull();
    respond(500, { "X-Request-Id": "<script>" });
    expect(((await api("GET", "/v2/x").catch((x) => x)) as ApiError).requestId).toBeNull();
  });
});
