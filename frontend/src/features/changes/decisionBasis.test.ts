import { describe, expect, it } from "vitest";
import { decisionBasis } from "./DecisionBasis";
import type { FlowData } from "./types";

const done = (extra: Record<string, unknown> = {}) => ({ job_id: "j", kind: "x", status: "APPROVED", execution_status: "COMPLETED", execution_result: {}, ...extra });
const flow = (over: Record<string, unknown>): FlowData => ({
  head: { source_revision_id: "r1", decision_epoch: 0 }, contracts: [], decisions: [],
  proposal: done({ execution_result: { edits: [{ path: "a.b", before: 1, after: 2 }] } }),
  approval: done({ execution_result: { snapshot: { source_revision_id: "r1", decision_epoch: 0 }, blocked_reasons: [] } }),
  review: done({ payload: {}, execution_result: { checks: { diff_scope: { status: "PASS" } } } }), diff: { lines: [] },
  ...over,
}) as unknown as FlowData;

describe("decisionBasis: why Approve is not offered", () => {
  it("names the missing check and who does what next (live B13: the review ran before the check existed)", () => {
    const b = decisionBasis(flow({}), null, false);
    expect(b.missing.join(" ")).toMatch(/check after the change has not been stated/);
    expect(b.missing.join(" ")).toMatch(/requester runs the review again/);
  });
  it("says the review must be rerun when the check was stated AFTER the review", () => {
    const b = decisionBasis(flow({ contracts: [{ contract_id: "vc-1", logical_path: "x/approval.yaml", expected: "300000", author_account_id: "ap" }] }), null, false);
    expect(b.missing.join(" ")).toMatch(/ran BEFORE the check was stated/);
  });
  it("is complete when the review names the stated check and nothing else is missing", () => {
    const b = decisionBasis(flow({ contracts: [{ contract_id: "vc-1", logical_path: "x/approval.yaml", expected: "300000", author_account_id: "ap" }], review: done({ payload: { verification_contract_id: "vc-1" }, execution_result: { checks: { diff_scope: { status: "PASS" } } } }) }), null, false);
    expect(b.missing).toEqual([]);
  });
});
