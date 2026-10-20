import { humanize, scopeText, valueText } from "@/lib/fmt";
import type { Intent } from "./types";

export function fmtForm(it: Intent) {
  const sf = (it.structured_form ?? {}) as Record<string, unknown>;
  return {
    title: humanize(sf.subject || it.target || "Change request"),
    from: valueText(sf.old_value, sf.unit, sf.currency),
    to: valueText(sf.new_value, sf.unit, sf.currency),
    scope: scopeText(sf.scope),
    applies: ((sf.applies_to as string[] | undefined) ?? []).join(", ") || "not stated",
  };
}

/** Cases the requester expects the change to cover that the stated scope does not name (exact comparison of the requester's own two fields, case-insensitive).
 *  Empty when no scope or no expectation was stated: nothing is guessed. */
export function scopeGap(it: Intent): string[] {
  const sf = (it.structured_form ?? {}) as Record<string, unknown>;
  const scope = (sf.scope && typeof sf.scope === "object" ? Object.values(sf.scope as Record<string, unknown>) : []).map((v) => String(v).trim().toLowerCase()).filter(Boolean);
  const applies = ((sf.applies_to as string[] | undefined) ?? []).map((v) => String(v).trim()).filter(Boolean);
  if (!scope.length || !applies.length) return [];
  return applies.filter((a) => !scope.includes(a.toLowerCase()));
}
