# Mobile UI — Conventions & Patterns

How the VAF web UI behaves on phones, and the rules to follow when adding or
changing UI so mobile keeps working without ever touching the desktop view.

## Core principle: desktop stays, mobile is layered on top

The desktop (and the QtWebEngine tray app, which is `pointer: fine`) layout is
the source of truth and must render **byte-for-byte unchanged**. Every mobile
adaptation is *additive* and gated so it only applies on small/touch viewports:

- Tailwind responsive variant `max-md:` (matches `< 768px`). This is the primary
  tool — append `max-md:*` utilities; never remove or change the base classes.
- The runtime hook `useIsMobile()` (`web/hooks/useIsMobile.ts`, SSR-safe, breakpoint
  768px) — for cases that need a JS branch (e.g. rendering different DOM, not just
  different styles). It returns `false` during SSR/first paint, then updates.
- `md:hidden` / `hidden max-md:flex` to show an element on exactly one of the two.

Rule of thumb: if a change is not behind `max-md:` / `isMobile` / a `md:` toggle,
it changes desktop — that is a regression, not a feature.

Breakpoints in use: `md` = 768px is the mobile cutoff almost everywhere. A few
older screens (e.g. the Memory full page, some dashboards) split at `lg` = 1024px;
match whatever the file already uses.

## Reusable patterns (copy the class strings)

### Full-screen sheet (modals / dialogs with real content)

A content modal (form, editor, list, viewer) fills the viewport on a phone instead
of floating as a centered card.

- Overlay (`fixed inset-0 ... flex items-center justify-center p-4`): append `max-md:p-0`
- Shell (`... max-w-* [max-h-[90vh]] rounded-2xl border ... flex flex-col overflow-hidden`):
  append `max-md:max-w-none max-md:h-[100dvh] max-md:max-h-none max-md:rounded-none max-md:border-0`
  (add `max-md:mx-0` if it has an `mx-*`; `max-md:max-h-none` neutralises a `max-h-[Nvh]` cap)
- Header / footer bars: append `max-md:p-4 max-md:shrink-0`
- Scrolling body: append `max-md:flex-1 max-md:overflow-y-auto max-md:p-4`
  (if the body lives inside a `<form>`, the form also needs
  `max-md:flex-1 max-md:flex max-md:flex-col max-md:min-h-0`)

Leave **small confirm / alert / loading dialogs** as centered cards — do not make a
"Delete?" two-button dialog full-screen. Only sheet things with substantial content.

### Off-canvas drawer (a left sidebar / section nav)

Used for the chat sidebar and the Logs section nav. Keeps the sidebar's structure
intact; it just slides in over a scrim on mobile.

- Sidebar element:
  `max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-50 max-md:w-64 max-md:shadow-2xl max-md:transition-transform max-md:duration-300`
  plus `drawerOpen ? "max-md:translate-x-0" : "max-md:-translate-x-full"`
- A hamburger button (`hidden max-md:inline-flex`) in the header opens it.
- A scrim sibling: `fixed inset-0 z-40 bg-black/40 md:hidden`, click closes.
- Close the drawer on selection (a `useEffect` on the selected value, or in the
  item `onClick`).

### Two-pane (sidebar/list + detail) → stacked column

The connection dashboards: a fixed-width pane next to a `flex-1` pane stack on mobile.

- The two-pane row: append `max-md:flex-col`
- The fixed-width pane (`w-72 shrink-0 border-r ...`):
  append `max-md:w-full max-md:max-h-[38vh] max-md:border-r-0 max-md:border-b max-md:shrink-0`
- The `flex-1` main pane: append `max-md:min-h-0` (and `max-md:shrink-0` if the body
  scrolls as one column)
- For a body that was `flex-1 ... overflow-hidden`, append `max-md:overflow-y-auto`
  so the stacked column scrolls.

### Grid stacking

Form-field grids must go single-column on a phone:

- `grid grid-cols-2` (or 3/4) **without** a `sm:/md:/lg:` prefix → append `max-md:grid-cols-1`
  (a 4-stop selector can use `max-md:grid-cols-2`).
- Grids that already carry `sm:/md:/lg:grid-cols-*` already collapse below their
  breakpoint — leave them.

### Compact header (app-like)

Big modal headers overflow a 360px row. Make them compact:

- Header bar: append `max-md:px-4 max-md:py-3`
- Left content row: append `min-w-0 max-md:gap-3`
- Icon box (`w-12 h-12 rounded-2xl bg-* shadow-lg`):
  append `shrink-0 max-md:w-10 max-md:h-10 max-md:rounded-xl max-md:shadow-none`
- Icon inside it: append `max-md:w-5 max-md:h-5`
- Title wrapper div: append `min-w-0`; title `<h2>/<h3>`: append `max-md:text-lg truncate`;
  subtitle `<p>`: append `max-md:text-xs truncate`
- Trim noisy actions on mobile: hide button text labels (`<span className="max-md:hidden">`),
  drop non-essential buttons (`max-md:hidden`) so the close `X` always fits, shrink
  wide controls (e.g. a date `<select>` gets `max-md:max-w-[120px]`).

## Gotchas that actually bit us (read before debugging mobile)

- **Touch scroll "swallowed" on content.** If swiping on a message/card does nothing
  while the edges scroll, an element is eating the gesture:
  - An absolutely-positioned overlay on top (e.g. the chat composer's gradient).
    Make the overlay `max-md:pointer-events-none` and re-enable its real controls
    with `max-md:[&>*]:pointer-events-auto`.
  - A decorative rail on the screen edge under the thumb (the right-edge prompt-nav
    dots). Hide it on mobile: `max-md:hidden`.
  - `touch-action: pan-y` is **angle-strict** — a slightly diagonal swipe is rejected.
    Prefer the default `touch-action: auto` for the chat scroll container; do not set
    `pan-y` unless you specifically need to stop a child's horizontal pan.
- **`position: fixed` inside a transformed ancestor** is clipped to that ancestor,
  not the viewport. The chat composer wrapper carries a `transform`, so a `fixed`
  scrim/popover inside it only covered the composer box. Fixes: close-on-outside via
  a `document` `pointerdown`/`scroll` listener (transform-proof), or portal the
  element to `document.body`.
- **A `flex-1` child won't scroll without `min-h-0`** (and `min-w-0` for horizontal).
  Add it to the scrolling child *and* its flex ancestors.
- **z-index of the drawer scrim.** The scrim must sit **above** the chat composer
  overlay (which is `z-40`) but **below** the drawer (`z-50`) — use `z-[45]`, else the
  bright input field pokes through the dimmed background.
- **ReactFlow graphs on touch.** Pinch/drag work by default, but a dense graph needs
  a wider zoom range so pinch-out shows the whole thing: set `minZoom={0.05}`
  (`maxZoom={4}`). Do **not** tell users to use the +/- buttons; fingers must work.
- **Avoid negative-margin hacks to widen content** (`-ml-11` to pull an answer under
  the avatar clipped the avatar). Restructure instead — e.g. render the element as a
  full-width sibling on mobile via `isMobile`, indented column on desktop.

## Global foundation (`web/app/globals.css`)

Already in place; reuse, don't reinvent:

- `--safe-bottom` / `--safe-*` CSS vars from `env(safe-area-inset-*)`; e.g. the composer
  uses `max-md:pb-[max(1.5rem,calc(var(--safe-bottom)+0.75rem))]` to clear the home bar.
- `@supports (height: 100dvh)` maps `.h-screen`/`.min-h-screen` to `100dvh` so the
  shell tracks the dynamic viewport (browser chrome / keyboard). On desktop `dvh == vh`.
- `@media (pointer: coarse)`: inputs forced to `font-size: 16px` (stops iOS zoom),
  `-webkit-tap-highlight-color: transparent`, `body { overscroll-behavior-y: none }`,
  and an opt-in `.touch-target { min-width/height: 44px }`.
- `web/app/layout.tsx` exports a `viewport` with `viewportFit: "cover"` and
  `interactiveWidget: "resizes-content"`.

## Checklist for a new component / modal

1. Does it open as a modal with real content? Apply the **full-screen sheet** pattern.
   A small confirm/loading dialog stays a centered card.
2. Any two-pane / fixed-width-sidebar layout? **Stack** it (`max-md:flex-col` + the
   pane rules above).
3. Any `grid grid-cols-2+` of form fields without a responsive prefix? Add
   `max-md:grid-cols-1`.
4. Header with icon + title + actions? **Compact** it so the close button always fits.
5. Scrollable body? Confirm the scroll works on touch (no overlay/rail/`touch-action`
   eating it) and the scrolling child has `min-h-0`.
6. Everything new behind `max-md:` / `isMobile` — verify desktop is untouched.

## Verify before committing

From `web/`:

- Types: `node node_modules/typescript/bin/tsc --noEmit` (ignore the pre-existing
  `SoulWizard.test.tsx` noise).
- Build: `node node_modules/next/dist/bin/next build` (must exit 0). `next lint` was
  removed in Next 16 — rely on `tsc`.
- The app serves a prebuilt `.next`; a visible change needs a rebuild **and** an app
  restart to appear. Test on an actual phone against the HTTPS proxy.
