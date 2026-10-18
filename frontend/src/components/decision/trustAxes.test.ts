import { describe, expect, it } from "vitest";
import { axisDisplay } from "./trustAxes";

describe("axisDisplay", () => {
  it("keeps the server wording, only turning underscores into spaces", () => {
    expect(axisDisplay("applied_pending_readback")).toEqual({ text: "applied pending readback", known: true });
    expect(axisDisplay("not_tested")).toEqual({ text: "not tested", known: true });
  });
  it("a missing axis is unavailable, never the word undefined", () => {
    for (const v of [undefined, null, "", "undefined", "null", "  "]) expect(axisDisplay(v)).toEqual({ text: "Unavailable", known: false });
  });
});
