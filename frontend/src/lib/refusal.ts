// Business wording for the stable refusal codes of the code-candidate and repository routes (ext/1). The server decides and re-checks every action; this
// table only words its answer. An unknown code is never turned into an explanation: it gets a neutral sentence, and the code stays for the Technical view.
import { ApiError } from "./api";

export const REFUSAL_WORDS: Record<string, string> = {
  repo_not_declared: "No repository with this id is available to you in this workspace: it is not declared here, or not visible to your role.",
  repo_not_ready: "The repository is declared but cannot be used right now.",
  remote_config_invalid: "The repository's remote declaration cannot be used.",
  no_read_grant: "This repository's declaration does not allow Ripple to read it.",
  no_publish_grant: "This repository's declaration does not allow opening a pull request. A publish request is only recorded; nothing is published.",
  permission_missing: "Your role does not include this action.",
  human_actor_required: "A person must do this.",
  not_confirmed: "Confirm the request first.",
  knowledge_not_committed: "Commit the knowledge change first: a code candidate only implements decided knowledge.",
  stale_binding: "What you saw is no longer current (the files, the knowledge or the patch changed). Look again before deciding.",
  candidate_not_completed: "The candidate is not ready yet.",
  nothing_to_validate: "The candidate changes no file, so there is nothing to validate.",
  invalid_field: "Something in the request is missing or not allowed. A decision needs a reason in your own words.",
  distinct_approver: "The person who asked for the patch cannot decide it; another person has to.",
  validation_not_passed: "Only a candidate whose validation passed can be approved.",
  knowledge_moved: "The decided knowledge changed after the patch was built. Build a new candidate.",
  already_decided: "This candidate and its validation were already decided.",
  not_approved: "An approval bound to this exact patch is needed first.",
  no_candidate: "No candidate exists for this change yet.",
};

export const UNKNOWN_REFUSAL = "The server refused this. Nothing was changed.";

/** The words for a refusal code; unknown or missing codes get the neutral sentence (never a guessed explanation). */
export function refusalWords(code: string | null | undefined): string {
  return (code && REFUSAL_WORDS[code]) || UNKNOWN_REFUSAL;
}

/** A failed request as {code, words, requestId}; the server's own text is never shown as the explanation. */
export function refusalOf(e: unknown): { code: string | null; words: string; requestId: string | null } {
  if (!(e instanceof ApiError)) return { code: null, words: "Something went wrong in the page. Nothing was changed.", requestId: null };
  if (e.network) return { code: null, words: "The server could not be reached. Nothing was changed.", requestId: null };
  const code = typeof e.body.code === "string" ? e.body.code : e.status === 403 ? "permission_missing" : null;
  if (e.status === 404 && !code) return { code: null, words: "This is not available to your account.", requestId: e.requestId };
  return { code, words: refusalWords(code), requestId: e.requestId };
}
