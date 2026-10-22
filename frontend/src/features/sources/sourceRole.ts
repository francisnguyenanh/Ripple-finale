// The kind of source a file is decides how the AI reads it at extraction (a policy states requirements, code shows behaviour, records show
// what happened). The person sets it at import; the file type only pre-fills a visible suggestion, and "unknown" stays a valid answer.
export const SOURCE_ROLES = [
  { value: "normative", label: "Policy or procedure (rules people must follow)" },
  { value: "implementation", label: "Code or configuration (what the system does)" },
  { value: "observational", label: "Records or logs (what happened)" },
  { value: "test", label: "Tests" },
  { value: "unknown", label: "Not sure" },
] as const;

export type SourceRole = (typeof SOURCE_ROLES)[number]["value"];

const BY_EXTENSION: Record<string, SourceRole> = {
  md: "normative", txt: "normative",
  py: "implementation", js: "implementation", ts: "implementation", java: "implementation", go: "implementation",
  yaml: "implementation", yml: "implementation", json: "implementation", toml: "implementation", ini: "implementation", cfg: "implementation",
  csv: "observational", jsonl: "observational", log: "observational",
};

/** A suggestion from the path only; a workbook or an unlisted type stays "unknown" for the person to decide. */
export function suggestRole(path: string): SourceRole {
  const p = path.toLowerCase().replace(/\\/g, "/");
  const name = p.split("/").pop() ?? p;
  if (/(^|\/)tests?\//.test(p) || /^test_|_test\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$/.test(name)) return "test";
  const ext = name.includes(".") ? name.split(".").pop() ?? "" : "";
  return BY_EXTENSION[ext] ?? "unknown";
}
