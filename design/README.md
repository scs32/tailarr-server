# Podscale design system

The branded style guide for the Podscale web controller. **Tailnet theme** —
slate surfaces + a cyan accent, dark-mode by default. Comps: CasaOS / Umbrel
(app-store feel) and Dockge (clean management panels).

## Files
- **`podscale.css`** — the single source of truth: design tokens (color, type,
  spacing, radius, shadow) as CSS custom properties, plus the component classes
  built on them. Everything else consumes this.
- **`index.html`** — kitchen-sink preview. Open it in a browser to see the
  system assembled (shell + pods + buttons + logs + forms + alerts).
- **`components/*.html`** — one preview per component, each marked with a
  first-line `<!-- @dsCard group="…" -->` comment so it registers as a card in
  a **claude.ai/design** design-system project via `/design-sync`.

## Tokens (Tailnet theme)
| role | var | hex |
|------|-----|-----|
| background | `--bg` | `#0F172A` |
| surface | `--surface` | `#1E293B` |
| border | `--border` | `#334155` |
| text | `--text` | `#E2E8F0` |
| muted | `--muted` | `#94A3B8` |
| accent | `--accent` | `#22D3EE` |
| running / ok | `--ok` | `#34D399` |
| starting / warn | `--warn` | `#FBBF24` |
| error / danger | `--danger` | `#F87171` |

Type: **Inter** (UI) + **JetBrains Mono** (logs/code). Referenced by family
here; self-hosted/bundled in Phase 2 (no font CDN — the tailnet box is offline).

## Components (card groups)
- **Foundations** — color & type swatches, app shell & nav
- **Pods** — pod card (running/stopped/starting/error), status badge
- **Catalog** — catalog card (installable / installed)
- **Forms** — inputs, textarea, toggle, grouped form section, validation
- **Components** — buttons (primary/secondary/ghost/danger + loading), log panel
- **Feedback** — alerts & toasts, empty state, confirm dialog, install stepper

## How this maps onto the SPA (Phases 2–3)
Each card becomes a React component with the same class contract; `podscale.css`
tokens port directly to the SPA's global stylesheet (or CSS-module variables).
The style guide stays the source of truth and is kept in sync with
claude.ai/design via `/design-sync` — incrementally, one component at a time.

## Preview locally
```sh
open design/index.html          # whole system
open design/components/pod-card.html   # one component
```
