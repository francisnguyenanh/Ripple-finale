// Engine numbers of the what-if simulation as a reader sees them: a share as a percentage and its change in percentage points, days with their unit, a missing value as
// "Unavailable" (never 0). Nothing is rounded beyond what the engine stored; only the float noise of the x100 multiplication is removed (12 significant digits).
export type Unit = "days" | "share";

export function engineNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (v && typeof v === "object" && typeof (v as Record<string, unknown>).mean === "number") return (v as Record<string, number>).mean;
  return null;
}

const hundred = (x: number) => String(Number((x * 100).toPrecision(12)));

export function formatEngine(v: unknown, unit: Unit, delta = false): string {
  const x = engineNumber(v);
  if (x === null) return "Unavailable";
  const sign = delta && x > 0 ? "+" : "";
  if (unit === "share") return delta ? `${sign}${hundred(x)} percentage points` : `${hundred(x)}%`;
  return `${sign}${String(x)} days`;
}
