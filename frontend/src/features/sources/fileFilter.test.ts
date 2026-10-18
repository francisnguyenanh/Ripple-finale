import { describe, expect, it } from "vitest";
import { filterFiles } from "./fileFilter";

const files = [
  { logical_path: "harbor/config/approval.yaml", media_type: "application/yaml" },
  { logical_path: "harbor/docs/finance_guide.md", media_type: "text/markdown" },
  { logical_path: "harbor/code/policy.py", media_type: "text/x-python" },
  { logical_path: undefined, media_type: undefined },
];

describe("filterFiles", () => {
  it("keeps every file for empty text, including one with no path", () => {
    expect(filterFiles(files, "")).toHaveLength(4);
    expect(filterFiles(files, "  ")).toHaveLength(4);
  });
  it("matches the path or the type, ignoring case", () => {
    expect(filterFiles(files, "APPROVAL")).toHaveLength(1);
    expect(filterFiles(files, "markdown")).toHaveLength(1);
    expect(filterFiles(files, "harbor/")).toHaveLength(3);
  });
  it("returns an empty list when nothing matches and never throws on missing fields", () => {
    expect(filterFiles(files, "zzz")).toEqual([]);
  });
});
