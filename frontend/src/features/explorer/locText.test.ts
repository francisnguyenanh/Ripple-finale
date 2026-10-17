import { describe, expect, it } from "vitest";
import { locText } from "./explorerText";

describe("locText", () => {
  it("says where code evidence sits in words (Business) and keeps the stored locator (Technical)", () => {
    const loc = { type: "code_span", qualname: "route_emergency", line_start: 8, line_end: 48 };
    expect(locText(loc)).toBe("function route_emergency, lines 8-48");
    expect(locText(JSON.stringify(loc))).toBe("function route_emergency, lines 8-48");
    expect(locText({ ...loc, qualname: "Router.route", line_start: 5, line_end: 6 })).toBe("method Router.route, lines 5-6");
    expect(locText({ type: "code_span", qualname: "<module>", line_start: 1, line_end: 1 })).toBe("module-level code, lines 1-1");
    expect(locText({ ...loc, part: 2, parts: 3 })).toBe("function route_emergency, lines 8-48, part 2 of 3");
    expect(locText(loc, true)).toBe(JSON.stringify(loc));
  });
  it("keeps the wording of the other locators", () => {
    expect(locText({ type: "key_path", key_path: "routing_rules.standard_manager_threshold_jpy" })).toBe('setting "routing rules > standard manager threshold jpy"');
    expect(locText({ type: "line_span", line_start: 3, line_end: 4 })).toBe("line start 3, line end 4");
  });
});
