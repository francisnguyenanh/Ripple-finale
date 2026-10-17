import { describe, expect, it } from "vitest";
import { nextAction, type Actor } from "./next";
import type { FlowData } from "./types";

const me = (account: string, ...perms: string[]): Actor => ({ account, can: (p) => perms.includes(p) });
const flow = (over: Record<string, unknown>): FlowData => ({ decisions: [], chat: null, intent: null, draft: null, ...over }) as unknown as FlowData;
const done = (extra: Record<string, unknown> = {}) => ({ job_id: "j", kind: "x", status: "APPROVED", execution_status: "COMPLETED", execution_result: {}, ...extra });
const chat = (over: Record<string, unknown>) => ({ status: "OPEN", awaiting_ai: false, awaiting_allowance: true, you_creator: true, you_are_creator: true, intent_id: null, understanding: null, ...over });

describe("who is next (hand-off wording follows the role model)", () => {
  it("a held AI reply names the reason, never 'ask an administrator to allow it'", () => {
    const n = nextAction(flow({ chat: chat({ ai_block: "suspended" }) }), me("r", "intent.create", "ai.use"));
    expect(n.kind).toBe("stop");
    expect(n.text).toMatch(/switched off for your account/);
    expect(n.who).toBe("You");
  });
  it("without a block the person is told to ask the AI themselves", () => {
    const n = nextAction(flow({ chat: chat({ ai_block: null }) }), me("r", "intent.create", "ai.use"));
    expect(n.text).toMatch(/Ask the AI/);
    expect(n.text).not.toMatch(/administrator/i);
  });
  it("another member only waits for the requester on someone else's conversation", () => {
    const n = nextAction(flow({ chat: chat({ you_are_creator: false }) }), me("v", "project.read"));
    expect(n.kind).toBe("wait");
    expect(n.who).toBe("The requester");
  });
  it("the owner confirms their own saved request; a colleague waits for the owner (no copy-paste link)", () => {
    const d = flow({ intent: { intent_id: "i", status: "PARSED", created_by: "alice" } });
    const owner = nextAction(d, me("alice", "intent.create", "intent.confirm_own"));
    expect(owner.kind).toBe("act");
    expect(owner.text).toMatch(/Confirm intent/);
    const other = nextAction(d, me("bob", "intent.create", "intent.confirm_own"));
    expect(other.kind).toBe("wait");
    expect(other.who).toBe("alice");
    expect(other.text).toMatch(/My work/);
  });
  it("the draft runs under the requester's own permission: no second person is announced", () => {
    const d = flow({ intent: { intent_id: "i", status: "CONFIRMED", created_by: "alice" } });
    const n = nextAction(d, me("alice", "change.prepare", "source.import", "ai.use"));
    expect(n.text).toMatch(/nobody has to allow it/);
  });
  it("the check after the change is the approver's and the requester is pointed to the inbox", () => {
    const d = flow({
      intent: { intent_id: "i", status: "CONFIRMED" }, draft: done(), impact: done(), simulation: done(), proposal: done({ requested_by: "alice" }), contracts: [],
      gaps: { stale: false },
    });
    expect(nextAction(d, me("alice", "source.import")).kind).toBe("wait");
    expect(nextAction(d, me("alice", "source.import")).text).toMatch(/My work/);
    expect(nextAction(d, me("carol", "verification.contract.set")).kind).toBe("act");
  });
  it("apply is offered only to someone holding the apply permission", () => {
    const d = flow({
      intent: { intent_id: "i", status: "CONFIRMED" }, draft: done(), impact: done(), simulation: done(), proposal: done({ requested_by: "alice" }), contracts: [{}],
      review: done(), approval: done(), decisions: [{ action: "approve", actor_id: "carol" }], gaps: { stale: false },
    });
    expect(nextAction(d, me("alice", "change.apply")).kind).toBe("act");
    expect(nextAction(d, me("carol", "change.approve")).kind).toBe("wait");
  });
});

describe("an administrator is not asked to do approver steps", () => {
  const proposalDone = { job_id: "p", kind: "proposal", status: "APPROVED", execution_status: "COMPLETED", execution_result: {}, requested_by: "req" };
  const withProposal = (over: Record<string, unknown> = {}) => flow({ intent: { intent_id: "i", status: "CONFIRMED", created_by: "req" }, draft: done(), impact: done(), simulation: done(), proposal: proposalDone, contracts: [], ...over });
  it("an administrator-only account is told an approver must state the check; an approver is asked", () => {
    const admin = { account: "adm", can: (p: string) => ["verification.contract.set", "change.approve"].includes(p), roles: ["admin"] };
    const approver = { account: "apr", can: admin.can, roles: ["change_approver"] };
    expect(nextAction(withProposal(), admin).who).toMatch(/approver \(not the requester\)/);
    expect(nextAction(withProposal(), approver).who).toMatch(/You \(approver\)/);
  });
});
