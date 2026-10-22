import { describe, expect, it } from "vitest";
import { SOURCE_ROLES, suggestRole } from "./sourceRole";

describe("suggestRole", () => {
  it("suggests from the file type and keeps a workbook or an unlisted type for the person", () => {
    expect(suggestRole("reference_system/docs/procurement-policy.md")).toBe("normative");
    expect(suggestRole("fixtures/harbor_public/app/routing.py")).toBe("implementation");
    expect(suggestRole("fixtures/harbor_public/config/approval.yaml")).toBe("implementation");
    expect(suggestRole("fixtures/harbor_public/data/requisitions.jsonl")).toBe("observational");
    expect(suggestRole("harbor_demo/data/approval_matrix.xlsx")).toBe("unknown");
    expect(suggestRole("README")).toBe("unknown");
  });

  it("recognises tests by folder or name before the extension", () => {
    expect(suggestRole("tests/test_routing.py")).toBe("test");
    expect(suggestRole("app/routing_test.py")).toBe("test");
    expect(suggestRole("web/Form.spec.ts")).toBe("test");
    expect(suggestRole("app\\Tests\\cases.py")).toBe("test");
  });

  it("only offers roles the server accepts", () => {
    expect(SOURCE_ROLES.map((r) => r.value).sort()).toEqual(["implementation", "normative", "observational", "test", "unknown"]);
  });
});
