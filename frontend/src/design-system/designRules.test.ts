import { describe, expect, it } from "vitest";
// The tool is plain ESM JavaScript used directly by `npm run check:design`; there is one copy of every rule.
// @ts-expect-error no type declarations for the .mjs checker
import { classify, checkDocs, scanSource, tokenParity, validatePolicy } from "../../tools/check-design-system.mjs";

const P = "src/features/x/X.tsx";
const none = { baseline: [], exceptions: [] };

describe("D01 scanSource", () => {
  it("accepts semantic token classes", () => {
    expect(scanSource(P, `<p className="text-muted bg-surface border-border text-danger">x</p>`)).toEqual([]);
  });
  it("rejects utility palette colours, arbitrary colours and raw colour literals in styling code", () => {
    expect(scanSource(P, `<p className="text-green-600">x</p>`)[0].rule).toBe("E01");
    expect(scanSource(P, `<p className="bg-[#fff]">x</p>`)[0].rule).toBe("E01");
    expect(scanSource(P, `<p className={cn("a", "#ff0000")}>x</p>`)[0].rule).toBe("E01");
  });
  it("does not treat a quoted hex in ordinary text/data as styling", () => {
    expect(scanSource(P, `const label = "Issue #abc123 was closed";`)).toEqual([]);
    expect(scanSource(P, `expect(text).toBe("#fff")`)).toEqual([]);
  });
  it("rejects a class that names an undefined token", () => {
    const f = scanSource(P, `<div className="bg-background">`);
    expect(f.map((x: { rule: string }) => x.rule)).toEqual(["E02"]);
  });
  it("does not scan the foundation file where raw values are defined", () => {
    expect(scanSource("src/index.css", "--x: #fff;")).toEqual([]);
  });
});

describe("D05 shared Select", () => {
  it("flags a raw select outside the shared component and allows the shared component itself", () => {
    expect(scanSource(P, `<select className="a"></select>`)[0].rule).toBe("E03");
    expect(scanSource("src/components/bits.tsx", `return <select ref={ref} />;`)).toEqual([]);
    expect(scanSource(P, `<Select value={v}></Select>`)).toEqual([]);
  });
});

describe("D04 catalog isolation", () => {
  it("rejects application code that imports the development catalog", () => {
    expect(scanSource("src/App.tsx", `import { Catalog } from "./design-system/Catalog";`)[0].rule).toBe("E09");
    expect(scanSource("src/App.tsx", `const C = React.lazy(() => import("./design-system/catalog-main"));`)[0].rule).toBe("E09");
  });
  it("allows the catalog files themselves", () => {
    expect(scanSource("src/design-system/catalog-main.tsx", `import { Catalog } from "./Catalog";`)).toEqual([]);
  });
});

describe("D01/D02 token parity", () => {
  const css = (dark: string) => `:root { --bg: #fff; --text: #000; --shadow-1: 0 1px 2px rgba(0,0,0,.1); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #000; --text: #fff; } }
:root[data-theme="dark"] { ${dark} }`;
  it("passes when light and both dark blocks define the same colour tokens", () => {
    expect(tokenParity(css("--bg: #000; --text: #fff;")).problems).toEqual([]);
  });
  it("reports a token missing from the dark theme", () => {
    expect(tokenParity(css("--bg: #000;")).problems).toEqual(["--text missing in data-theme=dark block"]);
  });
  it("fails visibly when the expected block is absent", () => {
    expect(() => tokenParity(":root { --a: #fff; }")).toThrow(/block not found/);
  });
});

describe("D02 baseline and exceptions", () => {
  const f = (expr: string) => ({ rule: "E02", path: P, line: 1, expr, why: "", fingerprint: `E02|${P}|${expr}` });
  it("grandfathers exactly the recorded occurrences; a copy is new", () => {
    const policy = { baseline: [{ rule: "E02", path: P, fingerprint: `E02|${P}|bg-background`, count: 1 }], exceptions: [] };
    const { fresh, legacy } = classify([f("bg-background"), f("bg-background")], policy);
    expect(legacy).toHaveLength(1);
    expect(fresh).toHaveLength(1);
  });
  it("treats an unrelated violation as new", () => {
    expect(classify([f("bg-card")], none).fresh).toHaveLength(1);
  });
  it("rejects wildcard, incomplete and expired exceptions", () => {
    const ex = { rule: "E01", path: "src/*", fingerprint: "E01|src/*|x", reason: "r", owner: "o", reviewBy: "2020-01-01", verificationRef: "v", replacementPlan: "p" };
    const errs = validatePolicy({ exceptions: [ex, { rule: "E01" }] }, "2026-10-17");
    expect(errs.join("\n")).toMatch(/wildcard/);
    expect(errs.join("\n")).toMatch(/expired/);
    expect(errs.join("\n")).toMatch(/missing/);
  });
  it("accepts a complete, current, exact exception", () => {
    const ex = { rule: "E01", path: P, fingerprint: `E01|${P}|text-red-500`, reason: "r", owner: "o", reviewBy: "2099-01-01", verificationRef: "v", replacementPlan: "p" };
    expect(validatePolicy({ exceptions: [ex] }, "2026-10-17")).toEqual([]);
    expect(classify([{ rule: "E01", path: P, line: 1, expr: "text-red-500", why: "", fingerprint: ex.fingerprint }], { exceptions: [ex] }).fresh).toEqual([]);
  });
});

describe("D03 docs", () => {
  const docs: Record<string, string> = Object.fromEntries(["UI_DEVELOPMENT_RULES", "DESIGN_TOKENS", "COMPONENT_GUIDELINES", "LAYOUT_PATTERNS", "CONTENT_AND_STATES", "ACCESSIBILITY"].map((d) => [d, `# ${d} --bg`]));
  it("passes with six complete docs", () => {
    expect(checkDocs((d: string) => docs[d] ?? null, () => true, ["bg"])).toEqual([]);
  });
  it("reports a missing doc, a dangling link, an internal-folder reference and an undocumented token", () => {
    const bad: Record<string, string> = { ...docs, ACCESSIBILITY: undefined as unknown as string, LAYOUT_PATTERNS: "see [x](gone.md)", CONTENT_AND_STATES: "see " + ["develop", "ing/plans"].join("") };
    const out = checkDocs((d: string) => bad[d] ?? null, (l: string) => l !== "gone.md", ["bg", "text"]).join("\n");
    expect(out).toMatch(/ACCESSIBILITY.md is missing/);
    expect(out).toMatch(/broken link gone.md/);
    expect(out).toMatch(/refers to the internal development folder/);
    expect(out).toMatch(/does not document --text/);
  });
});
