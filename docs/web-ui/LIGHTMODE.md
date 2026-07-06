# Light Mode — Color Reference

The exact colors the VAF web UI uses in **light mode** (the default), for every
surface, control, the agent avatar and its animation. Its twin is
[DARKMODE.md](DARKMODE.md) (same structure, dark values). The shared mechanism and
design tokens live in [DESIGN.md](DESIGN.md); avatar behavior in
[AgentAvatar.md](AgentAvatar.md).

Light mode is the original design. Its values are the **stock Tailwind palette** —
the `:root` CSS variables in `web/app/globals.css` are byte-identical to Tailwind's
defaults, so with the theme toggle off the UI renders exactly as it did before dark
mode existed.

## How it is selected

- Default when `localStorage.vaf_theme` is unset or `light`.
- Toggle: **Settings → Interface → Appearance → Dark mode** (off = light).
- No `dark` class on `<html>`; every color resolves to the `:root` values below.

## Mechanism

The palette is a **folding per-utility swap** (see [DARKMODE.md](DARKMODE.md) for the
full explanation): `bg-white`, `bg-gray-50..400`, borders and `text-gray-400..900`
resolve through CSS variables. In light mode those variables hold the **stock
Tailwind** values, so the swap is a no-op visually — everything below is just
Tailwind's own palette. Components carry `dark:` overrides for their dark values; in
light mode only the base (non-`dark:`) classes apply.

## Surfaces

| Role | Utility | Hex | Variable |
|---|---|---|---|
| Page background | `bg-gray-50`, `--background` | `#f9fafb` | `--sfc-gray-50` / `0 0% 100%` |
| Card / panel / modal | `bg-white`, `--card` | `#ffffff` | `--sfc-white` |
| Hover / control fill | `bg-gray-100` | `#f3f4f6` | `--sfc-gray-100` |
| Pressed / segment fill | `bg-gray-200`, `--secondary`/`--muted`/`--accent` | `#e5e7eb` | `--sfc-gray-200` |
| Strong fill | `bg-gray-300` | `#d1d5db` | `--sfc-gray-300` |
| Knobs / dots surface | `bg-gray-400` | `#9ca3af` | `--sfc-gray-400` |
| Sidebar chat-list fog fade | `--chat-fog` | `#ffffff` | `--chat-fog` |

## Borders / lines

| Role | Utility | Hex | Variable |
|---|---|---|---|
| Hairline | `border-gray-100` | `#f3f4f6` | `--lin-gray-100` |
| Standard border | `border-gray-200`, `--border`/`--input` | `#e5e7eb` | `--lin-gray-200` |
| Strong border | `border-gray-300` | `#d1d5db` | `--lin-gray-300` |
| Focus ring | `ring-gray-400` / accent `ring-amber-500` | `#9ca3af` | `--lin-gray-400` |

## Text

| Role | Utility | Hex | Variable |
|---|---|---|---|
| Primary text | `text-gray-900`, `--foreground` | `#111827` | `--txt-gray-900` |
| Body / strong | `text-gray-800` / `-700` | `#1f2937` / `#374151` | `--txt-gray-800/700` |
| Mid | `text-gray-600` | `#4b5563` | `--txt-gray-600` |
| Secondary | `text-gray-500` | `#6b7280` | `--txt-gray-500` |
| Placeholder / icon | `text-gray-400`, `placeholder-gray-400` | `#9ca3af` | `--txt-gray-400` |

## Components

In light mode the active/emphasis system is **dark** (near-black on white). The
`dark:` overrides listed here do **not** apply; only the base classes render.

| Component | Light-mode classes (base) |
|---|---|
| Primary / emphasis button (Save, Connect, CTA) | `bg-gray-900 text-white hover:bg-black` (`#111827`, white text) |
| Toggle ON track | `bg-gray-800` (`#1f2937`) |
| Toggle OFF track | `bg-gray-200` / `bg-gray-300` |
| Toggle knob | `bg-white` (`#ffffff`) |
| Active / selected nav / tab / pill | `bg-gray-900 text-white` (`#111827`) |
| Emphasis badge / chip | dark chip with light text |
| Decorative icon / step badge | `bg-gray-900` (`#111827`) with a white icon |
| User chat bubble | `bg-gray-800 text-white` (`#1f2937`) |
| Thinking-process block | subtle vertical gradient `from-[#fcfcfd] to-[#f8fafc]`, header `text-[#3b3f4a]` |

## Agent avatar & animation

The avatar is a rounded square **body** with a white **dot** (eye). See
[AgentAvatar.md](AgentAvatar.md).

| Part | Light-mode value |
|---|---|
| Active body | `#111827` (brand near-black) |
| Dim / archived body | `#e5e7eb` (light gray, with a soft lift shadow) |
| Eye (active) | `#ffffff` |
| Eye (dim) | `#b0b0b0` |
| Overlay glyph inks (orbs, rings, halo, check, bang) | `#2a3142` (dark ink on the light surface) |
| Step dots — active (think/say) | `bg-gray-900` / white with dark border |
| Step dots — done | `bg-gray-400` |
| Step dots — pending | `border-gray-400` ring |
| Expanded-dot halo | `hsl(var(--background))` = `#ffffff` |

## Special surfaces

| Surface | Light-mode value |
|---|---|
| Scrollbar thumb / hover | `#e2e8f0` / `#cbd5e1` |
| Text selection | `#000000` background, white text |
| ReactFlow controls / minimap | Tailwind/ReactFlow defaults (light) |
| Accent chips (`bg-blue-50` etc.) | stock Tailwind tints; accent text 600/700/800 stock |
| Semantic accents | primary/link blue `224 76% 48%`, destructive red |
| `color-scheme` | light (default) |

## Protected surfaces

Same set as dark mode (document paper, iframes, exports, Monaco, user content, modal
scrims, status colors) — in light mode they already match the surrounding light UI,
so no special handling is needed.

## Adding new UI

- Use plain Tailwind neutrals; they render as the stock values above.
- Any color fix must keep light mode **byte-identical** — do dark changes as `dark:`
  overrides only, never by editing the base class. A token diff of a dark-mode change
  should show only `dark:` additions (see [DARKMODE.md](DARKMODE.md)).
