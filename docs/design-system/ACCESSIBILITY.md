# Accessibility

Target: WCAG 2.2 AA.

- Landmarks and one `h1` per page (including denied and error states); heading order is consistent.
- Every control has a visible or accessible name; icon-only buttons have `aria-label`.
- Field errors are associated with their field; the message is not colour-only.
- State is text plus icon; colour is secondary. A spinner is never the only progress information.
- Keyboard: all actions are reachable; `Escape` closes overlays and focus returns to the opener; focus is kept across polling and not stolen by status updates. A link that scrolls inside the page must not replace the route hash.
- Disclosures use `aria-expanded` and `aria-controls`; the current navigation item uses `aria-current`. A long workflow opens the hidden target before moving focus.
- Tables: caption, headers, labelled local overflow. Graphs always have a text or tree equivalent.
- Contrast is measured in a real browser in both themes; the focus outline is `--focus`. `prefers-reduced-motion: reduce` disables animation and transition. Never animate the opacity or colour of text: a moving cue goes on a decorative (`aria-hidden`) element beside the text, under `motion-safe:`.
- Narrow screens: no page-level horizontal scroll; critical flows work at 320 px and 200% zoom.

## Verification
Automated: the browser test `tests/ui_next/test_a11y.py` runs axe-core (a pinned dev dependency) over the main pages in light, dark and phone width, and `npm run check:design` guards tokens. These are partial evidence: the keyboard walk-through, zoom and screen-reader sanity are manual, and no automatic result is a conformance claim.
