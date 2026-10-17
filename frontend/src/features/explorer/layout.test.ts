import { describe, expect, it } from "vitest";
import { COL_W, layout, matches, type GNode } from "./layout";

const n = (id: string, type: string, state = "observed", label = id): GNode => ({ id, type, label, state });

describe("explorer layout", () => {
  it("puts each kind in its own column and stacks rows without overlap", () => {
    const pos = layout([n("a", "artifact"), n("b", "artifact"), n("r", "candidate"), n("f", "finding")]);
    expect(pos.a.x).toBe(0);
    expect(pos.b.x).toBe(0);
    expect(pos.b.y).toBeGreaterThan(pos.a.y);
    expect(pos.r.x).toBe(2 * COL_W);
    expect(pos.f.x).toBe(3 * COL_W);
  });
  it("filters by state and text but never silently drops a node from the data", () => {
    const nodes = [n("a", "artifact", "observed", "policy.md"), n("r", "candidate", "confirmed", "limit")];
    expect(nodes.filter((x) => matches(x, "", "confirmed")).map((x) => x.id)).toEqual(["r"]);
    expect(nodes.filter((x) => matches(x, "POLICY", "")).map((x) => x.id)).toEqual(["a"]);
    expect(nodes.length).toBe(2);
  });
});
