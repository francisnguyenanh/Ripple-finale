import { describe, expect, it } from "vitest";
import { filterCounts, triage } from "./gapTriage";

const L = { missing_actor: "Who acts?", conflicting_facts: "Sources disagree" };
const G = [
  { gap_id: "g1", kind: "conflicting_facts", priority: "normal" },
  { gap_id: "g2", kind: "missing_actor", priority: "normal" },
  { gap_id: "g3", kind: "conflicting_facts", priority: "critical" },
  { gap_id: "g4", kind: "new_kind" },
  { gap_id: "g5", kind: "missing_actor", priority: "critical" },
];

describe("triage", () => {
  it("groups by question kind in a fixed order, the questions that need an answer first, every loaded question exactly once", () => {
    const groups = triage(G, L);
    expect(groups.map((g) => [g.key, g.items.map((i) => i.gap_id), g.critical])).toEqual([
      ["missing_actor", ["g5", "g2"], 1], ["conflicting_facts", ["g3", "g1"], 1], ["new_kind", ["g4"], 0]]);
    expect(groups.flatMap((g) => g.items).length).toBe(G.length);
    expect(groups[2].label).toBe("Other questions");
  });
  it("a filter only hides loaded questions and its counts match what it shows", () => {
    expect(triage(G, L, "critical").flatMap((g) => g.items.map((i) => i.gap_id))).toEqual(["g5", "g3"]);
    expect(triage(G, L, "kind:missing_actor").map((g) => g.key)).toEqual(["missing_actor"]);
    const counts = Object.fromEntries(filterCounts(G).map((c) => [c.key, c.count]));
    expect(counts).toEqual({ "": 5, critical: 2, "kind:missing_actor": 2, "kind:conflicting_facts": 2, "kind:new_kind": 1 });
    for (const [k, n] of Object.entries(counts)) expect(triage(G, L, k as never).flatMap((g) => g.items).length).toBe(n);
  });
  it("no question gives no group", () => {
    expect(triage([], L)).toEqual([]);
    expect(filterCounts([])).toEqual([{ key: "", count: 0 }, { key: "critical", count: 0 }]);
  });
});
