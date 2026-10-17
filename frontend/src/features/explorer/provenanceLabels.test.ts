import { describe, expect, it } from "vitest";
import { nodeHelp, nodeHistoryLines, nodeStatus } from "./explorerText";
import type { GNode } from "./layout";

const node = (state: string, history: Record<string, unknown>): GNode => ({ id: "c1", type: "candidate", label: "x", state, detail: { history } });

describe("labels come from stored provenance, not from time or revision", () => {
  it("a value changed in a file that an apply edited", () => {
    const n = node("updated", { update_kind: "value_changed", source_edited_by_apply: "job-apply-1" });
    expect(nodeStatus(n)).toBe("Value changed by an applied change");
    expect(nodeHistoryLines(n)).toContain("The source file was edited by an applied change.");
  });
  it("a value that differs with no apply recorded for the file is NOT called an applied change", () => {
    const n = node("updated", { update_kind: "value_changed", source_edited_by_apply: null });
    expect(nodeStatus(n)).toBe("Value read differently than in the earlier reading");
    expect(nodeStatus(n)).not.toMatch(/applied/);
    expect(nodeHistoryLines(n).join(" ")).toContain("No applied change edited this file");
  });
  it("a scope-only difference keeps the value and says so", () => {
    const n = node("updated", { update_kind: "scope_only", read_again_after_apply: "job-apply-0" });
    expect(nodeStatus(n)).toBe("Read again with a different scope (value unchanged)");
    expect(nodeStatus(n)).not.toMatch(/applied/);
    expect(nodeHistoryLines(n).join(" ")).toContain("read again because of an applied change");
    expect(nodeHelp(n)).toContain("read again");
  });
  it("an unchanged file read at an earlier revision is valid, not updated or out of date", () => {
    const n = node("pending-confirmation", { file_unchanged_since_read: true });
    expect(nodeStatus(n)).toBe("Extracted - not yet confirmed");
    expect(nodeHistoryLines(n).join(" ")).toContain("still valid");
  });
  it("a state that is not 'updated' ignores history", () => {
    expect(nodeStatus(node("stale", { update_kind: "value_changed" }))).toBe("Out of date (file changed)");
  });
});
