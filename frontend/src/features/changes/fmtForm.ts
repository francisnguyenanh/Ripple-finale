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
