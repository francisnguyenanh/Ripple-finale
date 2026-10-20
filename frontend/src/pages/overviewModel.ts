// Overview view-model: every figure is either known or null ("could not be read"). A failed read is never turned into 0 or
// "nothing waiting", and the fallback "start a change" step is offered only when everything it depends on is known.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Rec = Record<string, any>;

export interface Summary {
  files: number | null; coveragePartial: boolean | null; rules: number | null; confirmed: number | null;
  questions: number | null; openFindings: number | null; events: Rec[] | null;
  /** Rules of the ACCEPTED business knowledge (a different read model from the rules the AI read from files): 0 when none is accepted, null when unreadable. */
  businessRules?: number | null;
}

/** `undefined` for a part means its read failed. */
export interface Parts { head: Rec; rev?: Rec | null; overview?: Rec; queue?: Rec; audit?: Rec; business?: Rec }

export function buildSummary(p: Parts, recent: (t: string) => boolean): Summary {
  const { head, rev, overview, queue, audit, business } = p;
  const claims = overview ? ((overview.claims ?? []) as Rec[]) : null;
  return {
    files: !head.source_revision_id ? 0 : rev ? (rev.artifacts ?? []).length : null,
    coveragePartial: !head.source_revision_id ? false : rev ? !!rev.coverage && (!rev.coverage.complete || !rev.coverage.admission_complete) : null,
    rules: claims ? claims.length : null,
    confirmed: overview ? ((overview.decisions ?? []) as Rec[]).filter((d) => d.active && d.action === "confirm").length : null,
    questions: queue ? new Set(((queue.items ?? []) as Rec[]).map((i) => i.question_id)).size : null,
    openFindings: overview ? ((overview.findings ?? []) as Rec[]).filter((f) => !f.decision).length : null,
    events: audit ? ((audit.events ?? []) as Rec[]).filter((e) => recent(e.event_type)).slice(-5).reverse() : null,
    businessRules: business ? (typeof business.accepted?.rules === "number" ? business.accepted.rules : 0) : null,
  };
}

export type NextKind = "loading" | "work" | "add-files" | "analyze" | "questions" | "change" | "unknown";
export interface Next { kind: NextKind; text: string; to: string; label: string }

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export function nextStep(sum: Summary | null, work: { count: number; first?: string }, inbox: "loading" | "ready" | "failed"): Next {
  // The server's own work list decides first, so Overview, My work and Changes never disagree about who acts next.
  if (work.count > 0) return { kind: "work", text: `${plural(work.count, "item is", "items are")} waiting for you in My work: ${work.first ?? "open it"}.`, to: "changes", label: "Open the first item" };
  const unknown: Next = { kind: "unknown", text: "Some information could not be read, so the next step cannot be worked out. Try again; the other pages still work.", to: "", label: "" };
  if (!sum || inbox === "loading") return { kind: "loading", text: "Loading what to do next...", to: "", label: "" };
  if (sum.files === 0) return { kind: "add-files", text: "No files have been added yet. Add the documents, code or workbooks this workspace should know about.", to: "sources", label: "Add files" };
  if (sum.files === null || sum.rules === null) return unknown;
  // "nothing has been read" only when there is no accepted business knowledge either: the two read models are different and both are said
  if (sum.rules === 0 && !(sum.businessRules && sum.businessRules > 0))
    return { kind: "analyze", text: "Files are loaded but the AI has not read rules from them yet. Open a file and choose Analyze.", to: "sources", label: "Open Sources" };
  if (sum.questions === null) return unknown;
  if (sum.questions > 0) return { kind: "questions", text: `${plural(sum.questions, "question is", "questions are")} waiting for a person to confirm or answer.`, to: "questions", label: "Review questions" };
  // Starting something new is only suggested when "nothing is waiting" is actually known.
  if (inbox === "failed") return unknown;
  return { kind: "change", text: "Describe what you want to change in your own words. Ripple asks if anything is unclear, then follows it through review.", to: "changes", label: "Start a change" };
}
