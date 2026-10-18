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
| `--text` | primary text | `text-text` |
| `--text-muted` | secondary text (never the only place for a critical fact) | `text-muted` |
| `--primary` | primary action, active tab or mode | `bg-primary` |
| `--on-primary` | text and icon on `--primary` | `text-on-primary` |
| `--success` | a completed, scoped outcome (name the scope) | `text-success` |
| `--warning` | needs attention, waiting, partial | `text-warning`, `border-warning` |
| `--danger` | failed, rejected, destructive | `text-danger`, `bg-danger` |
| `--on-danger` | text and icon on `--danger` | `text-on-danger` |
| `--info` | in progress, informational | `text-info` |
| `--sim` | SIMULATED (no real effect) marker | `text-sim` |
| `--focus` | focus outline | used by `:focus-visible` |
| `--overlay-backdrop` | modal backdrop | used by `.rp-dialog::backdrop` |

Colour is never the only carrier of state: pair it with text and an icon. The contrast target is WCAG 2.2 AA; pairings must be measured in a real browser, not assumed.

## Other tokens
- Typography: Inter Variable (sans), JetBrains Mono Variable (mono, 13px). Body 16/24; page title 24/32; section 18/26; metadata 12-14.
- Spacing scale 4/8/12/16/24/32 px. Radii: `rounded-sm` 4, `rounded-md` 8, `rounded-lg` 12. Shadows: `shadow-card`, `shadow-pop`.
- Layers (CSS variables, for reference): `--z-sticky` 20, `--z-menu` 40, `--z-tooltip` 50, `--z-skip` 60. A native `<dialog>` opened with `showModal()` is in the browser top layer, above every z-index.
- Breakpoints: `sm` 640, `md` 768, `lg` 1024 (desktop navigation), `xl` 1280. Page content max width 1200 px.
- Control sizes (`Button`): `sm` min 32 px, `md` min 36 px (default), `lg` min 44 px (main touch actions). Form controls min 40 px.
- Motion: 150 ms `anim-in`; all animation and transition are disabled under `prefers-reduced-motion: reduce`.

## Valid raw values
Raw colour values live in `index.css` only (including graph stroke widths and the backdrop). Quoted business text, source quotes and test strings are not styling.

## Check boundary
`check:design` verifies token parity across the three theme blocks, flags undefined token classes and raw colours in feature code, and keeps a visible legacy baseline. It does not judge contrast or visual quality.
