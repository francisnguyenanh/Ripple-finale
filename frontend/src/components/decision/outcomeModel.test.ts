import { describe, expect, it } from "vitest";
import { CASE_LIMIT, outcomeOf } from "./outcomeModel";

describe("outcomeOf", () => {
  it("lists every outcome of either side, a missing side as 0 cases, and keeps the full changed count when the cases are capped", () => {
    const h = { transactions: 120, case: "route", changed_count: 75, unexplained_changes: 0, by_route_before: { manager: 3 }, by_route_after: { finance: 2 },
      changed: Array.from({ length: 75 }, (_, i) => ({ id: `R${i}` })) };
    const o = outcomeOf(h);
    expect(o.routes).toEqual([{ key: "manager", before: 3, after: 0 }, { key: "finance", before: 0, after: 2 }]);
    expect(o.cases.length).toBe(CASE_LIMIT);
    expect(o.changedCount).toBe(75);
    expect(o.invoices).toBe(false);
  });
  it("an empty run has no rows and no cases", () => {
    expect(outcomeOf({ transactions: 0, case: "match" })).toMatchObject({ routes: [], cases: [], changedCount: 0, invoices: true });
  });
});
