import { describe, expect, it } from "vitest";
import { citedBy, interpretationCheck, parseMetricRef } from "./metricRefs";

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

describe("interpretation check badge", () => {
  it("counts what the server kept and refused, and never passes a skipped or empty check", () => {
    expect(interpretationCheck({ statements: [{}, {}], rejected: [] })).toMatchObject({ status: "info", kept: 2, refused: 0 });
    expect(interpretationCheck({ statements: [{}], rejected: [{}, {}] })).toMatchObject({ status: "partial", label: "Metric references checked by the server: 1 kept, 2 refused" });
    expect(interpretationCheck({ statements: [], rejected: [{}] })).toMatchObject({ status: "partial", kept: 0, refused: 1 });
    for (const x of [null, undefined, { statements: [], rejected: [] }, { status: "skipped", statements: [{}] }]) expect(interpretationCheck(x).status).toBe("empty");
  });
});

