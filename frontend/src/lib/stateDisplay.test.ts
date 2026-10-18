import { describe, expect, it } from "vitest";
import { PILL_LABEL_MAX, stateTone } from "./stateDisplay";

describe("stateTone", () => {
  it("keeps the previous tone for every state the old pill knew", () => {
    for (const s of ["COMPLETED", "CONFIRMED", "APPROVED", "PASSED"]) expect(stateTone(s)).toBe("success");
    for (const s of ["FAILED", "REJECTED", "STALE"]) expect(stateTone(s)).toBe("danger");
    for (const s of ["NONE", "NOT_STARTED"]) expect(stateTone(s)).toBe("muted");
    expect(stateTone("RUNNING")).toBe("warning");
  });
  it("shows waiting-for-a-person states as warning, never success", () => {
    expect(stateTone("PENDING_APPROVAL")).toBe("warning");
    expect(stateTone("PARTIAL")).toBe("warning");
    expect(stateTone("ABSTAINED")).toBe("warning");
  });
  it("shows an unknown or empty token neutral, never as success", () => {
    expect(stateTone("SOMETHING_NEW")).toBe("muted");
    expect(stateTone("")).toBe("muted");
  });
  it("has a finite pill length limit", () => { expect(PILL_LABEL_MAX).toBeGreaterThan(10); });
});
