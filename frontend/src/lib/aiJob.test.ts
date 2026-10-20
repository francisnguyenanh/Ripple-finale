import { describe, expect, it } from "vitest";
import { aiJobBadge, aiJobPending, contribution, reasonsCount } from "./aiJob";

describe("aiJob adapters", () => {
  it("names the job scope and keeps unknown states neutral", () => {
    expect(aiJobBadge("done")).toEqual({ status: "success", label: "Job finished" });
    expect(aiJobBadge("waiting_for_approval").status).toBe("waiting");
    expect(aiJobBadge("weird")).toEqual({ status: "info", label: "State: weird" });
    expect(aiJobBadge(undefined).status).toBe("info");
  });
  it("polls only while the job can change by itself", () => {
    expect(aiJobPending("running")).toBe(true);
    expect(aiJobPending("generating")).toBe(true);
    expect(aiJobPending("done")).toBe(false);
    expect(aiJobPending("failed")).toBe(false);
  });
  it("never shows a refused output or a fallback as an AI result", () => {
    expect(contribution({ contribution: "retained", retained: 2, rejected: 1 }).label).toContain("2 kept, 1 refused");
    expect(contribution({ contribution: "rejected" }).status).toBe("failed");
    expect(contribution({ contribution: "retained", retained: 1, degraded: "deterministic_fallback" }).status).toBe("partial");
    expect(contribution(null).label).toBe("Model contribution unavailable");
  });
  it("counts refusals by reason without the detail after the colon", () => {
    expect(reasonsCount([{ reason: "figure_not_in_cited_facts:42" }, { reason: "figure_not_in_cited_facts:7" }, { reason: "duplicate" }])).toEqual([["figure_not_in_cited_facts", 2], ["duplicate", 1]]);
    expect(reasonsCount("x")).toEqual([]);
  });
});
