// Display text of one completion-axis value. A missing value is "Unavailable"; it is never the word "undefined", never "none",
// and never a success wording.
export function axisDisplay(raw: unknown): { text: string; known: boolean } {
  if (raw === null || raw === undefined) return { text: "Unavailable", known: false };
  const t = String(raw).replace(/_/g, " ").trim();
  if (!t || t === "undefined" || t === "null") return { text: "Unavailable", known: false };
  return { text: t, known: true };
}
