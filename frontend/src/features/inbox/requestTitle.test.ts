import { describe, expect, it } from "vitest";
import { rowTitle, shortId } from "./requestTitle";

describe("rowTitle (SCN-15)", () => {
  it("keeps the request's own words", () => {
    const t = rowTitle({ title: "Raise the manager threshold", proposal_job_id: "job-1", params: { intent: "intent-1" }, family: "intent_flow" });
    expect(t).toMatchObject({ text: "Raise the manager threshold", fallback: false, id: "job-1" });
  });
  it("an id sent as the title is not a title: family label and short id, full id kept", () => {
    const id = "intent-e7275769e82247e4b7bbde3ad5ceff9e";
    const t = rowTitle({ title: id, proposal_job_id: id, params: { intent: id }, family: "intent_flow" });
    expect(t).toEqual({ text: "Change request", fallback: true, id, short: "intent-e72757…" });
  });
  it("matches the id of a parameter too (a proposal row whose title fell back to the intent id)", () => {
    const t = rowTitle({ title: "intent-abc", proposal_job_id: "job-9", params: { intent: "intent-abc" }, family: "intent_flow" });
    expect(t).toMatchObject({ fallback: true, id: "intent-abc" });
  });
  it("business change and unknown family; empty title", () => {
    expect(rowTitle({ title: "chg-1", proposal_job_id: "chg-1", family: "business_change" }).text).toBe("Business knowledge change");
    expect(rowTitle({ title: "", proposal_job_id: "x-1" }).text).toBe("Change request");
    expect(rowTitle({ title: null, proposal_job_id: "x-1", family: "other" })).toMatchObject({ fallback: true, short: "x-1" });
  });
  it("a title that merely contains an id is still the person's words", () => {
    expect(rowTitle({ title: "Fix intent-1 wording", proposal_job_id: "intent-1" }).fallback).toBe(false);
  });
  it("shortId", () => {
    expect(shortId("chg-12")).toBe("chg-12");
    expect(shortId("abcdefghijklmnopqrstuvwxyz")).toBe("abcdefghijkl…");
  });
});
