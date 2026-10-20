// Where a displayed statement comes from (EUI-01 visual provenance). Origin is not validation and not authority: an AI label never means "correct", a source or engine
// label never means "accepted" or "approved". Pure adapter; an unknown origin is shown neutral, never as evidence.
export type Origin = "ai" | "source" | "engine";

export interface OriginDisplay { known: boolean; family: "ai" | "evidence" | "unknown"; label: string; note: string }

const ORIGINS: Record<Origin, OriginDisplay> = {
  ai: { known: true, family: "ai", label: "AI", note: "Written by a model: a reading of the facts, not a decision" },
  source: { known: true, family: "evidence", label: "Source", note: "Observed in the records" },
  engine: { known: true, family: "evidence", label: "Engine", note: "Computed by the deterministic engine" },
};

export function originDisplay(origin: unknown): OriginDisplay {
  return typeof origin === "string" && origin in ORIGINS ? ORIGINS[origin as Origin] : { known: false, family: "unknown", label: "Origin unknown", note: "Where this comes from is not recorded" };
}
