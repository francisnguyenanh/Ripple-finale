import { describe, expect, it } from "vitest";
import { filterTopics } from "./topicFilter";

const topics = [{ title: "Approval limits" }, { title: "Payment terms" }, { title: "Whole knowledge" }];

describe("filterTopics", () => {
  it("lists every loaded topic for empty or blank text", () => {
    expect(filterTopics(topics, "")).toHaveLength(3);
    expect(filterTopics(topics, "   ")).toHaveLength(3);
  });
  it("matches part of a title, ignoring case", () => {
    expect(filterTopics(topics, "LIMIT").map((t) => t.title)).toEqual(["Approval limits"]);
  });
  it("returns an empty list, not an error, when nothing matches", () => {
    expect(filterTopics(topics, "zzz")).toEqual([]);
  });
  it("does not change the order or the input", () => {
    const out = filterTopics(topics, "o");
    expect(out.map((t) => t.title)).toEqual(["Approval limits", "Whole knowledge"]);
    expect(topics).toHaveLength(3);
  });
});
