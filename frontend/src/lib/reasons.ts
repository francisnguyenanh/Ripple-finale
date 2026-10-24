// Plain English for the machine reasons the server returns (reason codes, key names inside sentences).
// Business view shows these sentences; Technical view shows the server text unchanged. Anything not in the
// table falls back to the words of the code (never silently dropped), so a new code is still readable.
import { humanize } from "./fmt";

const CODES: Record<string, string> = {
  quote_not_in_stored_observation: "the quoted text was not found in the stored file, so it was not used",
  empty_corpus: "no sample data was available to simulate with",
  no_confirmed_relation_evidence_bounded_investigation_not_run: "no relation evidence covers this rule, so its effect is unknown",
  knowledge_not_accepted: "the knowledge version has not been accepted yet",
  human_authoring_required: "a person has to write this change",
  human_approval_required: "a person has to approve this change",
  review_blocked: "the review found a blocker",
  model_mismatch: "the AI answered with a different model than the one configured",
  served_model_differs_from_pinned_role_model: "the AI answered with a different model than the one configured",
  reanalysis_deferred_for_budget: "some files were not re-read because the cost limit of this project was reached, so their rules are not current",
  model_data_policy_denied: "the project policy does not allow this data to be sent to the AI, so nothing was sent",
  provider_disarmed: "AI actions are switched off on this server, so nothing was sent",
  allocation_exhausted: "the approved allowance for this kind of AI action is used up, so nothing was sent",
  role_not_enabled: "this AI feature is not enabled on this server, so nothing was sent",
  ai_roles_not_configured: "no AI feature is enabled on this server, so nothing was sent",
  ai_config_unreadable: "the server's AI configuration cannot be read, so nothing was sent",
  quota_used: "the AI replies of this conversation are used up",
  stale: "it is out of date",
  UNAVAILABLE: "not available",
  NOT_RUN: "not run",
  PENDING_HUMAN: "waiting for a person",
};

const FIELDS: Record<string, string> = {
  applies_to: "cases it should cover", old_value: "current value", new_value: "new value", subject: "what is changing", scope: "applies to",
  unit: "what the amount measures", currency: "currency",
};

/** Business-view text for one server sentence. */
export function plain(text: unknown, technical: boolean): string {
  const s = String(text ?? "");
  if (technical) return s;
  const NOUN: Record<string, string> = { art: "a file", cand: "a rule", job: "a job", rev: "a version", src: "a version", km: "a knowledge version", vc: "the contract", intent: "the request", chat: "the conversation", dec: "a decision", finding: "a finding" };
  let out = s
    .replace(/job [^:]+: (\d+) candidates share the lineage key; not merged/g, (_m, n: string) => `${n} rules read from this file look like the same rule, so they were kept as separate entries and not merged`)
    .replace(/dependency_coverage_missing:\s*\[([^\]]*)\]/g, (_m, list: string) => {
      const n = list.split(",").map((x) => x.trim()).filter(Boolean).length;
      return `${n} unchanged file${n === 1 ? "" : "s"} had no relation coverage, so every file was read again`;
    })
    .replace(/dependency_coverage_available_for_all_unchanged_artifacts/g, "relation coverage exists for the unchanged files, so only the changed files were read again")
    .replace(/\b(?:sha256[=:]\s*)?(?:sha256:)?[0-9a-f]{64}\b/g, "(fingerprint)")
    .replace(/\bid=['"]?((?:art|cand|job|rev|src|km|vc|intent|chat|dec|finding)-[0-9a-z_-]{6,})['"]?/g, (_m, id: string) => NOUN[id.split("-")[0]] ?? "a record")
    .replace(/\b(art|cand|job|rev|src|km|vc|intent|chat|dec|finding)-[0-9a-z_-]{6,}\b/g, (_m, k: string) => NOUN[k] ?? "a record")
    .replace(/\bapplies_to:([A-Za-z0-9 _-]+):\s*/g, (_m, x: string) => `the case "${x}": `)
    .replace(/\b(applies_to|old_value|new_value|subject|scope|unit|currency):\s*/g, (_m, k: string) => `${FIELDS[k]}: `);
  for (const [code, words] of Object.entries(CODES)) out = out.replace(new RegExp(`\\b${code}\\b`, "g"), words);
  // Last resort: a remaining code-like word is shown as words (the exact code stays in the Technical view).
  out = out.replace(/\b[a-z0-9]+(?:_[a-z0-9]+)+\b/g, (m) => humanize(m).toLowerCase());
  out = out.replace(/\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b/g, (m) => humanize(m).toLowerCase());
  return out;
}

/** What a failed AI job means for a person and what to do next. `reason` is the server's `execution_reason`; the Technical view still shows it unchanged. */
export type FailureAdvice = { what: string; next: string; retryHelps: boolean };

export function failureAdvice(reason: unknown): FailureAdvice {
  const r = String(reason ?? "");
  const finish = /finish_reason=([A-Z_]+)/.exec(r)?.[1];
  if (r.startsWith("provider_output_truncated")) {
    return {
      what: "The AI's answer was cut off because it reached its length limit, so it was not used. Nothing from it was saved as a draft.",
      next: "Trying again with the same limit ends the same way. An administrator has to raise the AI length limit on the server first; then use Try again. The cost of this run is shown in Support & Access.",
      retryHelps: false,
    };
  }
  if (r.startsWith("provider_response_blocked") || r.startsWith("provider_prompt_blocked")) {
    return {
      what: "The AI provider withheld its answer" + (finish ? ` (${finish.toLowerCase().replace(/_/g, " ")})` : "") + ", so nothing was drafted.",
      next: "Check the wording of the request and the sources it relies on. If you change the request, confirm it again, then use Try again.",
      retryHelps: false,
    };
  }
  if (r.startsWith("provider_execution_failed")) {
    return {
      what: "The AI service did not return a usable answer, even after the automatic tries, so nothing was drafted.",
      next: "Use Try again. If it fails again, tell the administrator: the exact cause is in the Technical view and on the attempt record.",
      retryHelps: true,
    };
  }
  if (/exceeds? (the )?input ceiling|chunks? exceed/.test(r)) {
    return { what: "There is too much text in the files you asked about to read in one question, so nothing was sent.", next: "Open 'Advanced: choose the files to search' and pick fewer files, then ask again.", retryHelps: false };
  }
  if (r.startsWith("budget_exceeded") || r.startsWith("allocation_exhausted")) {
    return { what: "The approved cost or job allowance for this kind of AI action is used up, so nothing was sent.", next: "An administrator has to approve more allowance. Nothing was changed.", retryHelps: false };
  }
  if (r.startsWith("role_not_enabled") || r.startsWith("ai_roles_not_configured") || r.startsWith("ai_config_unreadable")) {
    return { what: "This AI feature is not enabled on this server, so nothing was sent and nothing was charged.", next: "The operator decides which AI features this server offers. Saved results can still be read.", retryHelps: false };
  }
  if (r.startsWith("provider_disarmed")) {
    return { what: "AI actions are switched off on this server, so nothing was sent.", next: "Ask the operator to switch them on, then use Try again.", retryHelps: false };
  }
  return { what: plain(r, false), next: "If it is not clear, tell the administrator; the exact text is in the Technical view.", retryHelps: true };
}
