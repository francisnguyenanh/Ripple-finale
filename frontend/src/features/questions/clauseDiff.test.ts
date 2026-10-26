import { describe, expect, it } from "vitest";
import { CLAUSE_SEP, clauseDiff, splitClauses } from "./clauseDiff";

const B = "actor: manager | when: request net amount jpy at least 1000000 JPY | exceptions: none | scope: process: purchase";
const marks = (cs: { mark: string }[]) => cs.map((c) => c.mark);

describe("clauseDiff (SCN-20)", () => {
  it("unchanged", () => {
    const d = clauseDiff(B, B);
    expect(d.aligned).toBe("label");
    expect(marks(d.before)).toEqual(["same", "same", "same", "same"]);
    expect(marks(d.after)).toEqual(["same", "same", "same", "same"]);
  });
  it("a value change marks only that clause, on both sides, text untouched", () => {
    const A = B.replace("1000000", "1500000");
    const d = clauseDiff(B, A);
    expect(marks(d.before)).toEqual(["same", "changed", "same", "same"]);
    expect(marks(d.after)).toEqual(["same", "changed", "same", "same"]);
    expect(d.after.map((c) => c.text).join(CLAUSE_SEP)).toBe(A);                  // nothing reworded or reordered
    expect(d.before.map((c) => c.text).join(CLAUSE_SEP)).toBe(B);
  });
  it("actor and condition change", () => {
    const A = "actor: finance controller | when: supplier risk is HIGH | exceptions: none | scope: process: purchase";
    expect(marks(clauseDiff(B, A).after)).toEqual(["changed", "changed", "same", "same"]);
  });
  it("a clause inserted in the middle does not mark the rest as changed; a removed clause keeps its meaning as removed", () => {
    const A = "actor: manager | owner: finance | when: request net amount jpy at least 1000000 JPY | exceptions: none | scope: process: purchase";
    const d = clauseDiff(B, A);
    expect(marks(d.after)).toEqual(["same", "added", "same", "same", "same"]);
    const R = "actor: manager | when: request net amount jpy at least 1000000 JPY | scope: process: purchase";
    const r = clauseDiff(B, R);
    expect(marks(r.before)).toEqual(["same", "same", "removed", "same"]);
    expect(marks(r.after)).toEqual(["same", "same", "same"]);
  });
  it("reordered clauses with the same values stay unchanged, each side in its own order", () => {
    const A = "when: request net amount jpy at least 1000000 JPY | actor: manager | exceptions: none | scope: process: purchase";
    const d = clauseDiff(B, A);
    expect(marks(d.after)).toEqual(["same", "same", "same", "same"]);
    expect(d.after[0].text.startsWith("when:")).toBe(true);
  });
  it("duplicate or missing labels: only identical clauses are matched, no guessed pairs", () => {
    const b = "when: a | when: b | scope: x";
    const a = "when: a | when: c | scope: x";
    const d = clauseDiff(b, a);
    expect(d.aligned).toBe("exact");
    expect(marks(d.before)).toEqual(["same", "removed", "same"]);
    expect(marks(d.after)).toEqual(["same", "added", "same"]);
    expect(clauseDiff("free text | more", "free text | other").aligned).toBe("exact");
  });
  it("empty strings, a missing side or no delimiter are not structured (shown verbatim)", () => {
    expect(clauseDiff("", B).structured).toBe(false);
    expect(clauseDiff(B, null).structured).toBe(false);
    expect(clauseDiff("a sentence without clauses", "another sentence").structured).toBe(false);
    expect(splitClauses("x|y")).toBeNull();                                        // only the exact " | " delimiter splits
  });
  it("long content is kept whole", () => {
    const long = "when: " + "x".repeat(600);
    const d = clauseDiff(`actor: a | ${long}`, `actor: b | ${long}`);
    expect(d.after[1]).toEqual({ text: long, mark: "same" });
  });
});
