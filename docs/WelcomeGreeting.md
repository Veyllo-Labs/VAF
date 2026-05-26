# Welcome Greeting — Design & Implementation

## What it is

When the chat is empty (no messages), a welcome screen is shown with an animated
AgentAvatar and a title that types itself out character by character.

The title is **randomly selected** from a pool of greetings each time the empty
state appears (new chat, session switch). If the user's name is known, personalised
variants like *"Hey Mert, womit kann ich helfen?"* are included in the pool.

---

## Where the text lives

Greetings are defined in the i18n message files:

| File | Section |
|------|---------|
| `web/messages/de.json` | `main.welcomeGreetings` |
| `web/messages/en.json` | `main.welcomeGreetings` |

Each file contains an array of strings. Entries that contain `{name}` are
**personalised variants** — they are only included in the random pool when a
user name is available. Entries without `{name}` are always eligible.

### Adding a new greeting

1. Open `web/messages/de.json` — add your German string to `main.welcomeGreetings`
2. Open `web/messages/en.json` — add the English equivalent at the **same array index**
3. Use `{name}` as placeholder if the greeting should address the user by name

```json
// de.json
"welcomeGreetings": [
  "Womit kann ich helfen?",
  "Hey {name}, womit kann ich helfen?",
  ...
]

// en.json  
"welcomeGreetings": [
  "How can I help you?",
  "Hey {name}, how can I help?",
  ...
]
```

> The indices do NOT need to match between languages — each locale picks randomly
> from its own array. You do not need to keep them in sync.

---

## User name source

The name comes from `user_identity.json` (stored at `~/.vaf/users/{username}/user_identity.json`).

The frontend fetches it via:
```
GET /api/user/persona  →  { user_identity: { name: "Mert", ... } }
```

This fetch already happens on page load (for time format). The name is stored
in the `userName` state variable alongside `userTimeFormat`.

If no name is set (empty `user_identity.json` or API unreachable), personalised
variants are excluded and a generic greeting is shown instead.

---

## Implementation — page.tsx

**Component:** `TypingTitle` (defined near `AgentAvatar`, around line 606)
- Takes a `text` prop and types it out character by character (38–60ms/char)
- Restarts animation whenever `text` changes

**State:**
- `userName: string | null` — fetched from `/api/user/persona`
- `welcomeText: string` — randomly selected greeting, computed by `useEffect`

**Selection logic:**
```tsx
useEffect(() => {
    if (!isEmpty) return;
    const raw = tMain.raw('welcomeGreetings') as string[];
    const pool = userName
        ? raw
        : raw.filter(g => !g.includes('{name}'));
    const picked = pool[Math.floor(Math.random() * pool.length)];
    setWelcomeText(picked.replace('{name}', userName ?? ''));
}, [isEmpty, userName]);
```

The effect runs whenever:
- The chat becomes empty (`isEmpty` transitions to `true`) — e.g. new chat
- The user name loads for the first time after page load

---

## Visual

```
        [●]          ← AgentAvatar (scale 1.8, idle mode, floating)

  Hey Mert, womit    ← TypingTitle — types out after 50ms delay
  kann ich helfen?

  Starte eine Unterhaltung...  ← static subtitle
```
