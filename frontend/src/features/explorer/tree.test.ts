import { describe, expect, it } from "vitest";
import type { GEdge, GNode } from "./layout";
import { buildTree, filterTree, openPath } from "./tree";

const n = (id: string, type: string, state = "observed", label = id, detail?: Record<string, unknown>): GNode => ({ id, type, label, state, detail });
const e = (from: string, to: string, type: string): GEdge => ({ id: `${type}-${from}-${to}`, from, to, type });

const nodes: GNode[] = [
  n("a1", "artifact", "observed", "policy/a.md"), n("a2", "artifact", "not-analysed", "policy/b.yaml"), n("a3", "artifact", "not-analysed", "app/c.py"),
  n("r1", "candidate", "pending-confirmation", "r1", { predicate: "limit", value: 1000 }), n("r2", "candidate", "confirmed", "r2", { predicate: "limit", value: 900 }),
  n("e1", "evidence", "observed", "line 3", { quote: "Managers approve up to 1,000,000 JPY" }),
  n("f1", "finding", "conflict", "CONFLICT", { kind: "CONFLICT", reason: "limits differ" }), n("d1", "decision", "confirmed", "confirm by x"), n("f2", "finding", "gap", "GAP"),
];
const edges: GEdge[] = [
  e("a1", "r1", "source-of"), e("a2", "r2", "source-of"), e("e1", "r1", "evidence-of"),
  e("r1", "f1", "involved-in"), e("r2", "f1", "involved-in"), e("d1", "r2", "decided-by"), e("r1", "r2", "relation"), e("r2", "r1", "relation"),
];

describe("explorer tree projection", () => {
  const t = buildTree(nodes, edges);
  it("lists every file, also those without rules or never analysed", () => {
    expect(t.files.map((f) => f.id)).toEqual(["a3", "a1", "a2"].sort((x, y) => nodes.find((q) => q.id === x)!.label.localeCompare(nodes.find((q) => q.id === y)!.label)));
    expect(t.files.find((f) => f.id === "a3")!.rules).toEqual([]);
  });
  it("keeps one canonical record per rule and counts an issue that spans two files once", () => {
    expect(t.files.flatMap((f) => f.rules).map((r) => r.id).sort()).toEqual(["r1", "r2"]);
    const under = t.files.flatMap((f) => f.rules).filter((r) => r.issues.some((i) => i.id === "f1"));
    expect(under.map((r) => r.id).sort()).toEqual(["r1", "r2"]);
    expect(t.totals).toMatchObject({ files: 3, rules: 2, issues: 2, decisions: 1, confirmed: 1, pending: 1, evidence: 1 });
  });
  it("shows an issue nothing points at instead of dropping it", () => {
    expect(t.looseIssues.map((x) => x.id)).toEqual(["f2"]);
  });
  it("treats links between rules as references, in both directions, with no recursion", () => {
    expect(t.files.flatMap((f) => f.rules).find((r) => r.id === "r1")!.related.length).toBe(2);
  });
  it("opens the ancestors of any node for a deep link", () => {
    expect(openPath(t, "e1")).toEqual(["a1", "r1"]);
    expect(openPath(t, "d1")).toEqual(["a2", "r2"]);
    expect(openPath(t, "a1")).toEqual(["a1"]);
    expect(openPath(t, "nope")).toEqual([]);
  });
  it("search keeps the way down to a match and does not change the data", () => {
    const v = filterTree(t, "1,000,000", "")!;
    expect([...v.hits]).toEqual(["e1"]);
    expect([...v.rules]).toEqual(["r1"]);
    expect([...v.files]).toEqual(["a1"]);
    expect(filterTree(t, "", "")).toBeNull();
    expect(t.totals.rules).toBe(2);
  });
  it("a state filter only matches rules and files in that state", () => {
    const v = filterTree(t, "", "confirmed")!;
    expect([...v.rules]).toEqual(["r2"]);
  });
});
