# Action Tag

The Action Tag is a lightweight, prompt-driven convention that lets the agent
declare, in natural language, **which tool it is about to use and why** — right
before it makes the tool call. The declaration is surfaced in the Web UI as its
own collapsible panel, separate from the reasoning (`<think>`) panel.

**Role.** Today it provides **transparency** (the user sees the agent's stated intent), and it is
the hook for a planned **declared-vs-actual** check and **runtime learning** (does what the agent
said it would do match the tool it actually called?). It is **not** the path by which learned tool
know-how reaches the model — that is router-driven and documented in
[WHARE_WANANGA.md](../memory/WHARE_WANANGA.md) ("Delivery"). The layer stays thin: a prompt instruction, Web
UI parsing for display, and a cheap backend matcher that is currently debug-only.

---

## Output format

The agent emits an `<Action>` block after its `<think>` block and immediately
before the tool call:

```
<think>
Simple weather lookup, no sub-agent needed. I will call web_search.
</think>
<Action>
Using web_search to find the current Berlin weather.
</Action>
```
(the tool call follows)

- One short sentence naming the tool and the goal.
- Placed AFTER `</think>` and BEFORE the tool call.
- Omitted for a pure chat reply that uses no tool.

---

## System prompt

The instruction lives in `vaf/core/system_prompt.py` and is injected on both
prompt-construction paths (the base instruction block and the Soul/persona path
in `build_prompt()`), under the heading **Action Declaration (when you use a
tool)**. It asks the model to emit one short `<Action>` block when it uses a tool.

It is phrased as a soft request, not a hard mandate: the tag is for transparency
and a future declared-vs-actual signal, and learned know-how reaches the model by
a separate, Action-independent path (router-driven injection, see
[WHARE_WANANGA.md](../memory/WHARE_WANANGA.md) "Delivery"). Nothing breaks when the tag is
omitted. Emission therefore depends on the model following the instruction and is
not guaranteed on every tool call of a multi-step run.

---

## Web UI rendering

All parsing happens client-side in `web/app/page.tsx`.

### Parsing (`parseContent`)

`parseContent` extracts **every** complete `<think>...</think>` and
`<Action>...</Action>` block from the message content, wherever they appear
(non-greedy, so prose between blocks is never swallowed). Multiple think blocks
are joined; the action blocks are joined. The remaining prose becomes the answer.

This guarantees that **raw tags never appear in the answer bubble**, even when
the model emits a second `<think>` or an `<Action>` after it has already started
answering. A trailing, still-unterminated tag (during streaming) is handled as a
partial block, and the relevant `isThinkingComplete` / `isActionComplete` flag is
set to `false`.

### Display

Two separate collapsible panels are rendered above the answer bubble:

| Panel | Component | Accent | Header |
|-------|-----------|--------|--------|
| Reasoning | `ThinkingDetails` | grey | "Thinking..." while streaming, "Thinking Process" when done |
| Action | `ActionDetails` | amber | "Action..." while streaming, "Action" when done |

Both behave identically: open while streaming, **auto-collapse** shortly after
the stream completes, and can be toggled open/closed manually. Each panel only
renders when it has content.

```
[ Thinking Process            v ]   <- grey, collapsible, auto-collapses
[ Action                      v ]   <- amber, collapsible, auto-collapses
  <answer bubble>                    <- prose only, no tags
```

---

## Action-Tag parser (backend)

This is a **separate** parser from the Web UI one above. `parseContent` (frontend) only
extracts the tag text to render it. The backend parser reads the agent's committed
`<Action>` intent from its own output and matches it against the live tool list. Its purpose is the
**declared-vs-actual** signal — comparing the tool the agent *said* it would use against the tool it
*actually* called — which seeds runtime learning. It does **not** drive know-how injection (that is
router-driven; see [WHARE_WANANGA.md](../memory/WHARE_WANANGA.md) "Delivery").

Location: `vaf/core/agent.py` — helpers `_extract_action_text()` and
`_match_action_to_tools()`, invoked inside the generation loop right after a generation
completes (where `full_response` and the detected tool calls are both known).

Matching is intentionally cheap (no LLM):

- the full tool name appearing in the action text scores `1.0`;
- otherwise the score is the fraction of the tool name's tokens (split on
  non-alphanumerics) that appear in the action text;
- candidates are the currently loaded tools (`_active_tools` if the router narrowed them,
  otherwise all registered tools);
- the top matches (with score) are reported; nothing is filtered or thresholded yet.

### Current state: debug only

The parser does **not** inject anything yet. On each generation that contains an `<Action>`
block it prints a terminal debug line (and writes the same to `logs/backend_*.log`):

```
[ACTION-MATCH] action="Using browser_agent to search Youtube for ..." (candidates=9)
[ACTION-MATCH] match: browser_agent (score: 100%)
[ACTION-MATCH] match: web_search (score: 50%)
[ACTION-MATCH] match: memory_search (score: 50%)
```

### Matching behaviour and limitations (v1, live-verified)

Because matching is pure token overlap against tool **names**, the current matcher is
effectively a "did the agent name the tool literally" detector:

- Works well when the agent writes the tool name in the action (e.g. `browser_agent`,
  `web_search`) -> high score, correct top match.
- **Misses** natural-language or non-English intent descriptions, even when an intended
  tool clearly exists. Observed live: `"Suche nach aktuellen Gruenden fuer den
  Bitcoin-Preisverfall."` -> `no tool match`, although `web_search` was intended — the
  German word "Suche" shares no token with the English name `web_search`. These are
  false negatives, not correct rejections.
- Secondary matches around 50% are usually shared-token noise (e.g. `web_search`,
  `memory_search`, `search_tools` all contain "search").

This is acceptable for the parser's purpose. For **declared-vs-actual** verification, literal-name
matching is usually enough — the agent typically names the tool it then calls — so the
natural-language false-negatives matter less now that injection no longer depends on this matcher.
Logging the actually-called tool next to the declared one (ground truth) is the next step toward the
runtime-learning signal.

Know-how injection is **implemented but decoupled from this matcher**: it runs at tool-schema build
time for the router-selected tools (`Agent.TOOLS` appends each tool's learned pitfalls via
`vaf/whare_wananga/delivery.py`), independent of whether an `<Action>` was emitted. See
[WHARE_WANANGA.md](../memory/WHARE_WANANGA.md) "Delivery".

---

## Persistence (reload and session switch)

The Action panel survives a page reload and switching to another chat and back —
the same way the Thinking panel does.

- **Server history** stores the full assistant content, including the `<think>`
  and `<Action>` tags (the `<think>` strip in `web_server.py` `_detect_language_simple`
  is only for TTS language detection and does not touch stored history).
- **Client cache reconciliation** in `page.tsx` (the WebSocket session-load
  handler) treats `<Action>` exactly like `<think>`:
  - the content-comparison normalizers (`normContent`, `norm`) strip both tag
    types so a server "answer only" version matches a cached "think + action +
    answer" version;
  - the "prefer the cached version" rule fires when the cached message contains
    `<think>` **or** `<Action>`.

The result: on reload the tags come back from server history and are re-parsed;
on session switch the persisted cache restores them.

---

## LLM context behavior

This is a deliberate design decision and differs from `<think>`:

- **`<think>` is stripped from the LLM context** (`vaf/core/agent.py`, "saved for
  UI display only"). Reasoning does not accumulate in the context window.
- **`<Action>` is kept in the LLM context** on purpose. It is short and reinforces
  the agent's committed intent across turns.

Independently of either tag, **the agent's actual actions always remain in
context** via the real `tool_calls` and their `role:tool` results, which
`_prepare_messages` preserves as valid call/result pairs. The agent therefore
always "remembers" what it did, regardless of how the tags are handled.

---

## Files

| File | Role |
|------|------|
| `vaf/core/system_prompt.py` | "Action Declaration" instruction (both prompt paths) |
| `web/app/page.tsx` | `parseContent` (tag extraction), `ThinkingDetails`, `ActionDetails`, session-load reconciliation |
| `vaf/core/agent.py` | Backend Action-Tag parser (`_extract_action_text`, `_match_action_to_tools`, debug match in the generation loop); `<think>` stripped from LLM context; `<Action>` intentionally retained |

---

## Known limitations

- Per-tool-call emission is not guaranteed (model-compliance dependent).
- The Action panel reflects what the model writes; it is not a verified record of
  execution. The authoritative record of a performed action is the tool call and
  its result.

---

## Related Documentation

- [Thinking-Mode.md](Thinking-Mode.md) — background (idle) agent; distinct from the `<think>` reasoning tag described here
- [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md) — system prompt assembly and context/token handling
- [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md) — tool selection and the `input_examples` description channel
- [WEB_UI.md](../web-ui/WEB_UI.md) — Web UI structure and message rendering

---

*Last updated: 2026-06-05*
