// Task sections of the Questions page. `ui_section` in the URL selects ONE section to show; `all` (or an unknown value) shows all of them.
// Without a value the page opens on Open questions (owner decision D2 = B), unless the link points at something inside another section
// (a change `chg`, a business answer `q`, a finding, a document answer `qa`): then that section, so every existing link keeps working.
// Sections stay mounted when hidden: switching never discards typed text, never starts a job and never repeats a read.
export type SectionId = "ask" | "change" | "rules" | "gaps" | "source-ask" | "open" | "findings" | "versions";
export type Group = "business" | "source";

export interface Section { id: SectionId; group: Group; label: string }

export const SECTIONS: Section[] = [
  { id: "ask", group: "business", label: "Ask" },
  { id: "change", group: "business", label: "Request a change" },
  { id: "rules", group: "business", label: "Rules" },
  { id: "gaps", group: "business", label: "Business questions" },
  { id: "source-ask", group: "source", label: "Ask about documents" },
  { id: "open", group: "source", label: "Open questions" },
  { id: "findings", group: "source", label: "Findings and conflicts" },
  { id: "versions", group: "source", label: "Knowledge versions" },
];

export const GROUP_LABEL: Record<Group, string> = {
  business: "Business knowledge (accepted rules)",
  source: "Source reading (what the AI read from files, not yet accepted)",
};

export const ALL_SECTIONS = "all";
export const DEFAULT_SECTION: SectionId = "open";

/** What the link points at inside a section, when it names no section itself. */
export interface LinkTargets { chg?: string; q?: string; finding?: string; qa?: string }

/** The selected section id, or null meaning "show all". An unknown or unavailable id is never treated as a selection (all are shown). */
export function selectedSection(param: string, available: SectionId[], link: LinkTargets = {}): SectionId | null {
  if ((available as string[]).includes(param)) return param as SectionId;
  if (param) return null;                                             // "all", or a value this page does not know: nothing is hidden
  const pointed: SectionId | null = link.chg ? "change" : link.q ? "ask" : link.finding ? "findings" : link.qa ? "source-ask" : null;
  if (pointed && available.includes(pointed)) return pointed;
  return available.includes(DEFAULT_SECTION) ? DEFAULT_SECTION : null;
}

export function visible(selected: SectionId | null, id: SectionId): boolean {
  return selected === null || selected === id;
}

/** SCN-17: the page is opened deep on one item of the selected section (a change `chg` in "change", a business answer `q` in "ask", a finding in "findings", a
 *  document answer `qa` in "source-ask"). Only then may the navigator fold into one line; the default section or a section chosen without such a link never does. */
export function deepLinked(selected: SectionId | null, link: LinkTargets): boolean {
  if (!selected) return false;
  return (!!link.chg && selected === "change") || (!!link.q && selected === "ask") || (!!link.finding && selected === "findings") || (!!link.qa && selected === "source-ask");
}
