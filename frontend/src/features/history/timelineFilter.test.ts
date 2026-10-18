import { describe, expect, it } from "vitest";
import { actorsOf, filterEvents } from "./timelineFilter";

const ev = [
  { actor_id: "ana", event_type: "DECISION_RECORDED" },
  { actor_id: "ben", event_type: "RESTORE_APPROVED" },
  { actor_id: "", event_type: "SOURCE_PUBLISHED" },
  { actor_id: "ana", event_type: "INTENT_PARSED" },
];
const label = (e: { event_type?: unknown }) => String(e.event_type).replace(/_/g, " ").toLowerCase();

describe("timeline filter", () => {
  it("lists the distinct named actors, sorted, without the system", () => {
    expect(actorsOf(ev)).toEqual(["ana", "ben"]);
  });
  it("keeps every loaded event when no filter is set", () => {
    expect(filterEvents(ev, { actor: "", text: "" }, label)).toHaveLength(4);
  });
  it("filters by actor and by text on the label or the raw type", () => {
    expect(filterEvents(ev, { actor: "ana", text: "" }, label)).toHaveLength(2);
    expect(filterEvents(ev, { actor: "", text: "restore" }, label)).toHaveLength(1);
    expect(filterEvents(ev, { actor: "", text: "SOURCE_PUBLISHED" }, label)).toHaveLength(1);
    expect(filterEvents(ev, { actor: "ben", text: "decision" }, label)).toHaveLength(0);
  });
  it("an event without an actor is never matched by an actor filter", () => {
    expect(filterEvents(ev, { actor: "ana", text: "source" }, label)).toHaveLength(0);
  });
});
