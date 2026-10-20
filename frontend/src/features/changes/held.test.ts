import { describe, expect, it } from "vitest";
import { alignNext, nextAction, type Actor } from "./next";
import { journey } from "./journey";
import { responsibility } from "@/features/request-assistant/taskView";
import type { FlowData } from "./types";
import { scopeGap } from "./fmtForm";

// The server projection decides who holds the step; the page's journey and next-step text follow it (permission is not assignment).
const me = (account: string, ...perms: string[]): Actor => ({ account, can: (p) => perms.includes(p) });
const confirmedNoDraft = { decisions: [], chat: null, draft: null, intent: { intent_id: "i", status: "CONFIRMED", created_by: "alice" } } as unknown as FlowData;
const tv = (o: Record<string, unknown>, waiting: Record<string, unknown>[] = []) => ({ obligations: [{ code: "ask_draft", label: "Ask for the AI draft of the change", required: true, state: "current", holder_label: "the requester", ...o }], waiting_on: waiting });

describe("one answer to 'who acts next'", () => {
  it("a person who may prepare the change but is not the one asked is never told 'You'", () => {
    const held = responsibility(tv({ obligation_me: false, eligible_me: true, reason: "the requester is asked; your role may also do it" }, [{ holder: "requester", holder_label: "the requester" }]));
    const raw = nextAction(confirmedNoDraft, me("judge-1", "change.prepare", "ai.use"));
    expect(raw.who).toBe("You");                                                    // the client permission alone would say "You"
    const n = alignNext(raw, held);
    expect(n.who).toBe("The requester");
    expect(n.kind).toBe("wait");
    expect(n.text).toContain("Ask for the AI draft of the change");
    expect(n.text).toContain("You may also do this, but you are not asked to");
    const step = journey(confirmedNoDraft, me("judge-1", "change.prepare", "ai.use"), held).find((s) => s.key === "draft")!;
    expect(step.mine).toBe(false);
    expect(step.who).toBe("The requester");
    expect(step.state).toBe("waiting");
  });
  it("the person who is asked keeps the instruction", () => {
    const held = responsibility(tv({ obligation_me: true, eligible_me: true, assigned_me: true }));
    const n = alignNext(nextAction(confirmedNoDraft, me("alice", "change.prepare", "ai.use")), held);
    expect(n.who).toBe("You");
    expect(n.kind).toBe("act");
    expect(journey(confirmedNoDraft, me("alice", "change.prepare", "ai.use"), held).find((s) => s.key === "draft")!.mine).toBe(true);
  });
  it("someone without the permission is not offered the step either", () => {
    const held = responsibility(tv({ obligation_me: false, eligible_me: false, reason: "your role cannot do this step" }, [{ holder: "requester", holder_label: "the requester" }]));
    const n = alignNext(nextAction(confirmedNoDraft, me("viewer-1", "project.read")), held);
    expect(n.kind).toBe("wait");
    expect(n.text).not.toContain("You may also");
  });
  it("without a readable projection the page keeps its own wording (nothing is guessed)", () => {
    const raw = nextAction(confirmedNoDraft, me("alice", "change.prepare", "ai.use"));
    expect(alignNext(raw, null)).toEqual(raw);
    expect(responsibility(null)).toBeNull();
  });
});

describe("scope gap next to decisions", () => {
  const it_ = (scope: unknown, applies: unknown) => ({ intent_id: "i", status: "PARSED", structured_form: { scope, applies_to: applies } }) as never;
  it("names the expected cases the stated scope does not cover", () => {
    expect(scopeGap(it_({ order_type: "standard" }, ["standard", "emergency"]))).toEqual(["emergency"]);
    expect(scopeGap(it_({ order_type: "Standard" }, ["standard"]))).toEqual([]);
  });
  it("guesses nothing when the scope or the expectation is missing", () => {
    expect(scopeGap(it_({}, ["emergency"]))).toEqual([]);
    expect(scopeGap(it_({ order_type: "standard" }, []))).toEqual([]);
  });
});
