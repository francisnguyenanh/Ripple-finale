import { describe, expect, it } from "vitest";
import { activeKey, JOURNEY, journey, stageTitle } from "./journey";
import type { Actor } from "./next";
import type { FlowData } from "./types";

const me = (account: string, ...perms: string[]): Actor => ({ account, can: (p) => perms.includes(p) });
const REQ = me("req", "source.import", "intent.create", "intent.confirm_own", "ai.use", "change.apply");
const APR = me("apr", "change.approve", "verification.contract.set", "change.accept_result", "ai.use");
const job = (kind: string, over: Record<string, unknown> = {}) => ({ job_id: kind, kind, status: "APPROVED", execution_status: "COMPLETED", execution_result: {}, requested_by: "req", ...over });
const base = (over: Record<string, unknown> = {}): FlowData => ({
  head: { source_revision_id: "r1", decision_epoch: 0 }, chat: null, decisions: [], contracts: [], notes: null, gaps: null, change: null, loop: null,
  intent: { intent_id: "i", status: "CONFIRMED", created_by: "req", structured_form: { subject: "limit", old_value: 500000, new_value: 300000, currency: "JPY" } },
  draft: job("change.draft", { execution_result: { status: "PARTIAL", not_covered: [1, 2] } }), impact: job("impact"),
  simulation: job("simulation", { execution_result: { harbor: { changed_count: 9, transactions: 240 } } }),
  proposal: job("proposal"), review: null, approval: null, apply: null, query: null, whatif: null, ...over,
}) as unknown as FlowData;
const by = (d: FlowData, who: Actor, key: string) => journey(d, who).find((s) => s.key === key)!;

describe("numbering", () => {
  it("is the position in one registry: consecutive, no skipped number, every title numbered", () => {
    const nums = JOURNEY.map((s) => Number(stageTitle(s.key).split(".")[0]));
    expect(nums).toEqual(nums.map((_, i) => i + 1));
    expect(stageTitle("apply")).toBe("8. Apply");
    expect(stageTitle("refresh")).toMatch(/^9\. Verification/);
  });
});

describe("who is holding the change (live B13: nobody could tell why Approve was grey)", () => {
  it("proposal ready, no check yet: the approver acts, the requester waits for an approver, the later steps are not started", () => {
    const d = base();
    expect(activeKey(d)).toBe("check");
    expect(by(d, APR, "check")).toMatchObject({ state: "current", mine: true });
    const r = by(d, REQ, "check");
    expect(r.state).toBe("waiting"); expect(r.who).toMatch(/approver/i); expect(r.mine).toBe(false);
    expect(by(d, REQ, "review").state).toBe("todo");
    expect(by(d, REQ, "approval").state).toBe("todo");
    expect(by(d, REQ, "draft").state).toBe("partial");                                            // done, but partly processed: said so
    expect(by(d, REQ, "draft").text).toMatch(/partly processed/);
  });
  it("a check stated for an earlier revision is to REDO, not to do", () => {
    const d = base({ contracts: [{ contract_id: "c", author_account_id: "apr", stale: true }], contractsStale: "the files changed after this proposal was prepared" });
    expect(activeKey(d)).toBe("check");
    expect(by(d, APR, "check")).toMatchObject({ state: "redo" });
    expect(by(d, APR, "check").text).toMatch(/no longer applies/);
  });
  it("a review that ran before the check existed is to redo, and the requester is the one to do it", () => {
    const d = base({ contracts: [{ contract_id: "c", author_account_id: "apr" }], review: job("review", { payload: {} }) });
    expect(activeKey(d)).toBe("review");
    expect(by(d, REQ, "review")).toMatchObject({ state: "redo", mine: true });             // the requester runs it again
    expect(by(d, APR, "review")).toMatchObject({ state: "redo", mine: false });
    expect(by(d, REQ, "review").text).toMatch(/Run the review again/);
  });
  it("an open reviewer request blocks approval and names who must resolve it; the reviewer's optional notes are shown as parallel work otherwise", () => {
    const ready = { contracts: [{ contract_id: "c", author_account_id: "apr" }], review: job("review", { payload: { verification_contract_id: "c" } }),
      approval: job("approval", { execution_result: { snapshot: { source_revision_id: "r1", decision_epoch: 0 } } }) };
    const blocked = base({ ...ready, notes: { open_change_requests: ["rev1"], notes: [] } });
    expect(by(blocked, APR, "approval")).toMatchObject({ state: "blocked", mine: false });
    expect(by(blocked, APR, "approval").text).toMatch(/rev1.*asked for changes/);
    expect(by(blocked, APR, "approval").who).toMatch(/reviewer/i);
    const free = base(ready);
    expect(by(free, APR, "approval").state).toBe("current");
    expect(by(free, APR, "approval").also).toMatch(/reviewer may read/);
  });
  it("approved: the apply step waits for someone with apply permission, with their role not a made-up name", () => {
    const d = base({ contracts: [{ contract_id: "c", author_account_id: "apr" }], review: job("review", { payload: { verification_contract_id: "c" } }),
      approval: job("approval", { execution_result: { snapshot: { source_revision_id: "r1", decision_epoch: 0 } } }), decisions: [{ action: "approve", actor_id: "apr", reason: "ok" }] });
    expect(activeKey(d)).toBe("apply");
    expect(by(d, REQ, "apply")).toMatchObject({ state: "current", mine: true });
    expect(by(d, APR, "apply").state).toBe("waiting");
    expect(by(d, APR, "apply").who).toMatch(/requester/i);
    expect(by(d, REQ, "approval").text).toMatch(/Approved by apr/);
  });
  it("a bundle prepared before the files moved is to redo, never approvable", () => {
    const d = base({ contracts: [{ contract_id: "c", author_account_id: "apr" }], review: job("review", { payload: { verification_contract_id: "c" } }),
      approval: job("approval", { execution_result: { snapshot: { source_revision_id: "old", decision_epoch: 0 } } }), decisions: [{ action: "approve", actor_id: "apr" }] });
    expect(by(d, REQ, "approval")).toMatchObject({ state: "redo" });
  });
});

describe("after apply: not complete while something still runs", () => {
  const applied = (change: Record<string, unknown> | null) => base({ contracts: [{ contract_id: "c", author_account_id: "apr" }], review: job("review", { payload: { verification_contract_id: "c" } }),
    approval: job("approval", { execution_result: { snapshot: { source_revision_id: "r1", decision_epoch: 0 } } }), decisions: [{ action: "approve", actor_id: "apr" }],
    apply: job("apply"), change, loop: null });
  it("applied but verification is still running: the system holds it, nothing is reported as finished", () => {
    const d = applied({ applyJob: job("apply"), dto: { state: "REANALYSING", completion: {}, reanalysis: { status: "RUNNING" } }, reJobs: [], revision: null });
    d.loop = { status: "RUNNING", stages: { reanalysis: { completed: 0, total: 1 }, verification: "PENDING" } };
    expect(activeKey(d)).toBe("refresh");
    expect(by(d, REQ, "refresh")).toMatchObject({ state: "waiting", who: "The system" });
    expect(by(d, REQ, "accept").state).toBe("todo");
    expect(by(d, REQ, "query").state).toBe("todo");
  });
  it("verified but nobody decided the result: a person with result permission acts; the change is not 'complete'", () => {
    const d = applied({ applyJob: job("apply"), dto: { state: "AWAITING_ACCEPTANCE", completion: { status: "AWAITING" }, reanalysis: { status: "COMPLETED" }, alignment: { global: "PARTIAL" } }, reJobs: [], revision: null });
    expect(activeKey(d)).toBe("accept");
    expect(by(d, REQ, "refresh").state).toBe("partial");
    expect(by(d, REQ, "accept").who).toMatch(/accept results/);             // the requester has no result permission: the role is named
    expect(by(d, APR, "accept")).toMatchObject({ state: "current", mine: true });
  });
});
