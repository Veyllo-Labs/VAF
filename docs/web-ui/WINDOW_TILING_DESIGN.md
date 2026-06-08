# Window Tiling

VAF can have several visual panels open on the right edge of the WebUI at the same time. The most
common case is a **browser sub-agent running inside a workflow**: both the **Workflow Runtime**
panel and the browser's **live view** want screen space at once. Rather than stacking them (one
hidden behind the other), VAF **tiles** them side by side. This document explains how that works
today and how it can be generalized.

## The problem it solves

A browser sub-agent running inside a workflow produces live screenshot frames, but the SubAgent
dock is suppressed during a workflow — its textual output is routed to the Workflow Runtime
terminal instead. Without tiling, the user sees "browser running" with no view of what it is doing,
and forcing the dock open puts it underneath the fixed Workflow Runtime overlay on the right edge,
overlapping. Tiling places the browser view immediately to the left of the Workflow Runtime window
so both stay visible.

How the frames themselves reach the UI from the browser subprocess is covered in the
"How frames reach the UI" section of [Browser Agent](../agents/BROWSER_AGENT.md).

## Right-docked panels

These panels are `position: fixed`, pinned to the right edge at `z-40`:

| Panel | Component | Width | Shown when |
|---|---|---|---|
| Workflow Runtime | `VAFWorkflowRuntime` (`right: 0`) | `w-full` / `sm:450` / `md:500` | the workflow store is open |
| Browser live view | `BrowserLiveTile` (`right: 500`) | `w-[460] max-w-[40vw]`, `xl+` | the workflow panel is open and a browser frame exists (not dismissed) |
| SubAgent dock | inside the main content right panel (not fixed) | ~58% of content | the sub-agent panel is open |

The SubAgent dock and the Workflow Runtime overlay are the pair that would otherwise collide; the
browser-in-workflow case is handled by the dedicated tile described below.

## How the browser tile works

`web/components/BrowserLiveTile.tsx` is a slim fixed window — header, URL bar, a live JPEG `<img>`
frame, and a close button. It is positioned with an explicit `right` offset so it docks immediately
to the left of the panel it sits beside:

- `rightOffset = 500` — the Workflow Runtime width at the `md`+ breakpoint, so the tile occupies the
  strip from `right: 500` to `right: 960`.
- Gated to `xl` screens, so two ~500 px panels never crowd out the chat on smaller displays.

`web/app/page.tsx` drives it from `subAgentState.browserFrame` / `browserUrl`:

- On `browser_frame_update` **inside a workflow**, the overlapping SubAgent dock is **not** opened —
  the tile renders the visual instead. Standalone (no workflow), the browser view still uses the
  SubAgent dock as usual.
- `browserFrame` is cleared on `workflow_start` (so no stale frame from a previous run lingers) and
  on `workflow_done` (the tile disappears).
- The tile is user-dismissible for the current run (`browserTileClosed`) and re-armed on the next
  workflow start.

The resulting layout: Workflow Runtime occupies `right: 0–500`, the browser tile `right: 500–960` —
side by side, no overlap.

## Limitations

- The tile is `xl`-gated: on `lg` (1024 px) two ~500 px panels would leave too little room for the
  chat.
- The `rightOffset` is a constant (`500`) matched to the Workflow Runtime width, not measured. It is
  correct for the current single browser-to-runtime pair but does not generalize to three or more
  panels (see below).
- During later workflow steps (for example document writing) the tile keeps showing the **last**
  browser frame until `workflow_done` — acceptable, since it is the last state the browser was in.

## Extending to a general tiling manager

The current implementation handles the one common pair. A general system would let any number of
right-docked panels arrange themselves automatically:

- **Right-dock registry.** A store of the currently-open right-docked panels, each with an id, a
  measured width, and an order. A hook (for example `useRightDockStack()`) returns the cumulative
  `right` offset per panel so they stack horizontally from the right anchor leftward — removing the
  hard-coded `500` and supporting three or more panels.
- **Measured widths.** Read each panel's real width (for example via `ResizeObserver`) so offsets
  stay correct across breakpoints and any future resizable panels.
- **Overflow strategy.** When the combined width exceeds the available space, fall back to tabs or
  stacking (one panel visible, the rest as chips) instead of tiling off-screen. Reserve a minimum
  width for the chat that tiling never consumes.
- **Unify the SubAgent dock.** The SubAgent dock currently lives in the main content flow; making it
  a registered right-dock panel would route every panel through one tiling system.
- **Beyond the browser.** Any visual sub-agent feed (a rendered PDF, a canvas, a map) could register
  as a dock panel and tile automatically.

## Source files

| File | Purpose |
|---|---|
| [web/components/BrowserLiveTile.tsx](../web/components/BrowserLiveTile.tsx) | The tiled browser live view |
| [web/components/workflows/VAFWorkflowRuntime.tsx](../web/components/workflows/VAFWorkflowRuntime.tsx) | The Workflow Runtime panel it tiles beside |
| [web/app/page.tsx](../web/app/page.tsx) | Tile mount, right offset, frame routing, stale-frame clears |

See also [Workflow UI Components](WORKFLOW_UI_COMPONENTS.md) and [Browser Agent](../agents/BROWSER_AGENT.md).
