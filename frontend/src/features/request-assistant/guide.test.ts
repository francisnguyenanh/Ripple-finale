import { describe, expect, it } from "vitest";
import { actionStillCurrent, actionTarget, citationTarget, suggestQuestions } from "./guide";

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
