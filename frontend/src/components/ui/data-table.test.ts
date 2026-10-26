import { describe, expect, it } from "vitest";
import { compareSort, sortRows, type Column } from "./data-table";

type R = { id: string; n: number | null; t: string; d?: string | null };
const col = (get: (r: R) => number | string | null | undefined): Column<R> => ({ id: "c", header: "C", cell: () => null, sortValue: get });

describe("DataTable sorting (SCN-09, loaded rows only)", () => {
  it("numbers compare as numbers, not text (2 before 10)", () => {
    const rows: R[] = [{ id: "a", n: 10, t: "" }, { id: "b", n: 2, t: "" }];
    expect(sortRows(rows, col((r) => r.n), "asc").map((r) => r.id)).toEqual(["b", "a"]);
    expect(sortRows(rows, col((r) => r.n), "desc").map((r) => r.id)).toEqual(["a", "b"]);
  });
  it("missing values stay last in both directions", () => {
    const rows: R[] = [{ id: "x", n: null, t: "" }, { id: "y", n: 1, t: "" }, { id: "z", n: 3, t: "" }];
    expect(sortRows(rows, col((r) => r.n), "asc").map((r) => r.id)).toEqual(["y", "z", "x"]);
    expect(sortRows(rows, col((r) => r.n), "desc").map((r) => r.id)).toEqual(["z", "y", "x"]);
  });
  it("equal keys keep the server order (stable tie-break)", () => {
    const rows: R[] = [{ id: "1", n: 5, t: "" }, { id: "2", n: 5, t: "" }, { id: "3", n: 1, t: "" }, { id: "4", n: 5, t: "" }];
    expect(sortRows(rows, col((r) => r.n), "asc").map((r) => r.id)).toEqual(["3", "1", "2", "4"]);
    expect(sortRows(rows, col((r) => r.n), "desc").map((r) => r.id)).toEqual(["1", "2", "4", "3"]);
  });
  it("text uses natural order and ignores case; ISO dates sort by their raw value", () => {
    expect(compareSort("item 2", "Item 10")).toBeLessThan(0);
    const rows: R[] = [{ id: "late", n: 0, t: "", d: "2026-10-26T09:00:00Z" }, { id: "early", n: 0, t: "", d: "2026-10-25T23:00:00Z" }];
    expect(sortRows(rows, col((r) => r.d), "asc").map((r) => r.id)).toEqual(["early", "late"]);
  });
  it("a column without a sort value leaves the order unchanged", () => {
    const rows: R[] = [{ id: "b", n: 2, t: "" }, { id: "a", n: 1, t: "" }];
    expect(sortRows(rows, { id: "c", header: "C", cell: () => null }, "asc")).toBe(rows);
  });
});
