# Design tokens

Source of truth: `frontend/src/index.css`. This file explains meaning and usage; `npm run check:design` verifies that every colour token below is defined for light, `prefers-color-scheme: dark` and `[data-theme="dark"]`, and that this page names each one.

## Colour tokens (semantic)
Light and dark values are in `index.css`. Token `--x` becomes the Tailwind classes `bg-x`, `text-x`, `border-x` (`--text-muted` is `text-muted`).

| Token | Meaning | Class examples |
|---|---|---|
| `--bg` | page background; also form control fill | `bg-bg` |
| `--surface` | primary surface (cards, dialogs) | `bg-surface` |
| `--surface-2` | subtle surface, hover, callout fill | `bg-surface-2` |
| `--border` | borders and separators | `border-border` |
| `--border-strong` | boundary of form controls (text input, text area, select): at least 3:1 against `--surface`, `--bg` and `--surface-2`; table header rule | `border-border-strong` |
| `--nav-bg` | side navigation background (wide screens), between the command bar and the canvas | `bg-nav-bg` |
| `--nav-hover` | hover fill of an inactive side navigation item | `hover:bg-nav-hover` |
| `--header-bg` | command bar background (navy) | `bg-header-bg` |
| `--header-surface` | hover / pressed fill of a control on the command bar | `hover:bg-header-surface` |
| `--header-border` | bottom rule of the command bar | `border-header-border` |
| `--header-text` | text and icons on the command bar | `text-header-text` |
| `--header-muted` | small labels and chip outlines on the command bar | `text-header-muted`, `border-header-muted` |
| `--header-focus` | focus outline of controls on the command bar (`.rp-cmd`), where `--focus` is below 3:1; popovers from the bar (`.rp-pop`) use `--focus` again | used by `.rp-cmd :focus-visible` |
| `--text` | primary text | `text-text` |
| `--text-muted` | secondary text (never the only place for a critical fact) | `text-muted` |
| `--primary` | primary action, active tab or mode | `bg-primary` |
| `--on-primary` | text and icon on `--primary` | `text-on-primary` |
| `--success` | a completed, scoped outcome (name the scope) | `text-success` |
| `--warning` | needs attention, waiting, partial | `text-warning`, `border-warning` |
| `--danger` | failed, rejected, destructive | `text-danger`, `bg-danger` |
| `--on-danger` | text and icon on `--danger` | `text-on-danger` |
| `--info` | in progress, informational | `text-info` |
| `--sim` | SIMULATED (no real effect) marker; never used for "AI" | `text-sim` |
| `--origin-ai` | origin marker of model-written content (AI interpretation, proposal, suggestion); paired with a dashed border, a sparkle icon and the text "AI" | `text-origin-ai`, `border-origin-ai` |
| `--origin-ai-surface` | tinted surface of a section whose content a model wrote | `bg-origin-ai-surface` |
| `--origin-evidence` | origin marker of source observations and engine computations (solid border; document or gauge icon; text "Source" / "Engine") | `text-origin-evidence`, `border-origin-evidence` |
| `--origin-evidence-surface` | tinted surface of a source or engine section | `bg-origin-evidence-surface` |
| `--focus` | focus outline | used by `:focus-visible` |
| `--overlay-backdrop` | modal backdrop | used by `.rp-dialog::backdrop` |

Colour is never the only carrier of state: pair it with text and an icon. The contrast target is WCAG 2.2 AA; pairings must be measured in a real browser, not assumed.

Origin tokens (EUI-01, 2026-10-20) mark WHERE content comes from, never whether it is correct, checked or approved. Their values were chosen by computed WCAG contrast
from the token values (not copied from a review): marker on `--surface` / `--surface-2` / `--bg` / its own surface, light 6.4-8.1:1, dark 7.8-10.2:1; `--text` on the
origin surfaces 13.7-15.7:1; `--text-muted` 5.6-6.6:1; `--primary` links 5.5-5.7:1. The AI marker is close to `--sim` and `--danger` in lightness, so the label, icon and
dashed border are required, not optional.

Enterprise surfaces (SCN-13, 2026-10-26): three zones and a crisp panel: navy command bar (`--header-*`), tinted side navigation (`--nav-bg`, active item on
`--surface` with a 4px `--primary` marker), stone canvas (`--bg`), white panels (`--surface`, thin `--border`, `shadow-card`). The palette follows the plan 17 §10
reference (a public MIT-licensed console, colour values only, no code copied). Three reference values failed a computed WCAG check and were adjusted, keeping the
hue: `--primary` #1D6FD0 → #1A63BC (link text was 4.2:1 on `--surface-2` / `--nav-bg`; now 5.0-5.9:1, white on it 5.9:1); `--warning` #A15C00 → #965500 (4.4:1 on the
new `--surface-2`; now ≥ 4.95:1); `--border-strong` #C5C0B7 / dark #3A4656 → #8A857C / #647081 (1.8:1 is too faint for a form-control boundary; now ≥ 3.1:1 on
every light / dark surface). `--header-focus` was added because `--focus` is 2.4:1 on the navy bar. Computed values: `--header-text` on `--header-bg` 15.4:1 (dark
15.3), `--header-muted` 9.0:1 (dark 7.3), `--header-focus` 7.4:1 (dark 8.7); `--text-muted` on `--nav-bg` 5.3:1, on `--nav-hover` 4.7:1. Computed ratios are not a
browser measurement: the real header, menu, select and badges are measured in the browser (`tests/ui_next/test_scn_polish_browser.py`).

## Other tokens
- Typography: Inter Variable (sans), JetBrains Mono Variable (mono, 13px). Body 16/24; page title 24/32; section 18/26; metadata 12-14. Floors (SCN-01): 12 px for eyebrows, metadata and badges; 14 px for table and task text; body stays 16/24 (the floors are not a reason to shrink a page). Hierarchy comes from weight, spacing and colour.
- Density: comfortable is the default for tasks, forms and reading; compact (`DataTable density="compact"`) only changes table padding where many records are compared; it never shrinks body text, touch targets or focus rings.
- Left accents (SCN-01): `border-l-4` with a semantic colour (`ACCENT_CLASS` in bits.tsx), on a neutral surface, selectively: the reader's own step (primary), blocked or partial (warning). Colour repeats what the text says.
- Spacing scale 4/8/12/16/24/32 px. Radii (SCN-13, enterprise scale): `rounded-sm` 4, `rounded-md` 5, `rounded-lg` 6 (was 4 / 8 / 12). Shadows: `shadow-card`, `shadow-pop`.
- Layers (CSS variables, for reference): `--z-sticky` 20, `--z-menu` 40, `--z-tooltip` 50, `--z-skip` 60. A native `<dialog>` opened with `showModal()` is in the browser top layer, above every z-index.
- Breakpoints: `sm` 640, `md` 768, `lg` 1024 (desktop navigation), `xl` 1280. Page content max width 1200 px.
- Control sizes (`Button`): `sm` min 32 px, `md` min 36 px (default), `lg` min 44 px (main touch actions). Form controls min 40 px.
- Motion: 150 ms `anim-in`; all animation and transition are disabled under `prefers-reduced-motion: reduce`.

## Valid raw values
Raw colour values live in `index.css` only (including graph stroke widths and the backdrop). Quoted business text, source quotes and test strings are not styling.

## Check boundary
`check:design` verifies token parity across the three theme blocks, flags undefined token classes and raw colours in feature code, and keeps a visible legacy baseline. It does not judge contrast or visual quality.
