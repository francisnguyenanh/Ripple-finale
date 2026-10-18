import { describe, expect, it } from "vitest";
import { SECTIONS, selectedSection, visible, type SectionId } from "./sections";

const all = SECTIONS.map((s) => s.id);

describe("Questions sections", () => {
  it("ids are unique and every section belongs to a group", () => {
    expect(new Set(all).size).toBe(all.length);
    expect(SECTIONS.every((s) => s.group === "business" || s.group === "source")).toBe(true);
  });
  it("opens on Open questions by default; 'all' or an unknown value shows every section", () => {
    expect(selectedSection("", all)).toBe("open");
    expect(selectedSection("all", all)).toBeNull();
    expect(selectedSection("nope", all)).toBeNull();
    for (const id of all) expect(visible(null, id)).toBe(true);
  });
  it("a section named in the URL wins; a link into another section opens that section", () => {
    expect(selectedSection("rules", all, { chg: "chg-1" })).toBe("rules");
    expect(selectedSection("", all, { chg: "chg-1" })).toBe("change");
    expect(selectedSection("", all, { q: "q-1" })).toBe("ask");
    expect(selectedSection("", all, { finding: "f-1" })).toBe("findings");
    expect(selectedSection("", all, { qa: "qa-1" })).toBe("source-ask");
    expect(selectedSection("", all.filter((i) => i !== "source-ask"), { qa: "qa-1" })).toBe("open");   // a section the account cannot use is never opened
  });
  it("shows only the selected section", () => {
    const sel = selectedSection("open", all);
    expect(sel).toBe("open");
    expect(visible(sel, "open")).toBe(true);
    expect(visible(sel, "ask")).toBe(false);
  });
  it("a section the account cannot use is not selectable", () => {
    const without: SectionId[] = all.filter((i) => i !== "source-ask");
    expect(selectedSection("source-ask", without)).toBeNull();
  });
});
