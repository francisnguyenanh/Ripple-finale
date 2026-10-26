import { describe, expect, it } from "vitest";
import { pickJump } from "./decisionJump";

const decide = { id: "rev-reason-c", disabled: false, label: "Go to the decision controls" };
const commit = { id: "rev-commit-c", disabled: true, reasonId: "why-commit", label: "Go to the commit control" };

describe("pickJump (SCN-17)", () => {
  it("takes the first asked-now obligation that has a control on the page", () => {
    expect(pickJump(["decide_knowledge"], { decide_knowledge: decide })).toBe(decide);
    expect(pickJump(["clarify", "commit_knowledge"], { commit_knowledge: commit })).toBe(commit);
  });
  it("no asked obligation, or none with a rendered control: no link (eligible-but-not-asked never gets one)", () => {
    expect(pickJump([], { decide_knowledge: decide })).toBeNull();
    expect(pickJump(["respond_to_changes"], { decide_knowledge: decide })).toBeNull();
    expect(pickJump(["decide_knowledge"], {})).toBeNull();
  });
});
