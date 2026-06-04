# AgentAvatar — Design & Behaviour

## Core Idea

The avatar is **not an icon, not a robot, not a symbol** — it is the agent itself.  
A living, organic dot that shows the user what the agent is doing internally right now.  
Like a facial expression, but abstract.

---

## Visual States

### 1. `idle` — Resting (latest message)
- **Appearance:** Two-layer white dot:
  - **Back layer** — larger (20 px), blurred (`blur(2.5px)`), semi-transparent (`opacity: 0.13`). **Static** (a soft halo, rasterized once).
  - **Front layer** — sharp white circle (14 px), gently floats + breathes via `agentAvatarIdleFloat 15s` (**scale + translate only** — stays circular)
- **Background:** Dark container (`bg-gray-900`), rounded corners (`rounded-xl`)
- **Feel:** The agent is present but passive. Quietly alive — the dot softly drifts and breathes.
- **When:** Most recent completed bot message, no active streaming

> ⚠️ **Performance note (do not regress):** The idle state must NOT animate `border-radius`,
> `filter`/`blur` or `box-shadow`, and the aura stays static. The app runs in QtWebEngine
> with the GPU in-process, where a continuously *repainting* idle animation leaks GPU memory
> (renderer RSS once climbed to several GB across the visible avatars). Idle animation is
> therefore **compositor-only** (`transform`/`opacity`). The organic **blob-morph** (animated
> `border-radius` via `agentAvatarMorph`) is intentionally limited to the transient active
> states below (thinking / talking / waiting). See `web/app/globals.css` and
> `vaf/core/desktop_window.py` for the full rationale.

### 1b. `idle + dim` — Resting (older messages)
- **Appearance:** Gray circle (`#b0b0b0`), completely still — no animation
- **Background:** Light gray container (`bg-gray-200`)
- **Feel:** Archive. The agent was here, now this message is history.
- **When:** All completed bot messages except the most recent one

---

### 2. `waiting` — System pipeline / before actual processing
- **Appearance:** White dot (`#ffffff`), subtle glow
- **Animation:** Very slow morph (`agentAvatarMorph` 5.5 s) + very slow breathe (`agentAvatarBreathe` 4.0 s)
- **Feel:** Relaxed, patient. "We're here, everything is running, no rush."
- **When:** System steps running (Router, RAG, info-tools) — after the prompt but before actual thinking or answering

---

### 3. `thinking` — Reasoning / think-tags active
- **Appearance:** White dot (`#ffffff`), visible glow (`rgba(255,255,255,0.35)`)
- **Animation:** Two overlapping animations
  - `agentAvatarMorph` — organic border-radius transitions (1.0 s)
  - `agentAvatarBreathe` — scale pulse 1.0 → 1.18 → 1.0 (0.7 s)
- **Feel:** Focused, turned inward. Steady and deliberate.
- **When:** `<think>` tag is open but `</think>` has not yet appeared

---

### 4. `talking` — Answer is streaming
- **Appearance:** White dot (`#ffffff`), slightly larger (15 px vs 14 px), intense glow
- **Animation:** `agentAvatarTalk` — looped (0.75 s)
  - Scale and shape change: 1.0 → 1.38 → 0.74 → 1.30 → 0.80 → 1.0
  - Border-radius shifts in sync — larger = more oval, smaller = more compressed
- **Feel:** Speaking, articulating. Energetic, present, direct.
- **When:** `</think>` is closed, answer text is streaming

---

## Transitions

- `idle → waiting/thinking/talking`: white dot springs in with bounce curve (`cubic-bezier(0.34, 1.56, 0.64, 1)`), idle dot shrinks and fades simultaneously
- `thinking → talking`: animation switches immediately — the moment thinking becomes speaking is intentionally abrupt
- `talking/thinking → idle`: active dot shrinks and fades with fast ease-in, idle dot eases back in with float animation

### Universal morph — any state to any state

The transitions above cover `idle <-> active`. Going *directly* between two active modes
(e.g. `thinking -> celebrate`, `surprised -> happy`) currently swaps the inner animation
abruptly. To make **every** state flow into **every** other state, use a *collapse-to-neutral,
then bloom* morph. This is the technique the standalone reference player uses (the
"Uebergaenge" / transition player in `docs/animations/agent_avatar/agent-all-animations.html`):

1. On a `mode` change, **collapse** the active-dot wrapper toward a small neutral point:
   `opacity -> 0`, `transform: scale(0.45)`, `filter: blur(4-5px)` over ~0.26 s.
2. At the neutral point, **swap** the rendered mode so the new emotion's keyframes start fresh.
3. **Bloom** back: `opacity -> 1`, `scale(1)`, `blur(0)` over ~0.3 s with the spring curve.

Because the morph runs on a *wrapper* (opacity / scale / blur), it is animation-agnostic — it
works for any pair in either direction, and stays compositor-only (no `border-radius` or
`box-shadow` repaint), so it respects the idle performance rule above.

**Minimal integration in `web/components/AgentAvatar.tsx`** — render a `shown` mode that lags
the incoming `mode` prop by one collapse phase, and drive everything (`ANIM`, `size`,
`ORIGIN_BOTTOM`, rings/satellite) off `shown` instead of `mode`:

```tsx
const SPRING = 'cubic-bezier(0.34, 1.56, 0.64, 1)';

const [shown, setShown] = React.useState<AvatarMode>(mode);
const [collapsed, setCollapsed] = React.useState(false);

React.useEffect(() => {
  if (mode === shown) return;
  setCollapsed(true);                 // 1) collapse current to the neutral point
  const t = setTimeout(() => {
    setShown(mode);                   // 2) swap mode at the neutral point
    setCollapsed(false);              // 3) bloom into the new state
  }, 260);                            // ~ collapse duration
  return () => clearTimeout(t);
}, [mode, shown]);

// active-dot wrapper style:
{
  opacity: collapsed ? 0 : 1,
  transform: collapsed ? 'scale(0.45)' : 'scale(1)',
  filter: collapsed ? 'blur(4px)' : 'blur(0)',
  transition: `opacity .26s ease, transform .3s ${SPRING}, filter .26s ease`,
}
```

Tuning knobs (match the reference player): neutral scale `0.45`, blur `4-5px`, collapse
`~0.26 s`, bloom `~0.3 s`. Raise the neutral scale or drop the blur for a snappier, less
"dreamy" morph; a neutral scale of `0` makes the agent disappear fully through the point.
Debounce rapid `mode` changes so a burst of updates does not stack collapses.

The same morph powers the Whare Wananga training stage, where the avatar cycles through the
full emotion range — there it is the *primary* feedback, so the universal morph (not the
abrupt active->active swap) is the better default.

---

## Position in Chat

```
[●] Message text...
 ↑
 AgentAvatar (w-9 h-9, bg-gray-900, rounded-xl)
```

The avatar always sits to the left of the bubble, aligned to the top of the message.  
Older messages: `dim` prop → light gray box, gray static dot.

---

## Usage Map

| Context | Mode | dim |
|---|---|---|
| Last bot message while streaming (think-tag open) | `thinking` | false |
| Last bot message while streaming (answer running) | `talking` | false |
| Most recent completed bot message | `idle` | false |
| All older completed bot messages | `idle` | true |
| Loading bubble (no response yet) | `waiting` | false |
| SystemStep (Router, RAG, info-tools) active | `waiting` | false |
| SystemStep done | `idle` | false |
| Workflow message | `idle` | false |

---

## Implementation

**File:** `web/components/AgentAvatar.tsx` — component `AgentAvatar` (imported by `web/app/page.tsx`)  
**Keyframes:** defined in `web/app/globals.css` (always available on first paint — no runtime injection)  
**Props:** `mode` (all 18 modes; default `'idle'`), `dim: boolean` (default `false`), `invert: boolean` (dark dot on light container, for the judge)  
**Interactive reference:** `docs/animations/agent_avatar/` — standalone single-file HTML showcases of every
state (base, emotions, away-scenes, activity) plus the transition player. Open
`animations/agent_avatar/agent-all-animations.html` to see everything in one place; no build step. These
are design reference / spec, not part of the built product.

**State mapping:**
- `loading === true` → pre-stream (no token yet) → `waiting`
- `isGenerating === true, loading === false` → streaming active → `thinking` or `talking`
- both false, latest message → `idle` (no dim)
- both false, older message → `idle + dim`

```tsx
<AgentAvatar mode="waiting" />
<AgentAvatar mode="thinking" />
<AgentAvatar mode="talking" />
<AgentAvatar />              // idle, latest
<AgentAvatar mode="idle" dim />  // idle, older message
```

---

## Design Principle

> The avatar should make the user feel like they are interacting with something alive —  
> not an app. Animation speed and intensity are directly coupled to the agent's cognitive state:  
> slow and relaxed while waiting, steady while thinking, rhythmic while speaking.  
> White = present. Dim gray = history.
