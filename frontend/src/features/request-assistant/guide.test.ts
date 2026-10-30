import { describe, expect, it } from "vitest";
import { actionStillCurrent, actionTarget, cancelNote, citationTarget, pendingNote, suggestQuestions, unavailableText } from "./guide";

const ob = (o: Record<string, unknown>) => ({ code: "decide_knowledge", label: "Decide the knowledge change", state: "current", obligation_me: false, eligible_me: false, ...o });

describe("suggestQuestions", () => {
  it("grounds the first suggestion in the reader's own current step and never claims a step that is not theirs", () => {
    const asked = suggestQuestions({ family: "business_change", obligations: [ob({ obligation_me: true })], blockers: [], waiting_on: [] });
    expect(asked[0]).toEqual({ id: "my_step", question: "What do I need to check before I decide the knowledge change?" });
    const judge = suggestQuestions({ obligations: [ob({ eligible_me: true })] });
    expect(judge[0].id).toBe("eligible");
    expect(judge[0].question).toMatch(/or is someone else expected to/);
    const viewer = suggestQuestions({ obligations: [ob({})] });
    expect(viewer.map((s) => s.id)).not.toContain("my_step");
  });
  it("adds blocker and waiting questions only when the view has them, caps at four and drops questions already asked", () => {
    const tv = { request_ref: { family: "business_change" }, obligations: [ob({ obligation_me: true })], blockers: [{ code: "b" }], waiting_on: [{ holder: "x" }] };
    const all = suggestQuestions(tv);
    expect(all.map((s) => s.id)).toEqual(["my_step", "blocker", "waiting", "purpose"]);
    expect(suggestQuestions(tv, ["what is blocking this request, and what would unblock it?"]).map((s) => s.id)).toEqual(["my_step", "waiting", "purpose", "decisions"]);
    expect(suggestQuestions(null)).toEqual([]);
  });
});

describe("action targets", () => {
  it("maps only known server action codes to existing controls and rechecks the current view", () => {
    expect(actionTarget("business_change", "approve_knowledge")).toBe("rev-reason");
    expect(actionTarget("business_change", "made_up")).toBeNull();
    expect(actionTarget("intent_flow", "approve_knowledge")).toBeNull();
    const tv = { permitted_actions: [{ action: "approve_knowledge", allowed: true, current: true }, { action: "commit_knowledge", allowed: true, current: false }] };
    expect(actionStillCurrent(tv, "approve_knowledge")).toBe(true);
    expect(actionStillCurrent(tv, "commit_knowledge")).toBe(false);
    expect(actionStillCurrent(null, "approve_knowledge")).toBe(false);
  });
});

describe("citation targets", () => {
  it("points only to sections the page already shows, from the kind and the typed key", () => {
    expect(citationTarget({ kind: "impact", key: "impact:rk-0123456789abcdef" })).toBe('[data-ref-key="rk-0123456789abcdef"]');
    expect(citationTarget({ kind: "impact", key: "impact:direct" })).toBeNull();                     // not a typed key: no guess
    expect(citationTarget({ kind: "impact", key: 'impact:rk-0123456789abcdef"] , *' })).toBeNull();  // no selector injection
    expect(citationTarget({ kind: "decision", key: "decision:x" })).toBe('[data-testid="rev-decisions"]');
    expect(citationTarget({ kind: "obligation" })).toBe('[data-testid="task-frame"]');
    expect(citationTarget({ kind: "request", key: "request:form:x" })).toBeNull();
    expect(citationTarget(null)).toBeNull();
  });
});

describe("waiting, stopping and no-answer wording (R11)", () => {
  it("says nothing for a queued question under 20 s, says it is slow after 20 s, and says a running one can no longer be stopped", () => {
    expect(pendingNote("queued", 5_000)).toBeNull();
    expect(pendingNote("queued", 20_000)).toMatch(/^This is taking longer than usual\./);
    expect(pendingNote("running", 1_000)).toMatch(/can no longer be stopped; if an answer comes, it is shown here and its cost is counted\.$/);
    expect(pendingNote("running", 25_000)).toMatch(/longer than usual.*can no longer be stopped/);
  });
  it("reports what Stop did without claiming the provider call or the charge was stopped", () => {
    expect(cancelNote("cancelled")).toBe("Stopped: the question had not been sent to the model.");
    expect(cancelNote("running")).toMatch(/can no longer be stopped/);
    for (const o of ["cancelled", "running", "finished"]) expect(cancelNote(o)).not.toMatch(/refund|not charged|no charge|cancelled the model/i);
    expect(cancelNote(undefined)).toBeNull();
  });
  it("explains a timeout in plain words with the reference and never shows the raw reason", () => {
    const t = unavailableText({ reason_code: "provider_timeout", reference: "rqm-1", reason: "provider_execution_failed: ProviderTimeoutError: provider timed out at the 45 s deadline" });
    expect(t).toBe("The assistant did not answer in time. Nothing was concluded; you can ask again, which is a new paid question. Reference: rqm-1.");
    expect(unavailableText({ reason_code: "something_new" })).toMatch(/^No answer was produced\./);
  });
});
