import { describe, expect, it } from "vitest";
import { buildSummary, ctaRepeatsFirstRow, nextStep, type Summary } from "./overviewModel";

const recent = (t: string) => t === "DECISION_RECORDED";
const head = { source_revision_id: "sr-1" };
const rev = { artifacts: [{}, {}], coverage: { complete: true, admission_complete: true } };
const overview = { claims: [{}, {}, {}], decisions: [{ active: true, action: "confirm" }, { active: false, action: "confirm" }], findings: [{ decision: null }, { decision: "x" }] };
const queue = { items: [{ question_id: "a" }, { question_id: "a" }, { question_id: "b" }] };
const audit = { events: [{ event_type: "DECISION_RECORDED" }, { event_type: "OTHER" }] };
const none = { count: 0 };

describe("buildSummary", () => {
  it("counts known figures", () => {
    const s = buildSummary({ head, rev, overview, queue, audit }, recent);
    expect(s).toMatchObject({ files: 2, coveragePartial: false, rules: 3, confirmed: 1, questions: 2, openFindings: 1 });
    expect(s.events).toHaveLength(1);
  });
  it("keeps a failed read as unknown, never as zero", () => {
    const s = buildSummary({ head, rev: null, overview: undefined, queue: undefined, audit: undefined }, recent);
    expect(s).toEqual({ files: null, coveragePartial: null, rules: null, confirmed: null, questions: null, openFindings: null, events: null, businessRules: null });
  });
  it("a workspace with no source revision really has zero files", () => {
    expect(buildSummary({ head: {}, overview, queue, audit }, recent).files).toBe(0);
  });
  it("marks partial coverage", () => {
    expect(buildSummary({ head, rev: { artifacts: [], coverage: { complete: false } }, overview, queue, audit }, recent).coveragePartial).toBe(true);
  });
});

describe("nextStep", () => {
  const ready: Summary = { files: 2, coveragePartial: false, rules: 3, confirmed: 1, questions: 0, openFindings: 0, events: [] };
  it("the server work list wins and names the first item", () => {
    expect(nextStep(ready, { count: 2, first: "approve the draft" }, "ready")).toMatchObject({ kind: "work", to: "changes" });
  });
  it("offers a new change only when every dependency is known", () => {
    expect(nextStep(ready, none, "ready").kind).toBe("change");
    expect(nextStep(ready, none, "failed").kind).toBe("unknown");
    expect(nextStep({ ...ready, questions: null }, none, "ready").kind).toBe("unknown");
    expect(nextStep({ ...ready, rules: null }, none, "ready").kind).toBe("unknown");
    expect(nextStep({ ...ready, files: null }, none, "ready").kind).toBe("unknown");
  });
  it("does not say no rules were read when the rules read failed", () => {
    expect(nextStep({ ...ready, rules: null }, none, "ready").kind).not.toBe("analyze");
    expect(nextStep({ ...ready, rules: 0 }, none, "ready").kind).toBe("analyze");
    expect(nextStep({ ...ready, rules: 0, businessRules: 0 }, none, "ready").kind).toBe("analyze");
    expect(nextStep({ ...ready, rules: 0, businessRules: 27 }, none, "ready").kind).not.toBe("analyze");     // accepted business knowledge exists: not "nothing was read"
  });
  it("routes questions and empty workspaces", () => {
    expect(nextStep({ ...ready, questions: 2 }, none, "ready")).toMatchObject({ kind: "questions", to: "questions" });
    expect(nextStep({ ...ready, files: 0 }, none, "ready").kind).toBe("add-files");
  });
  it("is loading until the summary and the work list have arrived", () => {
    expect(nextStep(null, none, "ready").kind).toBe("loading");
    expect(nextStep(ready, none, "loading").kind).toBe("loading");
  });
});

describe("ctaRepeatsFirstRow (SCN-16)", () => {
  const work = { kind: "work" as const, text: "", to: "changes", label: "Open the first item" };
  const a = { family: "business_change", proposal_job_id: "chg-1" };
  const list = { first: a, unavailable: 0, cut: false };
  it("same request (family + id) in the first visible row of a complete list", () => {
    expect(ctaRepeatsFirstRow(work, { ...a }, list)).toBe(true);
  });
  it("another request, or the same id in another family, is not a repeat", () => {
    expect(ctaRepeatsFirstRow(work, { family: "business_change", proposal_job_id: "chg-2" }, list)).toBe(false);
    expect(ctaRepeatsFirstRow(work, { family: "intent_flow", proposal_job_id: "chg-1" }, list)).toBe(false);
  });
  it("partial, cut or unread list, no first row, or a different next step keeps the full line", () => {
    expect(ctaRepeatsFirstRow(work, a, { ...list, unavailable: 1 })).toBe(false);
    expect(ctaRepeatsFirstRow(work, a, { ...list, cut: true })).toBe(false);
    expect(ctaRepeatsFirstRow(work, a, null)).toBe(false);
    expect(ctaRepeatsFirstRow(work, a, { ...list, first: undefined })).toBe(false);
    expect(ctaRepeatsFirstRow({ ...work, kind: "questions" }, a, list)).toBe(false);
    expect(ctaRepeatsFirstRow(work, undefined, list)).toBe(false);
  });
});
