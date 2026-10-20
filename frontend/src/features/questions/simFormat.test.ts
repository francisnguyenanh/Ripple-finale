import { describe, expect, it } from "vitest";
import { formatEngine } from "./simFormat";

describe("formatEngine", () => {
  it("shows a share as a percentage and its change in percentage points, without float noise or extra rounding", () => {
    expect(formatEngine(0.07, "share")).toBe("7%");
    expect(formatEngine(0.1234, "share")).toBe("12.34%");
    expect(formatEngine(0.0215, "share", true)).toBe("+2.15 percentage points");
    expect(formatEngine(-0.05, "share", true)).toBe("-5 percentage points");
  });
  it("keeps days with their unit and signs a positive change", () => {
    expect(formatEngine(12.3456, "days")).toBe("12.3456 days");
    expect(formatEngine(1.5, "days", true)).toBe("+1.5 days");
    expect(formatEngine(0, "days", true)).toBe("0 days");
  });
  it("never shows a missing value as zero", () => {
    expect(formatEngine(null, "share")).toBe("Unavailable");
    expect(formatEngine(undefined, "days", true)).toBe("Unavailable");
    expect(formatEngine({ mean: 0.25 }, "share")).toBe("25%");
  });
});
