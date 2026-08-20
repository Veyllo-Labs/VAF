# Dark Mode - Color Reference

The exact colors the VAF web UI uses in **dark mode**, for every surface, control,
the agent avatar and its animation. Its twin is [LIGHTMODE.md](LIGHTMODE.md) (same
structure, light values). The shared mechanism and design tokens live in
[DESIGN.md](DESIGN.md); avatar behavior in [AgentAvatar.md](AgentAvatar.md).

Dark mode is a **neutral gray** theme anchored on `#181818`. There is deliberately
**no blue or amber accent** for active/emphasis states - they use a light neutral.
Accent hues (status colors, links, category chips) are kept only where they carry
meaning.

## How it is turned on

- Toggle: **Settings → Interface → Appearance → Dark mode** (above Custom Cursor).
- Store: `web/lib/themeStore.ts` (Zustand), persisted in `localStorage` as
  `vaf_theme` = `light` | `dark`. Absent = light (so existing installs are unchanged).
- Applied as the `dark` class on `<html>`; `darkMode: 'class'` in
  `web/tailwind.config.ts`.
- No flash: a parser-blocking inline script in `web/app/layout.tsx` stamps the class
  before first paint.

## Mechanism (why plain Tailwind classes flip)

The neutral gray palette is a **folding per-utility swap**: `bg-white`,
`bg-gray-50..400`, borders and `text-gray-400..900` are re-pointed at CSS variables
that hold the dark values under `.dark` (in `web/app/globals.css`). So most of the UI
turns dark with **no per-component edits**. The values below are those variables.

Note the fold: `gray-500..950`, `black`, `text-white` and `text-gray-50..300` are
**not** changed by the swap. That is why some dark-mode colors are set as explicit
`dark:` overrides on the component (buttons, toggles, badges, the avatar) - listed in
the Components section.

## Surfaces

| Role | Utility | Hex | Variable |
|---|---|---|---|
| Page background | `bg-gray-50`, `--background` | `#181818` | `--sfc-gray-50` / `0 0% 9.4%` |
| Card / panel / modal / elevated | `bg-white`, `--card` | `#202020` | `--sfc-white` / `0 0% 12.5%` |
| Hover / control fill | `bg-gray-100` | `#262626` | `--sfc-gray-100` |
| Pressed / segment fill | `bg-gray-200`, `--secondary`/`--muted`/`--accent` | `#2d2d2d` (`0 0% 16%`) | `--sfc-gray-200` |
| Strong fill | `bg-gray-300` | `#383838` | `--sfc-gray-300` |
| Knobs / dots surface | `bg-gray-400` | `#484848` | `--sfc-gray-400` |
| Sidebar chat-list fog fade | `--chat-fog` | `#202020` | `--chat-fog` (matches the sidebar, not the page) |

## Borders / lines

| Role | Utility | Hex | Variable |
|---|---|---|---|
| Hairline | `border-gray-100` | `#262626` | `--lin-gray-100` |
| Standard border | `border-gray-200`, `--border`/`--input` | `#2f2f2f` (`0 0% 18%` = `#2e2e2e`) | `--lin-gray-200` |
| Strong border | `border-gray-300` | `#3a3a3a` | `--lin-gray-300` |
| Focus ring | `ring-gray-400`, `--ring` | `#4a4a4a` (ring uses blue `217 91% 60%`) | `--lin-gray-400` |

## Text

| Role | Utility | Hex | Variable |
|---|---|---|---|
| Primary text | `text-gray-900`, `--foreground` | `#ececec` (`0 0% 92.5%`) | `--txt-gray-900` |
| Body / strong | `text-gray-800` / `-700` | `#dadada` / `#c4c4c4` | `--txt-gray-800/700` |
| Mid | `text-gray-600` | `#a0a0a0` | `--txt-gray-600` |
| Secondary / dim | `text-gray-500`, `--muted-foreground` | `#8a8a8a` (`0 0% 54%`) | `--txt-gray-500` |
| Placeholder / icon | `text-gray-400`, `placeholder-gray-400` | `#6b6b6b` | `--txt-gray-400` |
| Light text on dark chips | `text-gray-100..300`, `text-white` | stock (`#f3f4f6`…`#ffffff`) | not folded - stays light |

## Components (explicit `dark:` overrides)

The active/emphasis system is one **light neutral** (no blue, no amber):

| Component | Dark-mode classes |
|---|---|
| Primary / emphasis button (Save, Connect, CTA) | `dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none` |
| Confirm dialog, the emphasis INVERTED (`web/components/ui/ConfirmDialog.tsx`) | safe answer takes the emphasis fill `dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:border-transparent`; confirming answer keeps `bg-gray-900 hover:bg-black text-white` with **no `dark:` fill at all** and adds `dark:border-[#3a3a3a]` |
| Toggle ON track | `dark:bg-[#d9d9d9]` |
| Toggle OFF track | `dark:bg-[#333333]` |
| Toggle knob (state-dependent, must contrast the track) | ON `dark:bg-[#1a1a1a]`, OFF `dark:bg-[#e8e8e8]` |
| Active / selected nav / tab / pill | `dark:bg-[#3a3a3a] dark:text-white` |
| Emphasis badge / chip | `dark:bg-[#3a3a3a] dark:text-gray-100` |
| Decorative icon / step badge (was blue-tinted `bg-gray-900`) | `dark:bg-[#2e2e2e]` |
| User chat bubble | `dark:bg-[#242424]` |
| Sub-agent window surfaces (`WIN_SHELL` / `WIN_CANVAS` / `WIN_CANVAS_ALT` / `WIN_BAR` in `web/components/SubAgentWindow.tsx`) | shell and toolbar `dark:bg-[#202020]`, recessed canvas `dark:bg-[#181818]`, file-row hairline `dark:border-[#262626]`. Raw hex, so nothing folds for them; the constants exist because seventeen sites shared four values and some were bound to be missed (they were - the librarian window shipped with near-white panels). Note the inversion: in light the chrome is the paler surface and the canvas recedes darker, in dark the chrome is the LIGHTER card and the canvas recedes to the page |
| Thinking-process block | flat `dark:from-[#1e1e1e] dark:to-[#1e1e1e]` (no gradient), header `dark:text-gray-300` |

**The inverted confirm pair, and why it needs almost no classes.** In a costly or
irreversible confirmation the emphasis fill `#e6e6e6` sits on the SAFE answer, not on
the confirming one, because the confirmed action costs real work. The fold is what makes
that cheap instead of a fight with the palette: `gray-500..950` and `black` are **not**
folded on surfaces and `text-white` is **not** folded on text, so `bg-gray-900
hover:bg-black text-white` is already right in both themes and the confirming button
needs no `dark:` fill - only `dark:border-[#3a3a3a]`, the hairline that keeps a black
button from vanishing into the `#181818` card. `bg-white` **is** folded (to `#202020`),
which is why the safe button has to state both halves as literals (`dark:bg-[#e6e6e6]`
for the fill, `dark:text-[#181818]` for the ink - `dark:text-gray-900` would render
light, see the first trap below).

The inversion is scoped to costly or irreversible confirmations and is **not** a change
to the primary-button rule above: an ordinary Save / Connect / CTA keeps the emphasis on
the action you want taken.

**Three traps (all caused real bugs):**
- `dark:text-gray-900` renders **light** (`#ececec`) because the text ramp folds - never
  use it as "dark text". For dark text on a light dark-mode surface use a literal, e.g.
  `dark:text-[#181818]`.
- The reverse trap: an accent tint such as `bg-orange-50` or `text-orange-700` looks
  unthemed and is not - every accent hue is re-pointed through `--acc-<hue>-*` under
  `.dark`. Adding a `dark:` override to one replaces the design system's color with an
  ad-hoc value instead of fixing anything. Check `app/globals.css` before assuming; the
  classes that genuinely need a hand-written dark half are raw hex, not named utilities.
- A toggle knob must contrast the track in **both** states; make its fill
  state-dependent, not a single always-light value.

## Agent avatar & animation

The avatar is a rounded square **body** with a white **dot** (eye). See
[AgentAvatar.md](AgentAvatar.md).

| Part | Dark-mode value |
|---|---|
| Active body | `#2d2d2d` (neutral gray - no blue tint) |
| Dim / archived body | `#1e1e1e` (a bit darker, to distinguish from active) |
| Eye (active) | `#ffffff` |
| Eye (dim) | `#8a8a8a` |
| Overlay glyph inks (orbs, rings, halo, check, bang) | `#ececec` (glow dimmed) |
| Step dots - active (think/say) | bright `#e6e6e6` |
| Step dots - done | mid `#6b6b6b` |
| Step dots - pending | faint near-bg ring `#4a4a4a` |
| Expanded-dot halo | `hsl(var(--background))` = `#181818` (was a white glow) |

All the animation **effects glow light** in dark mode - only colors change, never the
geometry or timing. The scene effects run on an `--ink` variable system (`.tsc`/`.asc`
in `globals.css`); under `.dark` it is redefined to a light ink so the thinking glyph
stream (`.tgly`), particles, lens/terminal/globe, and the plan-step boxes (`.planbox`,
driven by `--planbox-ink`) all render light (`#ececec` / soft white glow). The scene's
own body square stays the neutral avatar body (`#2d2d2d`).

## Special surfaces

| Surface | Dark-mode value |
|---|---|
| Scrollbar thumb / hover | `#333333` / `#454545` |
| Text selection | `#3b82f6` (blue) on white |
| ReactFlow controls | bg `#262626`, border `#2f2f2f`, icon `#b0b0b0` |
| ReactFlow minimap | `#202020` |
| ReactFlow attribution | `rgba(24,24,24,0.7)`, text `#6b6b6b` |
| Logs / observation window (timeline, canvas, and the Overview protection dashboard) | Draws with raw inline hex from the `tlColors(dark)` token set in `web/components/NotificationsModal.tsx`, resolved via `useThemeStore` (rule 4 in Adding new UI). Dark status inks use the lighter 400-range hues (green `#4ade80`, red `#f87171`, amber `#fbbf24`); category and semantic hues stay shared across both themes |
| Accent chips (`bg-blue-50` etc.) | HSL-derived dark tints (50 → L12%/S35%, 100 → L16%, 200 → L25%); accent text 600/700/800 → the stock 400/300/200 light hues |
| Semantic accents kept | primary/link blue `217 91% 60%`, destructive red `0 84% 60%` |
| `color-scheme` | `dark` (native selects, date pickers, scrollbars) |

## Protected - stay light in dark mode

These deliberately keep their light appearance:

- Rendered document paper, markdown/preview iframes and exports (Word/Acrobat model).
  Preview iframes are protected for free (the swap does not cross an iframe boundary), but a
  paper rendered as ordinary DOM is not. That one carries **`vaf-doc-paper`**, which is
  attached to the SAME declaration in `globals.css` that defines the light ramp
  (`:root, .dark .vaf-doc-paper { ... }`), so the whole subtree folds back to light and can
  never drift from the light theme, because it IS the light theme.
  `.dark .vaf-doc-paper` (0,2,0) outranks the `.dark` block (0,1,0), so source order does not
  matter; keeping it scoped under `.dark` is what preserves that.
  Do NOT try to protect such a surface by overriding individual utilities: the swap folds
  text and surfaces in OPPOSITE directions, so restoring only the background leaves the text
  folded light on white, i.e. unreadable. Pinned by
  `tests/test_dark_mode_protected_surfaces.py`.
- Monaco editor (uses its own `vs-dark` theme).
- User-content iframes and screenshots.
- Modal scrims (`bg-black/NN`).
- Status / semantic colors: green success, red error, amber/yellow **warnings**, blue
  **info** links and category chips - unchanged in both themes.

## Adding new UI

1. Prefer plain Tailwind neutrals (`bg-white`, `bg-gray-50`, `text-gray-900`,
   `border-gray-200`) - they flip automatically.
2. For an active/on/emphasis element, add the `dark:` override from the Components
   table (never invent a new active color; never reintroduce blue or amber).
3. Never change the non-`dark:` classes for a color fix - light mode must stay
   byte-identical (verify: a token diff should show only `dark:` additions).
4. Raw hex / inline styles / JS palettes / canvas / SVG do not flip - theme them
   explicitly (read `useThemeStore`, or point at a CSS variable such as
   `hsl(var(--background))`).
