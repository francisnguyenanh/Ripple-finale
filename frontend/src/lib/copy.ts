// Single English copy module for the application chrome. Original user input and source quotes are
// never routed through here; they are shown verbatim.
export const NAV = [
  { id: "overview", label: "Overview", purpose: "Where this workspace stands and what to do next." },
  { id: "sources", label: "Sources", purpose: "Documents, code and workbooks the knowledge is built from." },
  { id: "explorer", label: "Explorer", purpose: "How the things in your sources relate to each other." },
  { id: "questions", label: "Questions & Decisions", purpose: "Open questions that need a person to decide." },
  { id: "changes", label: "Changes", purpose: "Describe a change in your own words and follow it through review." },
  { id: "impact", label: "Impact & Simulation", purpose: "What a change would touch, and what happens if you try it." },
  { id: "harbor", label: "Sample process trial", purpose: "Run one sample requisition through the pinned Harbor example code under two versions of the configuration. A trial on a sample, not production and not a forecast." },
  { id: "history", label: "History", purpose: "What changed, when, and who decided." },
  { id: "documents", label: "Knowledge Documents", purpose: "Business and technical documents of the accepted knowledge, generated when you open them." },
  { id: "support", label: "Support & Access", purpose: "What this workspace supports and who may do what." },
] as const;
export type PageId = (typeof NAV)[number]["id"];
