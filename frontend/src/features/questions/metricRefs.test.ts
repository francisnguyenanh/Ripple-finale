import { describe, expect, it } from "vitest";
import { citedBy, parseMetricRef } from "./metricRefs";

describe("metric references", () => {
  it("parses only the server's typed reference form", () => {
    expect(parseMetricRef("s2.sla_breach_share.delta")).toEqual({ ref: "s2.sla_breach_share.delta", row: "s2.sla_breach_share", scenario: 2, kpi: "sla_breach_share", part: "delta" });
    for (const bad of ["s0.x.delta", "2.x.delta", "s1.x.mean", "s1.X.delta", "", null, 3]) expect(parseMetricRef(bad)).toBeNull();
  });
  it("links rows by reference, never by equal values", () => {
    const st = [{ metric_refs: ["s1.cycle_time_mean_days.delta", "s1.cycle_time_mean_days.scenario"] }, { metric_refs: ["s2.cycle_time_mean_days.delta"] }, { metric_refs: "x" }];
    expect(citedBy(st)).toEqual({ "s1.cycle_time_mean_days": [1], "s2.cycle_time_mean_days": [2] });
  });
});
