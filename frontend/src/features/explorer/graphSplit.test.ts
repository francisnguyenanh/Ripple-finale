import { describe, expect, it } from "vitest";
import { looseSplit } from "./graphSplit";

const n = (id: string) => ({ id });

describe("looseSplit (SCN-07, loaded and shown graph only)", () => {
  it("keeps counts: linked + loose = shown", () => {
    const shown = [n("a"), n("b"), n("c"), n("d")];
    const r = looseSplit([{ from: "a", to: "b" }], shown);
    expect(r.linked.map((x) => x.id)).toEqual(["a", "b"]);
    expect(r.loose.map((x) => x.id)).toEqual(["c", "d"]);
    expect(r.linked.length + r.loose.length).toBe(shown.length);
  });
  it("an edge to an item the filter hides makes it loose here, and says so", () => {
    const r = looseSplit([{ from: "a", to: "hidden" }], [n("a"), n("b")]);
    expect(r.loose.map((x) => x.id)).toEqual(["a", "b"]);
    expect([...r.hiddenLink]).toEqual(["a"]);
  });
  it("a self edge is not a link to another item; all loose when nothing links", () => {
    const r = looseSplit([{ from: "a", to: "a" }], [n("a"), n("b")]);
    expect(r.linked).toEqual([]);
    expect(r.loose.length).toBe(2);
  });
  it("an empty graph splits into nothing", () => {
    expect(looseSplit([], [])).toMatchObject({ linked: [], loose: [] });
  });
});
