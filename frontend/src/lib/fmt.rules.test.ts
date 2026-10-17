import { describe, expect, it } from "vitest";
import { ruleLine, ruleTitle } from "./fmt";

describe("operator rules keep their subject and direction", () => {
  it("tells the two emergency branches apart", () => {
    expect(ruleLine("request.net_amount_jpy", "less_than_or_equal_to", "0")).toBe("Request net amount jpy ≤ 0");
    expect(ruleLine("request.net_amount_jpy", "greater_than_or_equal_to", "1,000,000 JPY")).toBe("Request net amount jpy ≥ 1,000,000 JPY");
    expect(ruleLine("request.quantity", "less_than_or_equal_to", "0")).not.toBe(ruleLine("request.net_amount_jpy", "less_than_or_equal_to", "0"));
  });
  it("leaves ordinary predicates as they were", () => {
    expect(ruleTitle("Manager approval", "standard_manager_approval_threshold")).toBe("Standard manager approval threshold");
    expect(ruleLine("Manager approval", "standard_manager_approval_threshold", "500,000 JPY")).toBe("Standard manager approval threshold = 500,000 JPY");
  });
  it("falls back to the predicate when there is no subject", () => {
    expect(ruleTitle("", "less_than_or_equal_to")).toBe("Less than or equal to");
  });
});
