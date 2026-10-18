# UI development rules

Normative guidance for changing the Ripple frontend (`frontend/`, React + Vite + Tailwind v4, served under a strict CSP). Read this file first, then the references below. Backend API, permission and decision contracts stay authoritative for business behaviour; the design system cannot grant a permission or redefine what "accepted" or "verified" means.

References: [tokens](DESIGN_TOKENS.md) · [components](COMPONENT_GUIDELINES.md) · [layout patterns](LAYOUT_PATTERNS.md) · [content and states](CONTENT_AND_STATES.md) · [accessibility](ACCESSIBILITY.md).

## 1. Sources of truth
- Token values: `frontend/src/index.css` only. DESIGN_TOKENS.md documents meaning and usage; there is no second palette.
- Shared components: the code under `frontend/src/components/`. COMPONENT_GUIDELINES.md documents it. If code and docs drift, find the intended behaviour from the real callers and fix both in one change.

## 2. Reuse before creation
Search `components/ui`, `components/bits.tsx`, `StatusBadge` and the local feature helpers first. Extend an existing component compatibly when the meaning is the same. Extract a new shared component only when several real callers, or a critical repeated accessibility behaviour, justify it. A one-use domain view may stay local, composed from the primitives. Do not duplicate a status, evidence or permission display that an existing adapter already covers.

## 3. Styling and tokens
- Colours come from semantic tokens (`bg-surface`, `text-muted`, `border-border`, `text-danger`, ...). No new hex/rgb/hsl, arbitrary-colour class or utility palette class (`text-green-600`) in feature code.
- Only tokens defined in `index.css` exist. `bg-background`-style aliases from other design systems are not defined here.
- Local fonts only; no remote fonts, CDN assets or new UI framework. Do not change the CSP to make a style work.
- Dynamic graph geometry belongs to the graph library; author-defined semantic styling uses tokens.

## 4. Components and layout
Use `Button` (sizes `sm|md|lg`), labelled form controls (`Field`, `TextInput`, `TextArea`) and the shared `Modal`. Choose a task pattern explicitly (see LAYOUT_PATTERNS). Put the main outcome and the next action before long detail. Keep full evidence reachable on demand; local table or graph overflow must be labelled, and essential columns are never dropped on a narrow screen.

## 5. Content and state semantics
Job execution, source verification, accepted knowledge, human approval, user result acceptance, simulation measurement, AI interpretation and coverage are different dimensions; never merge them into one badge. Raw machine states stay in `data-state` and in the Technical view. Labels are scoped adapters; an unknown token is shown neutral, never as success; missing numbers are "Unavailable", never 0. See CONTENT_AND_STATES.

## 6. Actions and asynchronous behaviour
Presentation components receive actions from the owning feature and never call an API or model on mount. Do not change request bodies, headers, bindings or currentness checks while restyling. Pending state prevents double submit; status updates must not steal focus, reset typed input or remount a form. Nothing is auto-approved, auto-confirmed, committed or applied.

## 7. Accessibility
Semantic headings and landmarks, programmatic field errors, text plus icon for state, visible focus, keyboard operation, restored focus after overlays. See ACCESSIBILITY. Automated scans are partial evidence only.

## 8. Checks to run
From `frontend/`: `npm run check:design`, `npm run typecheck`, and the impacted `npm test -- <files>`; browser tests for changed journeys use the repository's browser test harness with an isolated database. Build (`npm run build`, writes `app/ui_next`) only when you own that bundle. `check:design` is a static guard: it cannot prove permissions, approval logic or visual quality. Known legacy debt is listed in `frontend/design-system-policy.json` and printed on every run. The same static checks (`check:design`, `typecheck`, `npm test`) can be run on demand in CI with the manually triggered `frontend-checks` workflow; it never runs on push or pull request and does not include the browser journeys.

## 9. Changing the system
A new shared token, component or pattern ships with its type, states, a real example, the documentation update and tests in the same change. An exception names the exact rule, path, expression fingerprint, reason, owner, a review date, verification and a replacement plan, and expires; it can never exempt authorization, provenance, unknown-value or approval rules. A deprecated pattern lists its replacement and the callers still to migrate.

## 10. Completion checklist
Existing pattern reused or justified; states and props documented; business meaning and guards unchanged; error, partial, stale and denied states accessible; keyboard, narrow screen and both themes checked; `check:design` and impacted tests recorded; docs and example current; no secret or private fixture.
