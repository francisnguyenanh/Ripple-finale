// The server's AI role allowlist as the page sees it (meta `ripple-ai-kinds`, written by the server from its budget configuration).
// "all" = every AI kind; "none" = no AI kind; otherwise a comma-separated list of job kinds. No meta = the server names no AI
// configuration, so availability is decided by the provider switch alone (the server still refuses anything it does not allow).
export type AiKinds = "all" | Set<string>;

export function parseAiKinds(content: string | null | undefined): AiKinds {
  const c = (content ?? "").trim();
  if (!c || c === "all") return "all";
  if (c === "none") return new Set();
  return new Set(c.split(",").map((k) => k.trim()).filter(Boolean));
}

export function kindEnabled(kinds: AiKinds, kind: string): boolean {
  return kinds === "all" || kinds.has(kind);
}

/** Plain words for an AI kind, for the "not enabled on this server" notes. */
export const AI_KIND_WORDS: Record<string, string> = {
  extraction: "AI reading of source files",
  "knowledge.gap_proposals": "AI-suggested gaps and disagreements",
  "gap.answer_interpret": "AI reading of a typed answer",
  "knowledge.qa": "AI answers to business questions",
  "change.understand": "AI understanding of a change request",
  "change.impact_deps": "AI-proposed dependencies",
  "change.draft_guidance": "AI guidance on a change draft",
  "intent.interpret": "AI interpretation of chat messages",
  "change.draft": "AI change drafts",
  "impact.judge": "AI impact judgement",
  "gap.answer": "AI-suggested answers to open questions",
  explain: "AI explanations",
  "sim.analysis": "AI reading of simulation results",
};

export const NOT_ENABLED = "This AI feature is not enabled on this server.";

/** Why an AI button is not usable now, or undefined when it is: the provider switch first, then the role allowlist. */
export function aiWhy(s: { aiOff: boolean; aiKind: (k: string) => boolean }, kind: string): string | undefined {
  if (s.aiOff) return "AI actions are switched off on this server";
  return s.aiKind(kind) ? undefined : NOT_ENABLED;
}
