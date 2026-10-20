// Deterministic business wording (NOT AI). Numbers, units, scope and state phrases from facts the
// server already returned. Text produced by a model is shown separately and labelled.

export const num = (v: unknown): string => {
  if (v === null || v === undefined || v === "") return "";
  const n = typeof v === "number" ? v : Number(String(v).replace(/[ ,]/g, ""));
  return Number.isFinite(n) ? n.toLocaleString("en-US") : String(v);
};

export const humanize = (s: unknown): string => {
  const t = String(s ?? "").replace(/[_.]+/g, " ").replace(/\s+/g, " ").trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : "";
};

const ANSWER_FIELD: Record<string, string> = { actor: "Who", condition: "When", outcome: "What happens", value: "Value", unit: "Unit", exceptions: "Exceptions", scope: "Applies to" };

export const answerField = (k: string): string => ANSWER_FIELD[k] ?? humanize(k);
export const answerValue = (v: unknown): string => (typeof v === "string" || typeof v === "number" ? String(v) : JSON.stringify(v));

/** A proposed answer for a reader: "Who: finance director" instead of the raw object. Unknown keys are humanized; nested values stay exact. */
export const answerText = (a: unknown): string => {
  if (a === null || a === undefined) return "";
  if (typeof a !== "object") return String(a);
  return Object.entries(a as Record<string, unknown>).map(([k, v]) => `${ANSWER_FIELD[k] ?? humanize(k)}: ${typeof v === "string" || typeof v === "number" ? String(v) : JSON.stringify(v)}`).join("; ");
};

const READING: Record<string, string> = { READY: "complete", NEEDS_CLARIFICATION: "needs clarification", UNSUPPORTED: "not something the form can hold", REFUSED: "refused" };
/** The status of a model's reading of an answer, as words (the raw status stays in data attributes and the Technical view). */
export const readingStatus = (s: unknown): string => READING[String(s)] ?? humanize(String(s ?? "")).toLowerCase();

export const isCurrency = (u: unknown): boolean => /^[A-Z]{3}$/.test(String(u ?? ""));

/** "1,000,000 JPY" (currency) or "1,000,000 net goods amount" (another unit). The exact value only. */
export const valueText = (value: unknown, unit?: unknown, currency?: unknown): string => {
  const n = num(value);
  if (n === "") return "not stated";
  const cur = isCurrency(currency) ? String(currency) : isCurrency(unit) ? String(unit) : "";
  const other = unit && !isCurrency(unit) ? String(unit) : "";
  return n + (cur ? " " + cur : "") + (other && !cur ? " " + other : "");
};

export const scopeText = (scope: unknown): string => {
  const e = Object.entries(scope && typeof scope === "object" ? (scope as Record<string, unknown>) : {});
  return e.length
    ? e.map(([k, v]) => humanize(k).toLowerCase() + ": " + (typeof v === "string" ? v : JSON.stringify(v))).join("; ")
    : "no scope stated";
};

export const basename = (p: unknown): string => String(p ?? "").split("/").pop() ?? "";

// Rules read from code often carry an OPERATOR as the predicate ("less_than_or_equal_to") and the real subject in `subject`
// ("request.net_amount_jpy"). Showing only the predicate made "<= 0" and ">= 1,000,000" read alike, so the subject leads and the operator is spelled out.
const OPERATORS: Record<string, [string, string]> = {
  less_than_or_equal_to: ["at most", "≤"], less_than: ["below", "<"],
  greater_than_or_equal_to: ["at least", "≥"], greater_than: ["above", ">"],
  equal_to: ["", "="], equals: ["", "="], equal: ["", "="], is: ["", "="], value: ["", "="], not_equal_to: ["not", "≠"],
};
const operatorOf = (predicate: unknown): [string, string] | null => OPERATORS[String(predicate ?? "").trim().toLowerCase()] ?? null;

export const ruleTitle = (subject: unknown, predicate: unknown): string => {
  const op = operatorOf(predicate);
  if (op && String(subject ?? "").trim()) return humanize(subject) + (op[0] ? ` (${op[0]})` : "");
  return humanize(predicate) || humanize(subject) || "Rule";
};

/** "Request net amount jpy ≥ 1,000,000 JPY" for an operator rule, "Rule title = value" otherwise. */
export const ruleLine = (subject: unknown, predicate: unknown, value: string): string => {
  const op = operatorOf(predicate);
  if (op && String(subject ?? "").trim()) return `${humanize(subject)} ${op[1]} ${value}`;
  return `${ruleTitle(subject, predicate)} = ${value}`;
};

/** Plain-language state of a job (state names stay in Technical view). */
export const JOB_PHRASE: Record<string, string> = {
  NONE: "Not started",
  QUEUED: "Waiting to run",
  RUNNING: "Running",
  COMPLETED: "Done",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  PENDING_APPROVAL: "Waiting for a person with permission to allow this run",
  APPROVED: "Allowed - waiting to run",
  PENDING: "Waiting",
};
export const jobPhrase = (s: string): string => JOB_PHRASE[s] ?? humanize(s);

export const intentPhrase = (s: string): string =>
  ({
    DRAFT: "Saved - waiting for the person who made it to confirm the request",
    PARSED: "Saved - waiting for the person who made it to confirm the request",
    CONFIRMED: "Confirmed",
    NEEDS_CLARIFICATION: "More details are needed before it can be saved",
    STALE: "Out of date",
  })[s] ?? humanize(s);

export const draftPhrase = (s: string): string =>
  ({ PARTIAL: "Partly processed - some parts need a person", NEEDS_CONFIRMATION: "Needs your confirmation first", COMPLETE: "Complete" })[s] ?? humanize(s);

export const gapPhrase = (s: string): string =>
  ({ unresolved: "Open - no answer yet", commented: "Commented - the next draft does not use a comment until a person confirms it", confirmed: "Decided by a person - the next draft uses it" })[s] ?? humanize(s);

/** Parse text the person typed into a number/bool when it is one; otherwise keep the text. */
export function parseValue(text: string): string | number | boolean {
  const t = text.trim();
  if (/^-?\d+$/.test(t)) return parseInt(t, 10);
  if (/^-?\d*\.\d+$/.test(t)) return parseFloat(t);
  if (t === "true") return true;
  if (t === "false") return false;
  return t;
}

export function newId(prefix: string): string {
  const raw = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now().toString(36);
  return `${prefix}-${raw.replace(/-/g, "").slice(0, 16)}`;
}

export const nodePhrase = (s: string): string =>
  ({
    "pending-confirmation": "Extracted - not yet confirmed", confirmed: "Confirmed by a person (source unchanged)",
    updated: "Read again - differs from the earlier reading", stale: "Out of date (file changed)", "not-analysed": "Not analysed", observed: "Read from the file",
    proposed: "Proposed link (not proven)", conflict: "Conflict - needs a decision", "scope-mismatch": "Different scope - review",
    gap: "Gap", ambiguity: "Ambiguous", "authority-decided": "Authority chosen",
  })[s] ?? humanize(s);
