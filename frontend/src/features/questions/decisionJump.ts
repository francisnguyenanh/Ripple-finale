// SCN-17: "Go to ... controls" on a business change. The target is chosen from the obligation this person is asked to do NOW (task projection, never a role
// name) and from the controls the review page actually renders for it under the current permissions; no target, no link. The jump only scrolls and moves focus:
// it never submits, approves, commits or applies, and a disabled control sends focus to its stated reason instead.
export interface JumpTarget { id: string; disabled: boolean; reasonId?: string; label: string }

/** The first asked-now obligation that has a rendered control on this page. */
export function pickJump(askedNow: string[], targets: Record<string, JumpTarget | undefined>): JumpTarget | null {
  for (const code of askedNow) {
    const t = targets[code];
    if (t) return t;
  }
  return null;
}

/** Scroll to and focus the control (or its reason when disabled). Refuses a control that is missing, hidden or inside an inert region; returns what it did. */
export function jumpTo(t: JumpTarget, doc: Document = document): "control" | "reason" | "none" {
  const el = doc.getElementById(t.id);
  const shut = el?.closest("details");                                                 // content of a closed disclosure is not focusable
  if (!el || el.closest("[hidden],[inert],[aria-hidden='true']") || el.getClientRects().length === 0 || (shut && !shut.open && !el.closest("summary"))) return "none";
  const reason = t.disabled && t.reasonId ? doc.getElementById(t.reasonId) : null;
  const target = reason && reason.getClientRects().length ? reason : el;
  if (target === reason && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  const reduce = doc.defaultView?.matchMedia("(prefers-reduced-motion: reduce)").matches ?? true;
  target.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  target.focus({ preventScroll: true });
  return target === el ? "control" : "reason";
}
