# VAF Agent Animation

Standalone HTML files that show the **VAF agent avatar** (the living white dot) — for
viewing, screenshots, and post content.

> Design reference for the agent avatar. Lives in the repo under `docs/animations/agent_avatar/`,
> but is not part of the built product (pure reference/spec).
> The actual React integration is documented in `docs/AgentAvatar.md`.

| File | Contents |
|---|---|
| **`agent-all-animations.html`** | **Overview** — every animation from every file in one place, each labeled, with a global light/dark toggle. The best starting point. |
| **`agent-avatar-showcase.html`** | The five base states from the app (idle · waiting · thinking · talking · dim); consistent with the others (own body element + a wink in idle) |
| **`agent-character-emotions.html`** | The **dot as a character** (dot animations as in the app, the body reacts subtly + a wink) — surprised, curious, idea, happy, success … + performance mode |
| **`agent-away-scenes.html`** | **"User-away" scenes** — the agent passes the time (reads the newspaper, watches TV, coffee break, juggles …) + idle mode. For old/archived chats |
| **`agent-activity-states.html`** | **Functional states** — shows *what the agent is doing right now*: 21 states in 4 clusters (Tool & Action · Status & Outcome · Lifecycle · Multi-Agent & Learning), dark + light + cycle |

They all use the **same dot** and the same base keyframes, ported 1:1 from the real
VAF code (`web/app/page.tsx` + `web/app/globals.css`).

---

## Open / view

These are plain HTML files — **no server, no installation needed**. Just open them in a
browser (double-click in the file manager).

**Via terminal (Linux):**
```bash
xdg-open "agent-all-animations.html"       # EVERYTHING in one view (start here)
xdg-open "agent-activity-states.html"      # the functional states
xdg-open "agent-away-scenes.html"          # the "user-away" scenes
xdg-open "agent-character-emotions.html"   # the character page
xdg-open "agent-avatar-showcase.html"      # the base states
```

**macOS:** `open agent-all-animations.html`
**Windows:** `start agent-all-animations.html`

---

## What the showcase page shows

| Section | Contents |
|---|---|
| **Live — interactive** | Dropdown for each mode, size slider, dark/transparent toggle |
| **All states** | `idle` · `idle+dim` · `waiting` · `thinking` · `talking` live & large |
| **Real UI size** | 36 px — exactly as in the chat |
| **Size grid** | 36 / 72 / 126 / 180 / 252 / 360 px |
| **Export** | Avatars on a transparent background (checkerboard = transparent) |

### The 5 states

- **idle** — white dot + static aura, gentle floating (float 15 s)
- **idle + dim** — gray, completely still (older messages / archive)
- **waiting** — slow morph (5.5 s) + breathe (4.0 s)
- **thinking** — focused pulsing (morph 1.0 s + breathe 0.7 s) + glow
- **talking** — rhythmic speaking (talk 0.75 s)

The dot is **white** (`#ffffff`) on a dark, rounded container
(36 px, `bg-gray-900`, `rounded-xl`).

---

## The overview (`agent-all-animations.html`)

A single file that shows **everything** we have — each state labeled individually, in four
sections: **Base**, **Emotions**, **Away scenes**, **Activity**. It unites both
representation layers of the same identity (the **dot** for base/emotions, the **figure**
made of body + eye for away/activity) and has a global **light/dark toggle** at the top.
The best starting point for a quick overall impression.

At the very top sits the **transition player** (section "0 · Transitions"): it runs through a
random list of **all** states and **animates** from each one into the next — every state
collapses to a soft point and blooms into the next (cross-dissolve + scale + blur, works
across both models). Controls: play/pause, reshuffle, duration per state. This is how you see
the **transitions** between the animations.

> **Building it into VAF:** these transitions are documented in the real app code — see
> `docs/AgentAvatar.md`, section *"Universal morph — any state to any state"*. It contains the
> concrete React integration (collapse-to-neutral -> swap -> bloom) matching
> `web/components/AgentAvatar.tsx`. The transition player here is the visual reference for it.

---

## The emotions (`agent-character-emotions.html`)

The living white **dot** is the star — its animations are **1:1 as in the app avatar**.
New: the **body** (the dark square) now reacts *subtly* (its own, restrained animation as a
separate element) — the dot runs unchanged alongside it. Classic animation principles
(squash & stretch, anticipation, overshoot, timing); every loop ends with a short rest beat.
In the **idle state the dot winks** occasionally.

| Group | State | What it signals |
|---|---|---|
| **Base** | Idle | present, winks now and then |
| | Waiting · Thinking · Talking | as in the real app avatar |
| **Reactions** | Surprised | something unexpected happened |
| | Curious | takes a closer look (leans & peeks) |
| | Confused | doesn't understand (yet) |
| | Idea | found a solution (aha flash) |
| **Feelings** | Happy · Excited | is glad / full of energy |
| | Dejected · Tired | slumps / relaxed |
| **Response** | Agreement · Refusal | nodding / head shake |
| | Listening · Searching / Scanning | takes it in / searches (arc sweep) |
| **Highlights** | Success | victory jump; the energy rings fire on **landing** (in sync with the dot) |
| | Working | processing in the background (orbiting satellite) |

Stage with state picker, size slider, background toggle and a **performance mode** that
plays through a small scene.

> Note: this file uses the **dot-centric** model (dot = star, body reacts subtly). Away &
> Activity use the **body+eye** model (the body carries the motion). Both share the same
> identity — dark square + white dot.

---

## The away scenes (`agent-away-scenes.html`)

What does the agent do when nobody is talking to it? These small idle scenes are meant for
the moment when the user opens an **old or archived chat**: the agent visibly "waits" for
you instead of just sitting still.

Important — the agent consists of **two parts**: the dark rounded square (= its **body**)
and the white dot (= its **eye / face**). The props (newspaper, TV, cup …) sit **outside**
its body; it handles them — the body leans and bobs, the eye looks, scans and blinks.

| Scene | What it tells |
|---|---|
| Reads the newspaper | holds the paper up, eye scans the lines, turns the page |
| Watches TV | sits in front of the TV, lit by the flicker, laughs now and then |
| Coffee break | leans toward the cup, takes a sip, sighs "ahh" |
| Juggles | three balls above it in a circling shower pattern, eye tracks them |
| Hums to itself | bobs to the beat, notes rise alongside |
| Plays with the ball | ball bounces in front of it (squash & stretch), eye follows up and down |
| Stargazes | leans back, looks up, stars twinkle, a shooting star |
| Naps | eye nearly shut, breathes heavily, "z z z" rise up |

The stage has a scene picker, size slider, a **light/dark toggle** and an **idle mode** that
cycles through all the pastimes on its own — exactly what the user would see in the away state.

**Light & dark:** the scenes work on both. The body stays the dark square and the eye stays
white; the external props (newspaper, steam, notes, balls, stars, "z z z") recolor from white
to ink via theme variables (`--ink` etc.) so they stay visible on a light background. There
are two galleries — one on dark, one on light.

---

## The functional states (`agent-activity-states.html`)

The operational layer: **what the agent is concretely doing right now** — so the user
understands in real time what it is working on, without reading logs. Same identity (body +
eye); the tools/indicators sit outside and are theme-aware (dark + light). 21 states in
4 clusters:

**1 — Tool & Action**

| State | What it does |
|---|---|
| Searches | magnifier moves over a document, eye follows |
| Writes / edits | types a line, cursor blinks |
| Runs a command | terminal runs, spinner turns, body under tension |
| Browses the web | globe rotates, eye follows |
| Downloads | data packets stream into it |
| Uploads | data packets stream out of it |

**2 — Status & Outcome**

| State | What it says |
|---|---|
| Success | task done (hop + checkmark + ring) |
| Error | something went wrong (jolt + "!") |
| Warning | caution advised (pulsing alert symbol) |
| Needs permission | asks, leans forward, waits for your OK ("?") |
| Blocked | can't get through (bumps against a barrier) |

**3 — Lifecycle & Connection**

| State | What it says |
|---|---|
| Waking | materializes, eye opens |
| Connecting | builds a connection to a node |
| Offline | connection lost, eye flickers out |
| Reconnecting | retry pulses, hoping to reconnect |
| Shutting down | powers off (CRT collapse) |

**4 — Multi-Agent & Learning** (VAF-specific)

| State | What it does |
|---|---|
| Delegates | buds off a sub-agent |
| Handoff | passes a task to a second agent |
| Learns / trains | takes in knowledge — matches Whare Wananga |
| Remembers | accesses memory (nodes light up in sequence) |
| Plans | lays out the steps, eye scans across them |

On the stage: state picker (grouped), size slider, light/dark toggle and a **cycle** that
plays through all 21 automatically.

---

## Creating screenshots / transparent images

**Quick:** in the *Export* section, zoom into the variant you want and crop it with the
screenshot tool:
- Linux: usually the `Print` key, or Shift + `Print` for a region
- macOS: Shift + Cmd + 4
- Windows: Snipping Tool (Win + Shift + S)

The checkerboard pattern marks the transparent areas.

**Real alpha PNG (transparent background, any size):**
That needs a small render step with a headless browser. Say so in chat — then I'll add a
`render.js` (Puppeteer) here that exports every state at every size as `transparent.png`
automatically.

---

## Customizing

All colors, sizes and animation timings sit at the very top of the `<style>` section of each
HTML file (CSS variables + keyframes). They are ported **1:1 from the real VAF code**
(`web/app/page.tsx` + `web/app/globals.css`), so they are faithful to the original.
