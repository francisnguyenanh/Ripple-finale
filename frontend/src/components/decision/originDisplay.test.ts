import { describe, expect, it } from "vitest";
import { originDisplay } from "./originDisplay";

describe("originDisplay", () => {
  it("keeps AI and evidence in separate families and never claims correctness", () => {
    expect(originDisplay("ai").family).toBe("ai");
    expect(originDisplay("source").family).toBe("evidence");
    expect(originDisplay("engine").family).toBe("evidence");
    for (const o of ["ai", "source", "engine"]) expect(originDisplay(o).note).not.toMatch(/verified|correct|approved|accepted/i);
    expect(originDisplay("ai").note).toMatch(/not a decision/);
  });
  it("shows an unknown origin as neutral, never as evidence", () => {
    for (const o of [undefined, null, "sim", "", 3]) {
      const d = originDisplay(o);
      expect(d.known).toBe(false);
      expect(d.family).toBe("unknown");
    }
  });
});
