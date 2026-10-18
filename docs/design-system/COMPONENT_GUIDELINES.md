# Component guidelines

Implemented shared components. Planned components are not listed until they exist and have a real caller.

| Component | File | Purpose and contract |
|---|---|---|
| `Button` | [button.tsx](../../frontend/src/components/ui/button.tsx) | `variant` primary/secondary/ghost/danger; `size` sm/md/lg (default md); `type` defaults to `button`; `asChild` for links. A disabled action needs a visible reason next to it. |
| `Modal` | [dialog.tsx](../../frontend/src/components/ui/dialog.tsx) | Native `<dialog>` (CSP-safe), `side` center or drawer, restores focus on close. |
| `Tabs*` | [tabs.tsx](../../frontend/src/components/ui/tabs.tsx) | Radix tabs with token styling. |
| `Tip` | [tooltip.tsx](../../frontend/src/components/ui/tooltip.tsx) | Short supplementary label only; never the sole place for a required fact. |
| `Card`, `Muted`, `Callout` | [bits.tsx](../../frontend/src/components/bits.tsx) | Surface with optional `h2` title; secondary text; boxed notice with tone info, warn, danger or wait. |
| `StatePill` | [bits.tsx](../../frontend/src/components/bits.tsx) | Domain state: business label in Business view, raw state in Technical view; `data-state` always carries the machine value. Tone comes from `stateTone` in [stateDisplay.ts](../../frontend/src/lib/stateDisplay.ts); an unknown token is neutral. A label longer than a short status wraps instead of being clipped; pass the full reason as `detail` or as adjacent text. `RUNNING` shows a small decorative dot that pulses only when motion is allowed; the label itself never animates, so its contrast holds in every frame. |
| `StatusBadge` | [StatusBadge.tsx](../../frontend/src/components/StatusBadge.tsx) | Generic loading, empty, queued, running, waiting, stale, partial, failed, success, info or simulated badge: icon plus text. A `success` label should name its scope. |
| `Field`, `TextInput`, `TextArea`, `Select` | [bits.tsx](../../frontend/src/components/bits.tsx) | Labelled controls (`min-h-10`, `bg-bg`). `Select` is the shared native select; give it a visible label through `Field` or an `aria-label`. |
| `Tech`, `TechField`, `Fingerprint`, `Name`, `Path`, `ClaimLabel`, `ClaimTitle` | [bits.tsx](../../frontend/src/components/bits.tsx) | Business and Technical views of the same fact; technical-only content is not rendered in Business view. |
| `Denied` | [bits.tsx](../../frontend/src/components/bits.tsx) | Refused read: plain panel, announces the refusal once, reveals nothing. |

## Decision and evidence presentation (`frontend/src/components/decision/`)
| Component | File | Purpose and contract |
|---|---|---|
| `ResultSummary` | [ResultSummary.tsx](../../frontend/src/components/decision/ResultSummary.tsx) | Titled outcome block: `scopeLabel` says how far the facts reach; `warnings` (`Notice[]`) always render before the content and the action; `availability` `unavailable` shows a notice instead of a guess. Blocking notices use `role="alert"`. |
| `NextActionPanel` | [NextActionPanel.tsx](../../frontend/src/components/decision/NextActionPanel.tsx) | The one next step: visible reason text, optional actor label, `blocked` state, action element supplied by the owning feature (does nothing on mount). |
| `TrustStateSummary` | [TrustStateSummary.tsx](../../frontend/src/components/decision/TrustStateSummary.tsx) | Independent completion axes side by side, never merged into a score or a green badge. A missing axis reads "Unavailable" (`axisDisplay` in [trustAxes.ts](../../frontend/src/components/decision/trustAxes.ts)). |

Other specialist components (semantic delta, findings triage, provenance inspector, simulation outcome, AI interpretation preview) are added only together with their first real consumer and are documented here when they exist.

## Development catalog
Synthetic examples of the shared components in both themes and modes: run `npm run dev` in `frontend/` and open `/app-next/catalog.html` ([Catalog.tsx](../../frontend/src/design-system/Catalog.tsx)). It is not an input of the production build and application code must not import it (`npm run check:design` rule E09). Add an example when a shared variant gets its first consumer; keep data synthetic and callbacks no-ops.

## Rules for these components
- Props carry presentation and callbacks only. No component fetches or mutates on mount.
- Keep `data-testid` and `data-state` stable; tests rely on them.
- Anti-patterns: a second modal implementation, a new status-to-colour map, a custom pill for an instruction-length sentence, a native select styled with an undefined token.

## Adding or changing a component
Update the table above, add a real example in the consuming feature, and extend the unit test that covers the display mapping (see [stateDisplay.test.ts](../../frontend/src/lib/stateDisplay.test.ts)). Deprecations are listed here with the replacement and the remaining callers.

Deprecated: a raw `<select>` with local classes. Replacement: `Select` from `bits.tsx`. Eleven raw selects remain (listed as `E03` legacy debt in `frontend/design-system-policy.json`: Analysis and Apply stages, Harbor, the business change review, Support workspace section, the shell workspace picker); migrate each with the package that owns the page, then delete its policy entry. New raw selects are rejected by `npm run check:design`.
