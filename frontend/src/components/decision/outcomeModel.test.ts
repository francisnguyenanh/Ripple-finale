import { describe, expect, it } from "vitest";
import { CASE_LIMIT, outcomeOf } from "./outcomeModel";
import { signed } from "./SimulationOutcome";

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

describe("signed difference (SCN-06)", () => {
  it("shows the direction of a count change without calling it better or worse", () => {
    expect(signed(3)).toBe("+3");
    expect(signed(-2)).toBe("−2");
    expect(signed(0)).toBe("0");
  });
});
