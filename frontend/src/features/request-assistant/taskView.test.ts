import { describe, expect, it } from "vitest";
import { taskSummary } from "./taskView";

const ob = (o: Record<string, unknown>) => ({ required: true, state: "current", obligation_me: false, eligible_me: false, holder_label: "an approver", label: "Decide", ...o });

describe("taskSummary (SCN-02 header facts from the projection only)", () => {
  it("asked: 'You' and the step label; several asked steps are counted, never one picked", () => {
    expect(taskSummary({ obligations: [ob({ obligation_me: true, eligible_me: true })], waiting_on: [] })).toMatchObject({ held: "You", part: "asked", partText: "Your step now" });
    const two = taskSummary({ obligations: [ob({ obligation_me: true, eligible_me: true }), ob({ obligation_me: true, eligible_me: true, label: "Commit" })], waiting_on: [] });
    expect(two).toMatchObject({ part: "asked", askedCount: 2, partText: "2 steps for you now" });
  });
  it("eligible-not-asked never becomes 'You'", () => {
    const s = taskSummary({ obligations: [ob({ eligible_me: true })], waiting_on: [{ holder_label: "the requester" }] });
    expect(s).toMatchObject({ held: "the requester", heldByYou: false, part: "eligible" });
  });
  it("a reader with no right sees the holder and 'Nothing for you now'", () => {
    expect(taskSummary({ obligations: [ob({})], waiting_on: [{ holder_label: "an approver" }, { holder_label: "an approver" }] })).toMatchObject({ held: "an approver", part: "none" });
  });
  it("parallel: asked and also waiting on someone else", () => {
    expect(taskSummary({ obligations: [ob({ obligation_me: true, eligible_me: true })], waiting_on: [{ holder_label: "a knowledge owner" }] }).held).toBe("You, and a knowledge owner");
  });
  it("optional contribution is labelled optional", () => {
    expect(taskSummary({ obligations: [ob({ obligation_me: true, eligible_me: true, required: false, label: "Review" })], waiting_on: [] })).toMatchObject({ part: "optional", partText: "An optional step for you", heldByYou: false });
  });
  it("blocked with nobody waited on, no open step, and an unknown holder stay distinct", () => {
    expect(taskSummary({ obligations: [ob({ state: "blocked" })], waiting_on: [], blockers: [{ text: "moved" }] }).held).toBe("Nobody can move it on its current path");
    expect(taskSummary({ obligations: [ob({ state: "done" })], waiting_on: [] }).held).toBe("No step is open");
    expect(taskSummary({ obligations: [ob({})], waiting_on: [] }).held).toBeNull();
  });
});
