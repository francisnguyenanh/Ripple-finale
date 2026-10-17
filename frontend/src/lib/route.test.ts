import { describe, expect, it } from "vitest";
import { hashFor, parseHash } from "./route";

describe("route", () => {
  it("round-trips a deep link", () => {
    const h = hashFor("ws 1", "changes", "abc", { chat: "c/1", empty: "" });
    expect(h).toBe("#/ws/ws%201/changes/abc?chat=c%2F1");
    expect(parseHash(h)).toEqual({ ws: "ws 1", page: "changes", sub: "abc", params: { chat: "c/1" } });
  });
  it("defaults to overview and tolerates junk", () => {
    expect(parseHash("#/ws/a").page).toBe("overview");
    expect(parseHash("#nonsense").ws).toBeNull();
  });
});
