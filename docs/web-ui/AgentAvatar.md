# AgentAvatar — Design & Behaviour

## Core Idea

The avatar is **not an icon, not a robot, not a symbol** — it is the agent itself.  
A living, organic dot that shows the user what the agent is doing internally right now.  
Like a facial expression, but abstract.

> **Brand & ownership:** the agent avatar — the living-dot visual identity and its animated
> states — is a brand asset of **Veyllo GmbH**. The source code is available under the
> repository license; the brand/identity itself is reserved (see `LICENSE`, section
> "Trademarks and Brand Assets").

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

### Transitions between states — keeping the agent in one piece

The hard case is going *between* states (e.g. `reads newspaper -> juggles`, or
`thinking -> celebrate`). The naive approach — render the new state as a fresh element and
cross-fade it over the old one — always reads as a **slideshow**: two different-looking agents
dissolving into each other. The goal instead is a single, continuous character whose
*surroundings* and *behaviour* change while the character itself never disappears.

**Four rules make a transition feel like one piece:**

1. **Persist the agent.** Never destroy, re-create, or fade out the whole agent on a state
   change. Keep one `body + eye` (or one dot) element alive across the whole session; only
   change what is *around* it and which animation runs *on* it.
2. **Settle to neutral, then start the new animation — never hard-swap mid-motion.** Swapping
   the running `@keyframes` while the old one is mid-cycle makes the element jump to the new
   animation's first frame. Instead, briefly remove the animation so the element eases back to
   its rest pose, and only start the new animation once it has settled.
3. **Author every keyframe to start and end at neutral (`0%` and `100%` = rest).** This is the
   rule that makes (2) work — and it is exactly what was missing for the eye. If an eye that
   "looks left" is written `0%,100% { transform: translate(-3px,0) }`, a freshly started
   animation snaps the eye 3px left. Write it `0%,100% { translate(0,0) }` with the look held
   in the middle (`15% .. 85% { translate(-3px,0) }`): now the animation *eases into* the look
   from rest, just like the body, and the loop boundary never snaps either.
4. **Change the surroundings only after the agent has arrived.** Fade the old props out, move
   the agent, then fade the new props in *after* the move completes — otherwise props appear at
   a position the agent has not reached yet.

#### Reference implementation

The transition player in `docs/animations/agent_avatar/agent-all-animations.html` (section
"0 · Transitions") applies all four to the body+eye scene states (away + activity): one
persistent agent **glides** between each scene's position while only the props cross-fade.

CSS:

```css
/* one persistent agent that glides between scene positions, never destroyed */
#tscene .agent { transition: left .6s cubic-bezier(.5,0,.2,1), top .6s cubic-bezier(.5,0,.2,1); }
/* hold a calm, neutral pose during the glide -> no animation-restart jitter */
.scene.gliding .body { animation: none !important; }
#tscene .body, #tscene .eye { transition: transform .3s ease; }  /* ease back to rest */
/* only the surroundings cross-fade; the agent itself never fades */
#tprops { position: absolute; inset: 0; transition: opacity .3s ease; }
```

JS — three phases per step (props out -> glide in a neutral pose -> arrive: animate + props in).
The agent element is created once and never recreated; each step only swaps the scene class
(which repositions the agent and picks its animation via `.scene.<type> .agent` /
`.scene.<type> .body`) and the contents of the props layer:

```js
function tStep() {
  const it = order[++tIdx % order.length];
  const lite = () => document.body.classList.contains('lite') ? ' light' : '';
  tprops.style.opacity = '0';                                   // 1) old surroundings fade out (agent stays visible)
  setTimeout(() => {                                            // 2) agent glides to the new spot in a neutral pose, no props
    tscene.className = 'scene gliding ' + (it.marker ? it.marker + ' ' : '') + it.type + lite();
    tprops.innerHTML = '';
  }, 260);
  setTimeout(() => {                                            // 3) arrived: drop `gliding` -> animation starts; props fade in
    tscene.classList.remove('gliding');
    tprops.innerHTML = it.props;                                // props only — the persistent agent is untouched
    tprops.style.opacity = '1';
  }, 900);                                                      // 260 (fade-out) + ~640 (glide)
}
```

#### Tuning knobs

Glide `.6s`; arrival at `900 ms` (= 260 fade-out + ~640 glide); props fade `.3s`. Keep the
per-state hold (time between steps) larger than the full transition (~1.2 s) so steps never
overlap — in the player the hold slider starts at 1.6 s for this reason.

#### Same-position switches (the in-chat avatar)

When the avatar only changes *mode* in place — no new position, no props, e.g. the chat avatar
going `thinking -> talking` — rules 1-3 are enough on their own: keep the dot element, let it
settle to neutral, start the next animation (whose `0%` is neutral, so it eases in). No glide
and no cross-fade are needed. Drive everything off a `shown` state that lags the `mode` prop by
the settle duration, so the new animation only starts after the dot has eased to rest:

```tsx
const [shown, setShown] = React.useState<AvatarMode>(mode);
const [settling, setSettling] = React.useState(false);
React.useEffect(() => {
  if (mode === shown) return;
  setSettling(true);                         // drop the animation -> dot eases to neutral (CSS transition on transform)
  const t = setTimeout(() => { setShown(mode); setSettling(false); }, 200);  // then start the new mode from rest
  return () => clearTimeout(t);
}, [mode, shown]);
// the animated dot: animation = settling ? 'none' : ANIM[shown]; with `transition: transform .2s ease`
```

#### Constraint

A single continuous agent only works across states that share the **same representation**. The
away + activity states all use the `body + eye` model, so they share one gliding agent. The base
+ emotion states use the dot-in-square model (different DOM + keyframes) — to fold them into the
same continuous agent, port them to `body + eye` first.

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
