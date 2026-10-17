import { describe, expect, it } from "vitest";
import { intentPhrase, newId, num, parseValue, scopeText, valueText } from "./fmt";

describe("fmt", () => {
  it("formats exact values with units, never inventing an operator", () => {
    expect(valueText(1000000, "net goods amount", "JPY")).toBe("1,000,000 JPY");
    expect(valueText("500000", "net goods amount", "")).toBe("500,000 net goods amount");
    expect(valueText(null)).toBe("not stated");
    expect(num("1,000")).toBe("1,000");
  });
  it("describes scope in words", () => {
    expect(scopeText({ order_type: "standard" })).toBe("order type: standard");
    expect(scopeText(undefined)).toBe("no scope stated");
  });
  it("parses numbers as numbers and keeps text", () => {
    expect(parseValue(" 30 ")).toBe(30);
    expect(parseValue("0.5")).toBe(0.5);
    expect(parseValue("true")).toBe(true);
    expect(parseValue("fourteen")).toBe("fourteen");
  });
  it("phrases a saved request for people", () => {
    expect(intentPhrase("PARSED")).toContain("waiting for the person who made it");
  });
  it("makes distinct ids", () => {
    expect(newId("ui-draft")).not.toBe(newId("ui-draft"));
    expect(newId("ui-draft")).toMatch(/^ui-draft-[0-9a-f]{16}$/);
  });
});
