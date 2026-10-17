// The stages of one change, in order. The ONE place that gives a stage its number and its title.
export const JOURNEY = [
  { key: "request", title: "Request and confirmation", target: "flow-intent" },
  { key: "draft", title: "AI draft with citations", target: "flow-draft" },
  { key: "analysis", title: "Impact and simulation", target: "flow-analysis" },
  { key: "proposal", title: "Proposal", target: "flow-proposal" },
  { key: "check", title: "Check after the change", target: "flow-check" },
  { key: "review", title: "Review", target: "flow-review" },
  { key: "approval", title: "Approval", target: "flow-approval" },
  { key: "apply", title: "Apply", target: "flow-apply" },
  { key: "refresh", title: "Verification and knowledge update", target: "flow-loop" },
  { key: "accept", title: "Result decision", target: "flow-change" },
  { key: "query", title: "Ask again on the new revision", target: "flow-query" },
] as const;
export type StepKey = (typeof JOURNEY)[number]["key"];

/** "4. Proposal": the number is the position in the registry, so a stage can be added or removed without a gap. */
export const stageTitle = (key: StepKey): string => {
  const i = JOURNEY.findIndex((s) => s.key === key);
  return `${i + 1}. ${JOURNEY[i].title}`;
};

/** The number of a stage, for sentences like "(stage 5)"; follows the registry, never typed by hand. */
export const stageNo = (key: StepKey): number => JOURNEY.findIndex((s) => s.key === key) + 1;
