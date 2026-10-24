import { describe, expect, it } from "vitest";
import { aiWhy, kindEnabled, NOT_ENABLED, parseAiKinds } from "./aiKinds";

describe("the server's AI role allowlist on the page", () => {
  it("reads all, none and a list; a missing meta leaves the decision to the provider switch", () => {
    expect(parseAiKinds(undefined)).toBe("all");
    expect(parseAiKinds("all")).toBe("all");
    expect(kindEnabled(parseAiKinds("none"), "knowledge.qa")).toBe(false);
    const k = parseAiKinds("knowledge.qa,extraction");
    expect(kindEnabled(k, "extraction")).toBe(true);
    expect(kindEnabled(k, "change.draft")).toBe(false);
  });
  it("says the switch first, then the allowlist, and nothing when usable", () => {
    const k = parseAiKinds("knowledge.qa");
    const s = (off: boolean) => ({ aiOff: off, aiKind: (x: string) => !off && kindEnabled(k, x) });
    expect(aiWhy(s(true), "knowledge.qa")).toContain("switched off");
    expect(aiWhy(s(false), "change.draft")).toBe(NOT_ENABLED);
    expect(aiWhy(s(false), "knowledge.qa")).toBeUndefined();
  });
});
