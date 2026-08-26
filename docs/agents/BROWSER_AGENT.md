# Browser Agent

VAF's `browser_agent` tool gives the AI agent the ability to control a real Chromium browser - navigating pages, clicking elements, filling forms, handling JavaScript-heavy sites, and extracting structured content from live web applications.

Unlike `web_search` (which calls search APIs and returns text snippets) or `webfetch` (which fetches static HTML), `browser_agent` renders pages exactly as a human would see them in a browser.

---

## Architecture

```
VAF Agent (LLM)
    │
    │  decides: "I need a real browser for this"
    │
    ▼
BrowserAgentTool          (vaf/tools/browser_agent.py)
    │                 │
    │  CDP WebSocket  │  Screenshot every 1.5 s (JPEG, ~50 KB)
    │  ws://localhost:9222   │
    │                 ▼
    │           WebSocket → browser_frame_update → WebUI
    ▼
vaf-browser               (Docker container)
    └── Chromium (HEADED, under KasmVNC's X server) --remote-debugging-port=9223
            ├── socat 9222 → 9223   (CDP reachable via Docker port map)
            └── KasmVNC WebSocket stream of the display on 127.0.0.1:6901
                (the interactive browser in the web UI; see
                [Interactive browser](#interactive-browser-driving-the-sandbox-by-hand))
```

**Chromium runs in a dedicated Docker container** (`vaf-browser`). VAF connects to it via the [Chrome DevTools Protocol (CDP)](https://chromedevtools.github.io/devtools-protocol/) over a local WebSocket. No browser is ever installed on the host machine.

The browser container is part of `docker-compose.memory.yml` and starts automatically alongside all other VAF services.

---

## Setup

### Start the browser container

```bash
docker compose -f docker-compose.memory.yml up -d
```

On first run, Docker builds the `vaf-browser` image from `docker/browser/Dockerfile` (~30 seconds). Later starts pass `--build` too: an unchanged build context is a cache hit and costs seconds, while a changed one is rebuilt rather than silently reused. Without that, a checkout that moves ahead of its images keeps running the old ones with nothing to show for it.

### Verify it's running

```bash
docker ps | grep vaf-browser
curl http://localhost:9222/json/version
```

A successful response looks like:
```json
{
  "Browser": "Chrome/...",
  "webSocketDebuggerUrl": "ws://localhost:9222/..."
}
```

---

## How the Agent Uses This Tool

The VAF agent receives `browser_agent` as one of its available tools alongside `web_search`, `webfetch`, and others. The agent's LLM decides autonomously when to use it based on the tool description and the task at hand.

### When the agent picks `browser_agent`

| Situation | Tool chosen |
|---|---|
| "What's the current price of X on amazon.com?" | `browser_agent` - prices are loaded by JS |
| "Search for Python tutorials" | `web_search` - static content, faster |
| "Log into my dashboard and get the usage report" | `browser_agent` - requires login + navigation |
| "Get the plain text of this Wikipedia article" | `webfetch` - static HTML, no interaction needed |
| "Fill out the contact form on example.com" | `browser_agent` - form interaction required |

### Tool call format

The agent calls the tool with a plain-language `task` parameter:

```json
{
  "task": "Go to news.ycombinator.com and return the top 5 story titles with their scores"
}
```

The agent may also specify:

```json
{
  "task": "Log into app.example.com with user@company.com / secret123, go to /reports, extract the Q1 summary table",
  "allowed_domains": ["app.example.com"],
  "max_steps": 40
}
```

### What happens internally

Once the agent calls `browser_agent`, the following happens:

```
1. BrowserAgentTool.run() is called (synchronous, VAF tool contract)
   │
2. _run_async_in_new_loop() spawns a new thread + event loop
   │
3. BrowserSession connects to Chromium via CDP (ws://localhost:9222)
   │
4. Screenshot loop starts (parallel task):
   │   Every 1.5 s: take_screenshot() → emit browser_frame_update → WebUI live view
   │
5. browser-use Agent loop starts (vision tier decided from the lane model):
   │
   ├── Capture DOM snapshot of current page
   ├── If page is unclear / CAPTCHA detected: also attach screenshot
   ├── Send DOM (+ optional screenshot) to VAFLLMBridge
   │     ├── Provider supports native vision → image passed directly
   │     └── Provider has no vision (e.g. DeepSeek) → vision_provider called
   │           → screenshot described as text → injected into message
   ├── LLM decides next action: navigate / click / fill / extract / done
   │     └── Can also call describe_page_visually() when explicitly stuck
   ├── Execute action on Chromium via CDP
   └── Repeat until task complete or max_steps reached
   │
6. Screenshot loop stops; persistent session cookies saved (if persistent=true)
   │
7. Extract final result from agent history
   │
8. Return result string to VAF agent
```

### Stopping a run

For a normal chat turn the browser runs **in-process** (the killable child-process mode is only used inside workflows, opted in via `VAF_SPAWN_BROWSER_SUBAGENT`). Pressing **Stop** in the WebUI sets a per-session stop flag (`TaskQueue.request_stop`). Two lanes watch that flag; both need the session id, which `run()` resolves on the calling thread and hands into the browser thread as an argument - the run executes on a fresh thread whose contextvar context would answer `None`, and a `None` session id silently disarms every stop lane (a live incident shipped exactly that: ten Stop presses, none seen).

**Lane 1 - the in-loop `_stop_monitor`** (a coroutine, polls every 0.5 s), for the healthy case:

1. calls browser-use's cooperative `agent.stop()` - the run halts cleanly at the next step boundary. This is the reliable path: a bare asyncio cancel cannot interrupt a blocking LLM call running in the executor thread, and browser-use can swallow a single `CancelledError` mid-step.
2. then cancels the run task to unblock as soon as the current step returns.

The monitor keeps trying until the run actually ends, instead of giving up after one attempt, so a swallowed cancel can no longer leave the browser running to `max_steps`.

**Lane 2 - the `_stop_watchdog` thread**, for the case the monitor structurally cannot handle: a synchronous block anywhere in browser-use/CDP starves the whole private event loop, so no coroutine ticks - the monitor included - and Python cannot force-kill a thread. The watchdog lives outside the loop, is armed before anything can block (startup included), and escalates:

1. every poll it schedules a cancel of the whole run task via `call_soon_threadsafe` - this lands the moment the loop ticks at all, and covers the startup phase (`_resolve_cdp_url` runs in the executor, `browser.start()` is bounded to 60 s and a failed start reports instead of degrading);
2. after a 10 s grace it restarts the `vaf-browser` container, severing the blocked socket so the loop revives and the pending cancel lands - the same answer `python_sandbox` gives (stop kills its Docker exec).

The closing awaits (`export_storage_state`, `browser.stop()`) are bounded too, so a browser that died mid-run cannot turn the stop itself into the next hang.

### Result format

The tool always returns a plain string - the VAF agent reads it and incorporates it into its response to the user.

```
Browser task completed.

Result:
1. "Show HN: I built a local-first AI framework" - 342 points
2. "Ask HN: What are you working on? (May 2026)" - 287 points
3. "Postgres 18 released" - 241 points
...
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `task` | string | Yes | - | Plain-language description of what the browser should do. Include URLs, credentials, and what data to extract. |
| `max_steps` | integer | No | 25 | Maximum number of browser actions before stopping. Cap: 100. |
| `allowed_domains` | string[] | No | unrestricted | Whitelist of domains the browser may visit. Prevents the agent from navigating outside the intended scope. |
| `persistent` | boolean | No | `false` | If `true`, cookies and login state are saved after the task and restored on the next call with the same `session` name. Use for sites that require login. |
| `session` | string | No | `"default"` | Named cookie store. Only used when `persistent=true`. Use a descriptive name like `"tipico"`, `"amazon"`, `"banking"`. Each name is independent. |

### Writing good task descriptions

Be specific. The more context the task contains, the fewer steps the agent needs.

**Too vague:**
```
"get the price"
```

**Good:**
```
"Go to https://shop.example.com/product/42 and return the current price including any active discounts"
```

**Login flow:**
```
"Navigate to https://app.example.com/login. Log in with username admin@company.com and password hunter2. 
After login, go to /settings/billing and return the current plan name and next renewal date."
```

---

## Persistent Sessions

By default every `browser_agent` call starts with a completely clean browser profile - no cookies, no login state. This is safe but means the agent must re-login on every task.

With `persistent=true`, VAF saves the browser's cookies and storage state to `~/.vaf/browser_sessions/<scope>/{session}.json` after each task and restores it at the start of the next call with the same session name. The `<scope>` segment is the caller's sanitized user_scope_id (or the local-admin scope in single-user mode) - see [Per-user session isolation](#per-user-session-isolation).

### First call - login

```json
{
  "task": "Go to tipico.de/login, log in with user@example.com / mypassword, confirm I am logged in",
  "persistent": true,
  "session": "tipico"
}
```

After this call, `~/.vaf/browser_sessions/<scope>/tipico.json` (where `<scope>` is the caller's user_scope_id) contains the session cookies.

### Subsequent calls - already logged in

```json
{
  "task": "Go to tipico.de and return my current open bets",
  "persistent": true,
  "session": "tipico"
}
```

The agent navigates directly to the page - no login step needed.

### Session file location

```
~/.vaf/browser_sessions/
└── <scope>/                 # one directory per user_scope_id
    ├── tipico.json
    ├── amazon.json
    └── banking.json
```

Each file is a Playwright `storage_state` JSON - cookies for all domains visited during the session - encrypted at rest (`VAFENC1` format, see [ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md)); a plaintext file from before the change still loads and is encrypted by the startup migration.

### Per-user session isolation

The `<scope>` directory segment keys saved sessions to a single VAF user, so saved logins and cookies are never shared across users. The scope value is resolved by this precedence:

1. an explicit caller `user_scope_id`, when one is passed;
2. otherwise the `VAF_USER_SCOPE_ID` environment variable (the child-process fallback);
3. otherwise `get_local_admin_scope_id()` (single-user / local install);
4. otherwise the literal `default`.

The resolved value is sanitized to `[A-Za-z0-9_-]` before it is used as the directory name. An operator inspecting `~/.vaf/browser_sessions/` therefore sees one opaque per-user subdirectory per scope rather than a flat list of session files.

### Security note

Session files contain login cookies - live auth tokens - and are encrypted at rest. They are stored per user under `~/.vaf/browser_sessions/<scope>/`, keyed by user_scope_id, so two VAF users sharing the same OS account cannot read or share each other's saved logins and cookies. During an agent run the state is staged as a decrypted owner-only temp file beside the store (browser_use reads and auto-saves the handed path itself) and folded back encrypted when the run ends; a temp left behind by a crash is deleted unread after an hour. OS-user file permissions on `~/.vaf/` remain an additional layer, not the only guard. Do not commit these files to version control.

---

## LLM Bridge

`browser_agent` does **not** use a separate AI service. It routes all reasoning through VAF's own LLM infrastructure - by default the same model used for everything else. The `browser_agent_provider` / `browser_agent_model` keys (Settings → AI & Model, "Browser agent model"; vision_provider pattern) give browser runs a dedicated model without changing the chat model - worth doing, because the browser loop needs strict structured output on every step and gains more from a strong NATIVE-vision model than chat does. Empty keys = ride the main provider, exactly as before.

```
browser-use internal loop
    └── await llm.ainvoke(messages, output_format=AgentOutput)
              │
              ▼
        VAFLLMBridge          (bridges async → sync)
              │
              ▼
        APIBackendManager     (VAF's existing LLM infrastructure)
              │
              ▼
        Your configured provider (Ollama / OpenAI / Anthropic / ...)
```

The bridge (`VAFLLMBridge`) implements browser-use's `BaseChatModel` protocol and delegates every LLM call to `APIBackendManager` - the same class used by all other VAF tools.

### Vision tiers

How a run gets to SEE is decided once at its start (`_browser_vision_mode`), from the
lane's model, and every tier RUNS - a setup with no vision anywhere degrades to
DOM-only work instead of failing:

| Tier | When | Behaviour |
|---|---|---|
| `native` | The lane model is vision-capable (provider registry) | `use_vision='auto'`: browser-use hands the model a screenshot tool it calls when the page needs seeing - scroll and layout decisions are spatial, and this is the biggest single jump for them. Text-only steps stay cheap. |
| `described` | Text-only lane model, but a `vision_provider` is configured | `use_vision=False`; the `describe_page_visually()` action turns a screenshot into text on demand (e.g. a CAPTCHA, an unclear layout). |
| `blind` | No vision anywhere | `use_vision=False`; the run continues on extracted DOM text, and its guidance tells it NOT to call `describe_page_visually` (it cannot answer) and to lean on `find_text` and `collect_page_text` instead. |

Every tier also receives navigation guidance (`extend_system_message`): prefer
`find_text` over blind scrolling, `scroll pages=10` for the bottom of a page, and the
VAF-added `collect_page_text` action - one step that scrolls through the whole page
(triggering lazy loading) and returns its full visible text, instead of paying a full
LLM round trip per viewport. Tiers, guidance and wiring are pinned by
`tests/test_browser_agent_vision_lane.py`.

### LLM model recommendation

browser-use requires the LLM to produce structured JSON on every reasoning step. This works well with:

| Provider | Recommended model |
|---|---|
| OpenAI | `gpt-4o` or `gpt-4o-mini` |
| Anthropic | `claude-sonnet-4-6` or newer |
| DeepSeek | `deepseek-v4-flash` |
| Local (Ollama) | `llama3.1:70b` or `qwen2.5:32b` minimum |

**Local models below ~30B parameters** frequently produce malformed JSON for complex browser tasks and will cause step failures. For production use, an API provider or a large local model is strongly recommended.

---

## Live View in WebUI

When `browser_agent` is running, the **SubAgent Window** in the WebUI opens automatically and shows a live browser view: the streamed Chromium itself on top (the same KasmVNC stream the interactive lane uses, watch-only), and the run's dock below (task, action plan, visited URLs, activity).

**The viewport is the real browser.** At run start the in-process lane grants the run's own chat session a WATCH-ONLY stream ticket (`agent_stream_started` in `vaf/core/browser_interactive.py`): the window's iframe loads the same viewer document as the interactive lane, with the viewer's `view_only` setting in the URL and pointer events off on top of it, so the person sees Chromium's own tab strip and omnibox exactly as the agent drives them - but cannot type into a browser the agent is using. Watch-only is enforced at the RELAY, not in the page: both of those live in the browser, so the stream proxy drops everything travelling from client to container on an agent grant (the RFB protocol needs no client frames to deliver a picture). Pinned by `tests/test_browser_pool.py`. The grant is emitted only to the session that owns the run (the ticket is the capability; a foreign user must not watch someone else's agent browse), and the ticket dies with the run.

**Screenshots stay.** The ~1.5s JPEG screenshot loop keeps running regardless - it feeds vision, the workflow tile, and the fallback view for the lanes without a stream grant (a spawned child validates tickets against the wrong process, so the child lane deliberately makes no grant and keeps the rebuilt chrome bar over screenshots). While the stream draws its first picture, the latest screenshot doubles as the connecting cover.

- **Live indicator** - red pulsing dot disappears when the task ends
- **Dock** - task, actions, history and activity below the viewport
- **Handover veil** - a change of hands (person to agent, agent back to person) plays a short full-viewport veil, which also covers the stream's ticket swap and reconnect

The viewport is visible in both **dock mode** (right side panel) and **overlay mode** (full-screen modal, triggered by clicking the SubAgent bubble in the chat).

### Interactive browser: driving the sandbox by hand

The globe button at the left of the chat header's rail (in the `h-16` band that puts it on the sidebar logo's optical line) opens the
browser window and, when no agent run holds the browser, asks the server for the
INTERACTIVE stream (`browser_interactive_start`). The window then shows the container's
Chromium fullscreen through a KasmVNC WebSocket stream - Chromium's own tabs, omnibox and
navigation, at up to 60 fps, with mouse and keyboard carried natively by the VNC protocol.
Nothing about this starts `browser_agent`: it is the same shared browser, driven by a
person instead of the model. Closing the window (or clicking the marked globe again) sends
`browser_interactive_stop`.

**The stream path.** The KasmVNC client is loaded from the container THROUGH the VAF web
server (`/api/browser-vnc/t/<ticket>/...`), never directly: the container port
(127.0.0.1:6901, loopback-only like CDP) is reachable for LAN users only via that proxy.
The ticket in the path is the credential, validated against the current lease on every
request; a stopped or superseded lease kills its ticket immediately. The two halves of
the stream carry that ticket differently, and it is worth knowing which is which: ASSETS
resolve relative to the client's own directory and take the ticket along by themselves,
but the SOCKET url is built from the client's settings (`ws://<host>:<port>/` + the
`path` setting, default `websockify`) and would otherwise dial a ticketless
`/websockify` that does not exist on the VAF server. Those settings are overridable per
URL (the client reads the hash first, then the query), so the stream URL carries
`?path=api/browser-vnc/t/<ticket>/websockify` and the client dials the proxy route.
`host` and `port` are appended by the frontend from the app's own backend-socket helper
(the dev server cannot proxy websockets at all, so the socket needs an address of its
own), while `encrypt` stays default: the client derives ws/wss from the page's protocol
exactly as that helper does.

**How the streamed browser is launched**, because four of these settings look cosmetic
and are not:

- **The browser keeps its OWN window** - tab strip, toolbar, bookmarks, downloads -
  dressed dark with `--force-dark-mode` (which themes the BROWSER, not page content, so
  sites still render as their authors meant). An earlier version hid all of it with app
  mode so the web UI could draw that chrome itself; it cost more than it bought. A
  middle-click into a new tab opened a second, ordinary window inside the streamed one,
  and every browser feature living in the toolbar was simply gone. Not `--kiosk` either:
  it hides the UI and also disables the right-click context menu, which is a feature
  here rather than decoration.
- **Chromium runs WITH its own sandbox.** The historic blocker was Docker's DEFAULT
  seccomp profile, which denies unprivileged user namespaces (`clone(CLONE_NEWUSER)`
  fails with EPERM, "Failed to move to new namespace ... Operation not permitted") -
  not the container itself. VAF starts the container with
  `docker/browser/chromium-seccomp.json`, Docker's default profile plus one allow rule
  for `clone`/`clone3`/`unshare`/`setns`, so the namespace sandbox works with **no
  added capability** (measured 2026-08-26: renderers run in their own user and PID
  namespaces). The entrypoint probes for user namespaces at startup (`unshare -U`);
  a runtime that does not apply the profile gets a loud warning and a `--no-sandbox`
  fallback, because an unsandboxed browser still beats a crash-looping one.
- **`--test-type` stays, sandbox or not.** Chromium's yellow "unsupported
  command-line flag ... Stability and security will suffer" infobar fires for ANY
  non-standard flag, not only `--no-sandbox`: dropping the suppressor together with
  `--no-sandbox` brought the bar straight back, now naming the load-bearing anti-bot
  flag `--disable-blink-features=AutomationControlled` (measured live). The wording is
  Chromium boilerplate about non-standard flags, not a statement about this setup.
  `--test-type` is not visible to pages: `navigator.webdriver` stays false. The cost of
  the suppressor is that it would ALSO silence the warning for a genuinely dangerous
  flag, so the launch line carries a guard-test denylist (`--disable-web-security`,
  `--disable-site-isolation-trials` and friends may never appear in the entrypoint) -
  the alarm lives in CI instead of the browser chrome. Both container lanes also run
  under docker-init (`init: true` / `--init`): crashed Chromium re-parents orphans
  onto PID 1, which the supervisor script cannot reap.
- **A window manager** (matchbox, ~300 KB) keeps the window at the size of the display,
  which changes whenever a viewer asks for its own geometry.
- **No session restore.** The supervisor kills Chromium with SIGKILL, which marks the
  profile as crashed, and a crashed profile reopens the windows it had - including any
  ordinary window that ever appeared, which then shows a full browser UI inside the
  streamed window and never leaves. The flag suppresses the bubble; the `exit_type`
  edit clears the mark that drives the restore.

Idle parking follows from the same fact: it EMPTIES the window by navigating it to
`about:blank` rather than creating a fresh tab and closing the rest, because a tab made
through CDP is an ordinary window. The old version closed the app window along with the
page (its `data:` URL is not `about:blank`), which is how re-opening the browser showed
a browser inside the browser.

**Three things this lane needs that are easy to miss**, each of which produced a window
that loaded and then showed nothing:

- **The viewer document is loaded by a RELATIVE url**, i.e. same-origin, so it rides
  whichever front door the person is on. An absolute `http://<backend>` url is wrong the
  moment LAN hosting with TLS is on, because the backend port is HTTPS-only then and a
  plain HTTP request to it returns nothing at all.
- **`X-Frame-Options` must be `SAMEORIGIN` for this path.** The server sets `DENY` on
  every other response; under `DENY` the browser fetches the viewer (a clean 200 in the
  access log) and then refuses to paint it in the window. The exemption is one prefix
  wide (`/api/browser-vnc/`) and same-origin only, so no foreign page can embed it.
- **The HTTPS proxy needs a `WebSocketRoute` for the stream path, not just an allowlist
  entry.** The path starts with `/api`, so the proxy's catch-all API route looks like it
  covers it; that route matches HTTP scopes only, and the upgrade comes back as HTTP 403
  with the backend never reached. The relay also had to learn BINARY frames - it read
  text only, which was invisible while the WebUI's JSON socket was its only client and
  fatal for a VNC stream.

**The lease.** One person drives at a time (`vaf/core/browser_interactive.py`): a second
window of the same user takes over (the older one is told "superseded"), a different user
gets "busy" without learning who holds the browser, and an admin may evict. A lease whose
viewer disappears is released after a 120s grace period, and the browser is parked back to
one blank tab - the same idle rule the agent lane uses, for the same measured 1027%-CPU
reason.

**The agent always wins - but only borrows.** `BrowserAgentTool.run()` evicts any
interactive lease at its very start (for both the in-process and the spawned-child lane,
since the hook runs before the spawn branch), and an interactive start is refused with
`agent_active` while a run is underway - the in-process flag covers the first, an IPC
scan for a live `browser_agent` task covers the second.

**A takeover of a live session is a HANDOVER, not a fresh start.** When the eviction
found a person driving, the page they were on is captured (`_current_page`, HTTP target
list, best-effort) and handed to the run of the same scope, exactly once and with a
short TTL: the run's task gains the instruction to CONTINUE there - their tab is still
open, their logins are live - and the clean-start scrub of a `persistent=false` run
stands down for such a continuation, because it would log out the very session it was
handed (measured live: asked to take over an open marketplace session, the run opened
the site fresh in its own tab instead). A person re-opening the browser themselves
outdates the pending handover. The per-turn browser context block teaches the model to
phrase takeover tasks as continuations; pinned by `tests/test_browser_interactive.py`.

**And the way back mirrors it.** However the run ends - finished, failed, or stopped by
the person - the same finally path fires the give-back, and the end-of-run parking
STANDS DOWN while that give-back is pending (`give_back_pending`): the person's window
re-enters the interactive mode on the browser exactly as the agent left it, tabs and
all, with the handover veil marking the change of hands. The CPU concern parking exists
for is not dropped: `agent_run_ended` arms a one-shot fallback that parks the browser
only if nobody claims it within a short grace (window closed mid-run, resume refused). The refusal is not blind for the
run's own session: it carries the run's watch-only stream, so opening the window during a
run shows the live browser rather than nothing. The window flips to the agent view (live
stream or screenshots, plus task, actions, history, activity) the moment agent data
streams. The eviction is remembered server-side (`_pre_agent_holder`): when the run ends,
the evicted holder's session receives the stop event with `resumable: true` and its
window re-enters the interactive mode by itself, with a short handover veil in each
direction - the agent must not close a browser window the person opened. A run that
evicted nobody ends as before: the window auto-closes a few seconds after completion.
The give-back is consumed on first use, and a lease the person starts by hand clears any
pending one.

**Logins persist, without a switch.** Interactive use is always the PERSISTENT mode - the
counterpart of the agent's `persistent=true` runs, and deliberately without a toggle of its
own: whether a login gets remembered is decided where the person expects that question, in
the browser itself, which asks. The person's cookies are loaded into the browser at lease
start and exported at lease end - to the SAME per-scope storage-state file the agent's
persistent sessions use (`~/.vaf/browser_sessions/<scope>/default.json`, Playwright shape,
cookies only), so a login performed by hand is a login the agent has on its next
`persistent=true` run, and vice versa. The forget-everything mode exists only on the agent
lane (`persistent=false` runs). On a handover to a DIFFERENT user scope the shared browser
gets a VERIFIED whole-profile wipe first - cookies, every site's stored state, history,
Chromium-saved passwords, autofill and downloads all die with the profile, and the wipe
is confirmed (marker consumed by the container's supervisor) before anyone gets the
browser. The same rule fires when the jar owner is unknown (fresh server process). The
handover is fail-closed: a wipe that cannot be confirmed refuses the lease (or the run)
with a `browser_handover_failed` security event instead of proceeding over a stranger's
state (see [USER_ISOLATION.md](../security/USER_ISOLATION.md)).

**The chat knows where you are.** While a chat's window drives the browser, that chat's
messages travel WITH browser context: the current page URL and any selected text are
injected into the turn (the code-viewer pattern - stored by `web_server.py` at send time,
injected and cleared per turn by `headless_runner.py`), and a screenshot of the current
view rides the existing attached-images lane, so it appears under the message like an
upload and reaches the model as vision input. Captured server-side at send time via a
short CDP snapshot (`snapshot_context` in `vaf/core/browser_interactive.py`), and only
for the chat session that holds the lease - other sessions get nothing. A small "Browser"
chip next to the workspace chip says the context is riding along.

**The connecting cover, and why it waits for pixels.** The stream viewer brings its own
branded loading screen, and the window covers it with its own until the picture is up -
covering rather than patching the vendor client, which is what keeps the licence
boundary above intact. The trigger is the subtlety: an open socket is NOT a picture. The
viewer opens its websocket immediately and then shows that splash for the whole protocol
handshake, so lifting the cover on the accept revealed the splash instead of hiding it.
The server therefore counts bytes relayed from the display server and reports the
crossing once (`stream_bytes` in `vaf/core/browser_interactive.py`). The count is reset
whenever a stream is HANDED OUT, not only when the last viewer disconnects: a lease can
survive a close (same person, same chat, so `start` reuses it) and came back carrying
the previous viewer's count, which reported "picture is up" before a pixel had arrived
and left the foreign splash visible on every open after the first. The threshold is
measured, not guessed: the entire RFB handshake is 45 bytes (greeting 12, security types
2, result 4, ServerInit 27) and the first framebuffer update is 49132, so 4 KB cannot be
reached by handshake traffic and cannot be missed by a real frame. **Who decides that the picture is there.** The viewer itself, when it has ever spoken:
it calls `parent.postMessage({action:"connection_state", value})` on every visual state
change, with `connected` at the moment it takes its own splash down, and the window
listens for that (same-origin frame, and only messages from ITS iframe are accepted).
The server's byte count is the FALLBACK, for the case where that message never arrives,
because their postMessage contract is not ours to rely on. The reason the viewer has the
last word: the server sees the picture arrive on the wire before the viewer has decoded
and drawn it, so a cover lifted on the server's signal alone shows the splash for the
length of that gap - a brief flash that survived every earlier fix. Whatever the last
viewer said is forgotten when a stream starts or ends, because closing and re-opening
can hand back the same ticket, and a stale `connected` would suppress the cover for
exactly the moment the splash is on screen.

The window's own rule is
deliberately blunt: the cover is up whenever there is no picture, not only for the first
one. An earlier version covered the first picture per stream, to avoid blanking a page
somebody was reading - but there is nothing to blank, because when the stream is down
the viewer has already replaced the page with its own white splash. Every reconnect is
one of those moments, and that is why the splash kept flashing briefly. The cover is
also SILENT: a plain dark surface, with its spinner and wording fading in only after
three seconds (pure CSS, no timer), because a label that appears for a second and
vanishes is just a splash of our own - which is exactly what it was mistaken for.

The stream viewer's splash cannot be switched off from the outside, and that was checked
rather than assumed: its 44 URL settings (`initSetting` in the client bundle) contain
nothing for it, `Xkasmvnc` has no such option and the server's YAML no such key. It is
two things in its stylesheet - `#noVNC_transition` with a hard `background: #fff` plus an
embedded logo, and a `body` background of `#fff url(./splash-*.jpg)` - and neither its
script nor its stylesheet mentions `prefers-color-scheme`, so it is always white. A
configurable "Splash Background" exists only in the commercial Kasm Workspaces product,
not in this server. Covering it is therefore the only route that does not patch the
vendor client, which the licence boundary above rules out.

With no run behind it and no interactive stream, the window shows an honest empty state
("No browser session yet") instead of a starting banner; `presence` separates a hand-opened
window from a run whose data is on its way. While another sub-agent is actually RUNNING,
the globe stands down and says so. The button reports "open", not "on top": every viewer
and editor keeps its priority over the sub-agent window, and the button deliberately does
not close them to get the slot - closing the document panel detaches the chat's documents
on the server, which a browser click must never do.

### How frames reach the UI (subprocess bridge)

The browser agent runs as its own **child process** (in workflows, and via `subagent run`). That
process has no local WebSocket clients, so `web_interface.emit_browser_frame()` / `emit_browser_step()`
**bridge each frame over HTTP** (off-thread, non-blocking) to the main process's
`POST /api/subagent/stream` whenever `VAF_IN_SUBAGENT_TERMINAL=1`. The generic endpoint then
broadcasts a `browser_frame_update` to the session's WebSocket, which the WebUI already handles. Both
methods reach that decision through the single `_bridge_or_push()` fork shared by every live-view
emitter; the lane is re-read from `VAF_IN_SUBAGENT_TERMINAL` on **every** event and never cached,
because the workflow CLI sets and the headless runner clears that variable inside a live process. So
the path is: child screenshot loop → HTTP → main process → WebSocket → `subAgentState.browserFrame`.

### Inside a workflow: tiled live view

When the browser runs as a **workflow step**, the SubAgent dock is normally suppressed (its output
goes to the Workflow Runtime terminal). Frames are visual, though, so they are shown in a dedicated
`BrowserLiveTile` **docked to the left of the Workflow Runtime window** (side by side, not
overlapping). Standalone (outside a workflow) the browser view still renders in the SubAgent dock.
See [Window Tiling](../web-ui/WINDOW_TILING_DESIGN.md) and [Workflow UI Components](../web-ui/WORKFLOW_UI_COMPONENTS.md).

---

## Security

### The security model, in layers

A malicious page has to cross four independent layers before it reaches anything
that matters; each layer exists on its own evidence and is pinned by its own tests:

| Layer | What stands there | Where it is enforced |
|---|---|---|
| 1. Chromium's own sandbox | The page's renderer runs in its own user and PID namespace with Chromium's per-process seccomp-bpf filter (the layer-1/layer-2 design of [Chromium's sandbox documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/linux/sandboxing.md)). Verified by namespace comparison of live renderer processes. | `docker/browser/chromium-seccomp.json` makes it possible; entrypoint probe |
| 2. Hardened container | `cap_drop ALL` + `SYS_CHROOT` only, `no-new-privileges`, custom seccomp (Docker's default plus the user-namespace syscalls), non-root user, docker-init. A renderer escape lands here with no capabilities to use. | compose service + pool `docker run`, pinned by `tests/test_browser_entrypoint_supervise.py` and `tests/test_browser_pool.py` |
| 3. Network partition | Loopback-only published ports, per-lease stream tickets through the VAF server, an isolated bridge network with no route to the database, one PRIVATE network per pooled instance, filtering DNS (1.1.1.2 + DoH) and uBlock Origin Lite. | compose + `browser_pool.py` |
| 4. Per-user regime | A dedicated instance per user (pool, on by default) with its own profile volume; on the shared fallback a VERIFIED profile wipe on every change of hands, fail-closed, serialized; saved logins encrypted at rest (`VAFENC1`). | `browser_interactive.py`, `data_files.py` |

Cross-cutting: every degradation is VISIBLE. A pool fallback, a refused handover and
an overdue browser engine each raise a security event
(`browser_pool_fallback`, `browser_handover_failed`, `browser_image_stale`) that the
security dashboard shows, and the launch line carries a guard-test denylist of
security-disabling flags because `--test-type` suppresses Chromium's own warning bar
for every flag alike.

### Fitness for sensitive accounts (banking, mail, bookkeeping)

The layers above are what makes interactive logins to sensitive services a supported
use. Three conditions frame it, and they are boundaries of the design, not bugs:

1. **The HOST is the trust boundary.** CDP on `127.0.0.1:9222` carries no
   authentication (see Network isolation below): anyone who can run a process on the
   machine can drive the logged-in browser. Sensitive accounts belong on machines
   whose local users are all trusted.
2. **Multi-user deployments should set `browser_pool_strict`** (and size
   `browser_pool_max` to the user count), so no two users' sessions can ever meet on
   the shared fallback - the refusal is then a visible busy, never silent sharing.
3. **Interactive use stays local; agent runs do not.** A `browser_agent` run over a
   logged-in session sends page text (and, with vision, screenshots) to the
   configured LLM provider - that is how the agent works. Type sensitive sessions by
   hand in the interactive browser, or accept that exposure deliberately.

Safe Browsing's list refresh is disabled with the rest of Chromium's background
networking; the malware/phishing screen comes from the filtering DNS resolver and
the content blocker instead (see Content blocking and DNS filtering below).

### Permission level: `write`

`browser_agent` is classified as a `write` tool - it can navigate, click, and submit forms, but does not require a separate destructive-action confirmation gate. Actions visible in the live view give the user real-time oversight of what the agent is doing.

### Network isolation

The CDP port (`9222`) is bound to `127.0.0.1` only - it is **never exposed** to the network or other machines. The trust boundary is the HOST: CDP itself carries no authentication, so any process or OS account on the machine that can dial `127.0.0.1:9222` has full control of the logged-in browser. On a single-tenant machine that is the owner; a multi-account host must treat local users as inside the boundary. This is a named boundary, not an oversight.

The `vaf-browser` container runs on its own isolated Docker network (`vaf-browser-network`) and is **not** on `vaf-network`. This means the browser container cannot reach `postgres` or `redis` by hostname - a compromised browser (e.g. via SSRF or a malicious page) has no direct path to VAF's database.

### The stream port is the weaker twin of the CDP port

Named because it is measured, not because it is comfortable. KasmVNC on `6901` runs with
`-disableBasicAuth -SecurityTypes None` (see `docker/browser/entrypoint.sh`) and is published on
`127.0.0.1` exactly like CDP - but the two are NOT equally defended, and the difference runs the
wrong way:

- **CDP refuses a browser page.** Measured: its WebSocket endpoint answers `403` to any request
  carrying an `Origin` header at all, `/json/*` sends no CORS header, and arbitrary DNS names are
  rejected in the `Host` header. A page in the user's ordinary browser cannot drive it.
- **The stream accepts one.** KasmVNC requires an `Origin` header to be PRESENT and then accepts
  any value. A page's WebSocket API always sends `Origin` and cannot suppress it, so a page open
  in the user's normal browser can complete the handshake against `ws://127.0.0.1:6901` and get a
  bidirectional RFB channel: framebuffer out, `KeyEvent`/`PointerEvent` in. That is read AND
  control of whatever session is logged in.
- **Both are open to any local process** under the host trust boundary described above. For the
  stream that is the same class as CDP; the page vector is the part CDP does not have.

The loopback publish is what keeps the LAN out, and the ticketed proxy
(`/api/browser-vnc/t/<ticket>/`) is what gates the intended path - neither is an access control on
the port itself. Until this is closed, treat a machine that browses the ordinary web in another
browser while a sensitive session is open in the sandbox browser as a machine where those two can
meet.

### Container hardening

Both container lanes - the compose service and the per-user pool's `docker run` - start the browser with the same hardening, pinned against each other by `tests/test_browser_entrypoint_supervise.py` and `tests/test_browser_pool.py`:

- `cap_drop: ALL` plus `cap_add: SYS_CHROOT` (Chromium's zygote chroots its sandboxed children; measured to fail without it),
- `no-new-privileges:true`,
- `seccomp=docker/browser/chromium-seccomp.json` - Docker's default profile plus the user-namespace syscalls, which is what enables Chromium's own sandbox (see the launch-flags section above).

A renderer exploit therefore lands inside Chromium's namespace sandbox first, and only then inside a capability-stripped container. The profile is read by the docker CLIENT (compose resolves it relative to the repo root; the pool passes an absolute path), so it ships in the checkout, not in the image.

Deliberate: Chromium's password manager is pinned ON by managed policy (`PasswordManagerEnabled` in `/etc/chromium/policies/managed/vaf-security.json`, written by the Dockerfile), together with address autofill (`AutofillAddressEnabled`). It is what lets an AGENT run carry a session on: VAF's own per-scope cookie store restores the SESSION, but when a cookie has expired and a login form appears, only a saved credential gets past it. As a profile preference this was fragile twice over - a stray toggle in the browser's settings silenced it (measured: the shared container's profile carried `credentials_enable_service:false` while the image default is on), and every cross-user handover wipes the profile and its preferences with it. A managed policy survives both. Two prices, both named: without a keyring in the container Chromium falls back to its `basic` password store, which is weak at rest, so saved passwords belong on a DEDICATED per-user instance whose profile volume is that one person's, never on the shared fallback (where the handover wipe deletes them at every change of hands anyway). And `AutofillCreditCardEnabled` is deliberately left OFF: a browser an agent can drive should not be able to auto-fill payment data into whatever form it opens.

### Content blocking and DNS filtering

The browser ships hardened against the web it browses, for the person and the agent alike:

- **uBlock Origin Lite** (GPL-3.0) is baked into the image and installs itself into the profile on browser startup. It blocks ads, trackers and malicious ad payloads via MV3 declarativeNetRequest rules - filtering happens inside the browser's network stack, no extension process reads page content. The artifact is the official, unmodified release zip from the project's GitHub releases (not scraped from the Chrome Web Store, whose terms do not cover automated downloads), version-pinned with a verified checksum in `docker/browser/Dockerfile`. The install lane is the one Linux distros use (a CRX packed at image build plus a descriptor in `/usr/share/chromium/extensions`), because Chromium 150 silently ignores `--load-extension`; the Dockerfile documents the mechanism, and the entrypoint must never reintroduce `--disable-default-apps` or `--disable-extensions`, each of which kills this lane (all three facts measured in the container and pinned by `tests/test_browser_entrypoint_supervise.py`). To update the blocker, bump `UBOL_VERSION`/`UBOL_SHA256` and rebuild. The same local-build licence boundary as KasmVNC applies: publishing a prebuilt image would make Veyllo the distributor of this GPL binary too.
- **Cloudflare security DNS** (the 1.1.1.2 malware/phishing-blocking resolver) is wired in twice: as Chromium's DNS-over-HTTPS resolver via a managed policy (`DnsOverHttpsMode: automatic` + the `security.cloudflare-dns.com` template, `/etc/chromium/policies/managed/vaf-dns.json`), and as the container's plain-DNS upstream in `docker-compose.memory.yml` - so filtered resolution holds even on networks that block DoH, and encrypted resolution is used wherever it is reachable. Deliberately NOT the 1.1.1.3 family variant: malware filtering is wanted, content censorship is not. Both halves are pinned by `tests/test_browser_entrypoint_supervise.py`.

- **Safe Browsing: wired, and honest about when it works.** The browser now carries what Safe Browsing needs - Chromium's API keys reach the process (the entrypoint launches the binary directly and therefore has to source `/etc/chromium.d/apikeys` itself, which the Debian wrapper would otherwise do), the two flags that killed it are gone (`--safebrowsing-disable-auto-update` and `--disable-background-networking`, the latter also carried the component updater's CRLSets, i.e. certificate revocation), and `SafeBrowsingProtectionLevel: 1` is pinned by managed policy so no profile state and no profile wipe can lose it. Standard protection sends URL hash PREFIXES, never full URLs; level 2 (Enhanced) is deliberately not set. MEASURED LIMIT: with Debian's shared API key the list update at `safebrowsing.googleapis.com/v4/threatListUpdates` is refused, no list store is ever written, and Google's own phishing test page loads without an interstitial - that key is assigned "for access to their services like sync and gmail", not for Safe Browsing. Set `VAF_BROWSER_GOOGLE_API_KEY` to your own Google API key to activate it; both container lanes pass it through, and the container log says on every start which of the two states it is in. Until then the phishing defence is the filtering DNS resolver plus the content blocker.

Both changes live in the image and need a rebuild to take effect (see [Rebuild after Dockerfile or entrypoint changes](#rebuild-after-dockerfile-or-entrypoint-changes)).

### Domain restriction

Use `allowed_domains` whenever the task scope is known:

```json
{
  "task": "Extract the pricing table from the plans page",
  "allowed_domains": ["yourproduct.com"]
}
```

This prevents the browser agent from following redirects or links to unintended external sites.

### Chat channel restrictions

By default, `browser_agent` is **blocked** on Telegram, WhatsApp, and Discord channels: these channels have no interactive confirmation flow, so channel-restricted tools cannot run there.

This is controlled by the `channel_tools_unrestricted` setting (Settings → Advanced, default **on**). When enabled, messaging-channel sessions get the same tools as the main agent - including `browser_agent` - and run **without** the per-call confirmation gate. The channel whitelist (`paired_only` by default) and the per-user `admin_only` checks still apply; turn it off to restrict channel sessions to non-channel-restricted tools.

---

## Configuration

### Concurrency / Multi-user

This section is about the SHARED browser container. Two people do not normally meet
there any more: the per-user pool below is on by default and gives each of them a
browser of its own. The shared container is what remains for everyone the pool cannot
serve, and everything here describes that fallback.

On it, `browser_agent` serialises all calls - only **one** browser session runs at a time. If a second user (or a second concurrent workflow) triggers `browser_agent` while a session is already running, the call waits in a queue for up to **120 seconds** before giving up with a "Browser agent is busy" message.

This avoids memory exhaustion and tab interference in the shared Chromium container.

To allow **2 parallel sessions** on the shared container, on a machine with sufficient RAM (≥ 8 GB):

```bash
# System environment before starting VAF (see "Where these settings go" below)
VAF_BROWSER_MAX_PARALLEL=2
```

The practical limits are:

| `VAF_BROWSER_MAX_PARALLEL` | Recommended host RAM | Notes |
|---|---|---|
| `1` (default) | 4 GB+ | Safe for single-user or small teams |
| `2` | 8 GB+ | Handles two concurrent users |
| `3+` | 16 GB+ | Not recommended; Chromium memory adds up fast |

---

### Per-user browser pool (parallel use)

**On by default.** Up to two people at a time get a browser container of their own;
everyone beyond that shares the fallback container described above. Four config keys
govern it, all admin-only:

| Config key | Default | What it decides |
|---|---|---|
| `browser_pool_max` | `2` | How many per-user browsers may run at once. `0` switches the pool off. |
| `browser_pool_min_free_mb` | `2500` | Free-memory floor; below it no NEW instance is started. |
| `browser_pool_idle_seconds` | `900` | When an unused instance is stopped to give its RAM back (data kept). |
| `browser_pool_strict` | `False` | Strict mode: no dedicated instance means BUSY, never the shared fallback. |

**A personal browser follows the image.** A container is pinned for life to the image ID
it was created from, so without this the per-user browsers - the ones people actually work
in - would be the LAST to receive a Chromium security fix, while the shared browser is
rebuilt by the stack start and by the age gate above. Before an instance is adopted, its
image ID is compared against the one the shared container runs; on a mismatch the
container is removed and recreated on the current image. The profile volume is named and
remounted, so history, logins and bookmarks come back with it - only the stale binary is
thrown away.

**Fallbacks are visible, and strict mode refuses them.** With the pool active, every
resolution that ends on the shared container (capacity, memory floor, docker trouble)
is recorded as a `browser_pool_fallback` security event. With `browser_pool_strict`
on, the same situation refuses instead: the interactive browser answers busy, an
agent run returns a clear "no dedicated browser available" error, and `render_check`
reports the refusal - no lane quietly shares. A pool set to `0` is exempt on both
counts: sharing IS the configuration then. For a company where two users' sessions
must never meet (online banking, bookkeeping), strict mode plus a `browser_pool_max`
sized to the user count is the intended setup.

#### Raising the number of parallel browsers

Budget about **2 GB of RAM per concurrently active user** - that is the container's own
memory cap - and leave the free-memory floor beneath what the machine still has free.
A 32 GB machine comfortably carries 4 to 6; a 8 GB machine should stay at 2.

Three ways to change it, in the order most people will use them:

1. **Settings > Advanced > Browser pool** (admin only). Set "Parallel browsers" to the
   number you want and save. It takes effect for the next browser that is opened; no
   restart, and browsers already running are left alone.
2. **`~/.vaf/config.json`** - the same keys, if you would rather edit the file:
   ```json
   {
     "browser_pool_max": 4,
     "browser_pool_min_free_mb": 2500,
     "browser_pool_idle_seconds": 900
   }
   ```
   Save while VAF is stopped, or use the Settings UI, which writes the file safely under
   a lock.
3. **Environment variables**, for a deployment that pins the value outside the config
   file. They OVERRIDE the config keys, so a machine can be capped no matter what an
   admin sets in the UI:
   ```bash
   # System environment of the process that starts VAF (see below)
   VAF_BROWSER_POOL_MAX=4
   VAF_BROWSER_POOL_MIN_FREE_MB=2500
   VAF_BROWSER_POOL_IDLE_S=900
   ```

**If raising the number appears to do nothing**, the free-memory floor is the usual
reason: an instance is only created while at least `browser_pool_min_free_mb` is free,
and otherwise the caller quietly falls back to the shared browser. The decision is
logged - look for `[browser_pool] at capacity` or `[browser_pool] low memory` in the
`webui` domain log. To switch the pool off entirely, set `browser_pool_max` to `0`.

#### Where these settings go

The VAF server reads its environment from the process that starts it - a shell, a
systemd unit, a service wrapper. It does **not** read a `.env` file; nothing in the
Python runtime loads one. `~/.vaf/compose.env` is passed to `docker compose` only, so
variables placed there reach the containers, never the server. Anything a config key
covers is better set through Settings or `config.json`, which need no restart of the
shell that launched VAF.

Each user scope gets a browser CONTAINER of their own
(`vaf/core/browser_pool.py`): their own profile volume - history, browser-saved
passwords and downloads become legitimately per-user instead of state the handover
scrub has to wipe (a dedicated instance is never scrubbed and never profile-wiped) -
their own CDP and stream endpoints, and therefore PARALLEL use: two users browse (or
run `browser_agent`) at the same time in two different browsers, and "busy" between
users disappears. Instances are cloned from the shared container's image, published
loopback-only on ephemeral ports, and named by a scope hash so a container listing
never says who uses the machine. The stream ticket names the instance: the VNC proxy
routes each window to the browser that issued its ticket.

**Each instance gets a network of its own**, never the shared browser's. Inside the
container Chromium's CDP proxy listens on `0.0.0.0:9222` and KasmVNC on `0.0.0.0:6901`
with authentication deliberately off - safe only because the host publishes them on
loopback and the VAF server is the only door. On a shared bridge network that stops
being true between containers: a page in one user's browser could dial another user's
container IP and drive it. A per-instance network leaves each browser with no peer at
all. The filtering DNS resolvers compose gives the shared container are passed to each
instance too, so the DNS hardening applies there as well.

The pool only ever ADDS isolation: whenever it cannot serve - switched off
(`browser_pool_max` at `0`), at capacity, free memory below the floor, docker
unreachable, the shared container missing as the image template, or no user scope on
the call - the caller falls back to the shared container and its handover scrub. That
fallback is also where the named residual of the quick scrub still applies, so a
machine that runs the pool at capacity is not uniformly partitioned; see
[USER_ISOLATION.md](../security/USER_ISOLATION.md). An idle instance is stopped (never
removed: the container and its profile volume stay) and wakes on the next use; a
stopped instance keeps its port mapping, so endpoints stay stable. Budget roughly
1-2 GB RAM per concurrently active user. Needs the same docker CLI access the stop
watchdog already uses.

Named boundary: a spawned `browser_agent` child resolves the caller's instance
itself (the parent created it before the spawn), but the interactive lane's
IPC-based "agent run underway" scan cannot attribute a spawned run to one instance;
that scan keeps guarding whichever manager it runs on, which can refuse an
interactive start conservatively during workflow browser runs.

### Downloads

```bash
# System environment of the process that starts VAF (never a .env file:
# nothing in the Python runtime loads one)
VAF_BROWSER_DOWNLOADS=workspace   # default; "off" denies downloading outright
```

Files downloaded inside the sandbox browser - by the person or by an agent run - never
stay in the container (a folder nobody can reach; "it downloaded, but where?" was a live
question). In `workspace` mode a sweep drains every FINISHED download (Chromium keeps
in-progress files as `*.crdownload`): while a person is driving, the lease janitor
delivers within a few seconds; a lease end, a takeover and an agent run's end drain the
rest. Each file passes the same threat funnel every ingress lane asks
(`inspect_upload_file`, origin `browser_download` - a download is foreign bytes like any
upload; a blocked file is deleted and surfaces in the security dashboard) and lands
under the owner's own file area, `VAF_Projects/<uid8>/Downloads/`, with a sanitized,
collision-suffixed name and a 512 MB size cap. Delivered or blocked, the container copy
is deleted - the folder is a hand-off point, not storage.

Ownership follows the jar: downloads go to whoever holds (or last held) the browser,
and a scope CHANGE purges the folder unread - a previous holder's undelivered files
must never ride into the next person's workspace. `off` is enforced in the browser
itself (`Browser.setDownloadBehavior deny` at every handover), not in our chrome.
Pinned by `tests/test_browser_interactive.py`.

### Uploads: the workspace, mirrored in

```bash
# System environment of the process that starts VAF (never a .env file:
# nothing in the Python runtime loads one)
VAF_BROWSER_WORKSPACE_SYNC=on   # default; "off" mirrors nothing
```

The reverse of the download sweep, for the same reason: a website's file picker (and
browser-use's `upload_file` action) can only see the CONTAINER filesystem, so
"upload my PDF" is impossible unless the file exists in there. The holder's file area
(`VAF_Projects/<uid8>/`) is mirrored one-way into `/home/browser/Workspace` - at lease
start, on every janitor tick (signature-gated: an unchanged workspace costs a
directory walk and nothing else, so a file the agent just produced becomes uploadable
within seconds), and at an agent run's start. Hidden files stay out, single files over
64 MB stay out, and the mirror stops at 512 MB total - it is a convenience, not a
backup, and the host always wins.

The container's file dialog is ANCHORED to it: the entrypoint sets the XDG Documents
dir, a GTK sidebar bookmark and the picker's start directory to the Workspace folder -
the only place in the container that holds anything of the person's. Agent runs get
the mirrored paths as browser-use's `available_file_paths` whitelist, so `upload_file`
can attach exactly the owner's files and nothing else. On a scope change the mirror is
purged unread together with the downloads folder, and the full profile scrub wipes it
too. Pinned by `tests/test_browser_interactive.py` and the entrypoint guard.

### Handover scrub depth

**A change of hands is not configurable.** When the shared browser passes to a DIFFERENT
user scope (a foreign interactive lease or agent run, or an unknown jar after a server
restart), the whole Chromium profile is wiped - history, Chromium-saved passwords,
autofill, bookmarks, downloads, the HTTP disk cache, the NSS certificate and key
database, and every site's stored state - via a marker
the container's supervisor consumes between Chromium launches, and VAF VERIFIES that
consumption before granting the browser (cost: one Chromium relaunch, ~3-5 s). The
handover is fail-closed: a wipe that cannot be confirmed refuses the lease or the run,
logs a `browser_handover_failed` security event, and leaves the previous state guarded.
A switch that could turn this off would not be a setting but a defect, so there is none.

What remains configurable is the SAME-SCOPE clean start (a non-persistent
`browser_agent` run of the same user - a clean start is that lane's documented promise):

```bash
# System environment of the process that starts VAF (never a .env file:
# nothing in the Python runtime loads one)
VAF_BROWSER_SCRUB=quick   # default; "full" deepens same-scope clean starts
```

| Mode | Cleared on a same-scope clean start | Cost |
|---|---|---|
| `quick` (default) | Cookies, plus stored state (localStorage, IndexedDB, CacheStorage, service workers) of every origin the jar names. An origin that stored data without ever setting a cookie is missed - a same-user residual, accepted because the cross-user path is the full wipe above. | < 1 s |
| `full` | The whole-profile wipe, same as a change of hands (best-effort here) | Chromium relaunch, ~3-5 s |

The wipe needs the same docker access the stop watchdog already uses. The content
blocker reinstalls itself into the fresh profile automatically.

**When the handover happens** is part of the contract, not an implementation detail: it
runs after the run has passed the concurrency gate and after its stop watchdog is armed
(`hand_jar_to_run`, in the executor). Before the gate it would scrub a browser another
run is still driving, logging that run out of every site mid-task; before the watchdog
it would swallow a Stop for as long as an unresponsive browser makes it wait. A spawned
child does its own handover behind its own gate rather than having the parent do it.
Both orderings are pinned by `tests/test_browser_pool.py`. A per-user instance is exempt
from the scope-change half entirely - its profile belongs to one person.

### Custom CDP port

If port `9222` is already in use on your machine, override it in `.env` before starting Docker:

```bash
# .env
BROWSER_CDP_PORT=9333
```

Then set the matching env var for VAF:

```bash
VAF_BROWSER_CDP_URL=ws://localhost:9333
```

`VAF_BROWSER_CDP_URL` can also be set in VAF's `config.json` or as a system environment variable.

### Remote browser (advanced)

If you run the browser container on a separate machine or in a cloud environment, point VAF to it:

```bash
VAF_BROWSER_CDP_URL=ws://browser-host.internal:9222
```

Make sure the CDP port is not exposed publicly - restrict access at the firewall level.

---

## Anti-Bot Detection

VAF hardens the browser so a vanilla automated browser's obvious tells are removed. The robust, hard-to-bypass parts live in how Chromium is launched (`docker/browser/entrypoint.sh`), not in fragile JavaScript:

- **Headed Chromium under a virtual X display** (KasmVNC's Xkasmvnc) - not `--headless`. The new headless mode still has subtle, detectable tells; a real headed browser does not.
- **`--disable-blink-features=AutomationControlled`** - `navigator.webdriver` is natively `false` (no detectable JS redefine).
- **Version-matched User-Agent** - derived at startup from the actual Chromium version, with no "HeadlessChrome" marker; consistent with `navigator.platform = "Linux x86_64"`.
- **HTTP/2 kept on**, realistic window size (1920×1080), `en-US` locale.
- **Fingerprint supplement** (`vaf/tools/_stealth_supplement.js`, injected via CDP at connect time): replaces the software-renderer "SwiftShader" WebGL string with a realistic Linux/Mesa value and adds subtle, seeded canvas/audio noise. It **never** touches `navigator` (own-property pollution is itself a tell, so playwright-stealth is intentionally **not** injected).
- **Behavioural cadence** - randomized pauses between actions and a short per-step "think time" instead of machine-perfect timing.

This passes common bot-detection checks (`navigator.webdriver`, headless UA, WebGL, plugins, `window.chrome`). **Honest limits:** it does not match TLS JA3/JA4 fingerprints or fully spoof WebRTC, so aggressive managed WAFs (Cloudflare-managed, Kasada, Akamai) can still block, and a flagged/datacenter IP remains the hard limit - use a residential proxy for those.

### Proxy and timezone

The browser can route through an upstream proxy. Both are optional environment variables on the `vaf-browser` container (default: direct connection):

```bash
# .env  (or the environment before `docker compose ... up`)
VAF_BROWSER_PROXY=http://user:pass@host:8080      # or socks5://host:1080  (empty = direct)
VAF_BROWSER_TZ=America/New_York                    # match the proxy region (default: Europe/Berlin)
```

When `VAF_BROWSER_PROXY` is set, the entrypoint adds `--proxy-server` and WebRTC leak protection (`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`) so the real IP does not leak around the proxy. Set `VAF_BROWSER_TZ` to a timezone consistent with the proxy's exit region (a timezone that contradicts the IP is a tell).

---

## Docker Container Details

**Image:** built from `docker/browser/Dockerfile`  
**Base:** `debian:bookworm-slim` + Chromium from Debian repos + the KasmVNC server (version-pinned vendor .deb, checksum-verified; it is BOTH the X server the headed Chromium renders into and the WebSocket stream of that display)  
**Architecture:** amd64 and arm64 are both built, each against its own pinned KasmVNC checksum. The target comes from BuildKit's `TARGETARCH`, and the Dockerfile declares that ARG WITHOUT a default on purpose: BuildKit fills the predefined platform ARGs only when they carry no value of their own, so a default wins over the injected value and an arm64 machine would build an arm64 image around an amd64 package. An empty or unsupported value stops the build instead of guessing.

**Licence boundary (KasmVNC is GPL-2.0).** As things stand VAF distributes none of
it: the Dockerfile fetches an unmodified, version-pinned vendor `.deb`, and the image
is built on the user's own machine (`docker compose build`), where it runs as a
separate process alongside the other Debian packages. That is aggregation, with no
copyleft reach into VAF's own code, and it is why the client is never patched to hide
its branding - the web UI covers it with its own loading state instead.
**Publishing a PREBUILT `vaf-browser` image would change exactly that**: Veyllo would
become the distributor of a GPL-2.0 binary and would owe, for the pinned release, the
licence text and the complete corresponding source (or a written offer for it). Until
someone deliberately takes that on, this stays a local build.  
**Container name:** `vaf-browser`  
**Internal port:** `9223` (CDP), exposed as `9222` via socat  
**Memory limit:** 2 GB (`shm_size: 1gb`)  
**User:** non-root (`browser:browser`)  
**Hardening:** `cap_drop ALL` + `cap_add SYS_CHROOT`, `no-new-privileges:true`, and the seccomp profile `docker/browser/chromium-seccomp.json` (Docker's default plus the user-namespace syscalls) - the combination that lets Chromium run WITH its own sandbox; see [Container hardening](#container-hardening)  
**Health check:** every 10 seconds, and BOTH halves have to answer: CDP on `9222` and the KasmVNC stream on `6901`. They fail apart, so checking one proves nothing about the other. An image built before the stream existed serves CDP perfectly while nothing listens on 6901, and a CDP-only check called that container healthy - the pool then handed it out and the ticket route answered 502 on the first human click. `BrowserPool._wait_healthy` applies the same two-part probe before an instance is handed out, fetching the very path the ticket route serves.

The container runs a **supervised** Chromium process **headed under a virtual X display (KasmVNC's Xkasmvnc)**, not `--headless`. Real headed Chrome leaks far fewer automation signals, so it is the stronger anti-bot baseline (see [Anti-Bot Detection](#anti-bot-detection)). If Chromium ever exits (a crash, an OOM, or the startup issue in [Troubleshooting](#troubleshooting)), the entrypoint relaunches it and serves the CDP proxy only while the browser is live, so the service self-heals instead of staying down. browser-use opens new tabs per task and cleans them up on completion.

**Default behaviour:** each task starts with a clean browser - and that is enforced, not
assumed: the shared jar is scrubbed at run start (always for a non-persistent run, and on
any change of user scope for persistent ones), because browser_use drives the default
browser context and would otherwise inherit whatever the previous user left behind.

**Persistent mode** (`persistent=true`): cookies and storage are saved to `~/.vaf/browser_sessions/<scope>/{session}.json` (one subdirectory per user_scope_id) after each task and restored at the start of the next. See [Persistent Sessions](#persistent-sessions).

### Rebuild after Dockerfile or entrypoint changes

`entrypoint.sh` is copied into the image at build time, so a change to it (or the Dockerfile) needs
a rebuild, not a plain `restart` (which reuses the old image and keeps the old baked-in script).

Starting the stack through VAF now does this for you: the locally built services (browser, TTS) are
started with `--build`. The command below is what to run when you build by hand, and what to reach
for when the log says the KasmVNC stream did not answer.

```bash
docker compose -f docker-compose.memory.yml up -d --build vaf-browser
```

**Chromium updates are the age gate's job, not `--build`'s.** The apt layer that installs
Chromium (unpinned, from Debian) only re-runs when the Dockerfile text above it changes; a
cached `--build` leaves the engine at whatever version the layer was first built with.
Once the image is older than `browser_image_max_age_days` (default 14), the next stack
start rebuilds it with `--pull --no-cache`, which refreshes the Debian base and Chromium
with it; the Security dashboard's firewall card shows the engine version and image age.
To force the same thing by hand:

```bash
docker compose -f docker-compose.memory.yml build --pull --no-cache vaf-browser
docker compose -f docker-compose.memory.yml up -d vaf-browser
```

---

## Troubleshooting

### `Connection refused` on ws://localhost:9222

The browser container is not running or not yet healthy.

```bash
# Check container status
docker ps | grep vaf-browser

# View logs
docker logs vaf-browser

# Restart
docker compose -f docker-compose.memory.yml restart vaf-browser
```

### Container is `(unhealthy)` / logs show `Missing X server or $DISPLAY`

Chromium runs headed under Xkasmvnc (display `:99`). If a previous run crashed without cleaning up, a stale `/tmp/.X99-lock` survives a restart and the X server aborts with `Server is already active for display 99`. The leftover socket then makes startup *look* ready while Chromium actually has no display (`Missing X server or $DISPLAY` → exit), so socat reports `Connection refused` on CDP and the healthcheck flips the container to `(unhealthy)`.

The entrypoint (`docker/browser/entrypoint.sh`) removes the stale lock/socket on start and waits for the X server to be genuinely alive, so this self-heals on the next start. If you still hit it, recreate the container so it gets a clean `/tmp`:

```bash
docker compose -f docker-compose.memory.yml up -d --force-recreate vaf-browser
```

### Container is `(unhealthy)` after a rebuild / logs show `Trace/breakpoint trap` (SIGTRAP)

Debian's `chromium 150.0.7871.46` (and later) crashes on startup with a SIGTRAP when `--no-first-run`
is set and the profile resolves to an EEA region (the container reports `TZ=Europe/Berlin`). Chromium
prints its version, then dies before CDP opens on `9223`, so socat loops with `Connection refused`
and the healthcheck flips the container to `(unhealthy)`. This is Debian bug #1141618 (`149` works,
`150` regressed) on the search-engine-choice code path. The entrypoint launches Chromium **without**
`--no-first-run` (the trigger) and keeps the first-run search-engine choice quiet with
`--disable-search-engine-choice-screen` + `--search-engine-choice-country=US`; if you built the image
before that fix, rebuild:

```bash
docker compose -f docker-compose.memory.yml up -d --build vaf-browser
```

### Container is `(unhealthy)`, logs repeat `The profile appears to be in use by another Chromium process ... on another computer`

A per-user browser keeps its profile in a named volume so logins survive, but the
container around it is disposable. Chromium's `SingletonLock` inside that profile
records `hostname-pid`, so after a hard stop (a force-removed container, a host reboot)
the next container meets a lock naming a machine that no longer exists, reads it as "in
use on another computer" and refuses to start - forever, one supervisor relaunch per
second, while every browser open first waits out the full health deadline before falling
back to the shared browser.

`start_chromium` now deletes the singleton artifacts before each launch, which is safe by
construction: the supervisor's loop-top `pkill -9` already guarantees only one Chromium
per container, and that is the only thing the lock could protect. If you meet this on an
image built before that fix, `docker rm -f <container>` is enough - the profile volume
and its logins are not touched.

### Agent keeps failing steps / `VAFLLMBridge: cannot parse`

The configured LLM is not producing valid structured JSON. Switch to a larger model or an API provider. See [LLM model recommendation](#llm-model-recommendation) above.

### `max_steps` reached without result

The task was too complex for the step budget, or the site uses techniques that defeat DOM-based navigation (heavy CAPTCHAs, aggressive bot detection). Try:

- Increasing `max_steps` (up to 100)
- Breaking the task into smaller subtasks
- Adding `allowed_domains` to prevent the agent from getting lost on redirect chains

### `ERR_HTTP2_PROTOCOL_ERROR` on certain sites

A site may terminate the connection when it detects an automated browser fingerprint - Chromium reports this as `ERR_HTTP2_PROTOCOL_ERROR`. This is a network-layer bot detection, not a CAPTCHA.

The container keeps **HTTP/2 enabled** (real Chrome uses it; disabling it is itself a fingerprint tell), and relies on the headed + fingerprint hardening described in [Anti-Bot Detection](#anti-bot-detection) to avoid being flagged in the first place. QUIC is left off because UDP through Docker NAT is unreliable; it is tunable in `docker/browser/entrypoint.sh`.

If you still see this error, the site is likely doing TLS fingerprinting (JA3) - a deeper layer that a non-patched Chromium cannot defeat; a residential proxy (see [Anti-Bot Detection](#anti-bot-detection)) is the practical lever there.

### CAPTCHA / bot detection

See [Anti-Bot Detection](#anti-bot-detection) for the full hardening (headed Chromium, automation-flag removal, version-matched UA, and the fingerprint supplement). This makes the browser pass common detection checks; a vanilla automated browser does not.

When a CAPTCHA is encountered, the agent uses on-demand vision (`describe_page_visually`) to understand the challenge visually. For image-based CAPTCHAs (reCAPTCHA v2 "click all traffic lights"), a vision-capable model (Anthropic, GPT-4o, Gemini) can attempt to solve them. Behavioral CAPTCHAs (reCAPTCHA v3, Cloudflare Turnstile) depend on browser fingerprint and session/IP trust - the hardening helps, but a flagged IP remains the hard limit.

---

## render_check: the single-look probe

`render_check` is the browser container's second face: not an agent, a probe.
One navigation, no clicking, and a developer's report back - final URL and
title, page errors (uncaught exceptions), console output, failed network
requests (HTTP 400+), the rendered text, and a screenshot saved into the chat
workspace (inspect it with `analyze_image` when layout matters). It exists for
the build-run-inspect-fix loop: write a page, render it, read what actually
happened, fix, render again.

Two callers, one implementation (`vaf/core/browser_render.py`):

- The **main agent** has `render_check` as a registry tool.
- The **coder** registers it as an inner tool bound to the project directory,
  next to `run_tests`: `run_tests` proves the logic, `render_check` proves the
  page. Relative targets like `index.html` resolve against the project.

Targets:

- **Workspace files** ride the existing workspace mirror: the project folder
  is synced into the container and the file opens as
  `file:///home/browser/Workspace/<path>`. Only files inside the caller's own
  project root are renderable (same jail as every file tool).
- **`localhost` URLs** are rewritten to `host.docker.internal`, which both the
  compose browser and pooled instances map to the host (`host-gateway`). The
  hard limit, stated in the tool description because no rewrite can remove it:
  the dev server must listen on `0.0.0.0` (`next dev -H 0.0.0.0`,
  `python -m http.server --bind 0.0.0.0`); a server bound to `127.0.0.1` is
  invisible to any container. The mapping adds a NAME, not reachability - the
  host's docker bridge address was always routable from the container.
- **Public URLs** pass through unchanged.

The probe never evicts anyone: while an interactive session or an agent run
holds the browser, it answers busy instead of hijacking the visible tab. After
rendering it parks the page on `about:blank` (an animating page with nobody
watching burns CPU permanently). Everything runs through the same per-user
resolution as the rest of the browser stack, so a pooled user probes their own
instance.

## Known Limitations (v1)

| Limitation | Notes |
|---|---|
| **On-demand vision** | A vision-capable browser-lane model takes screenshots itself when needed (see [Vision tiers](#vision-tiers)). For text-only models, configure a Vision Model in Settings → AI & Model; with no vision at all the run continues DOM-only. |
| **Session persistence** | Available via `persistent=true` + `session` parameter. Default mode still clears state between calls. |
| **CAPTCHA** | No solver integrated. Sites with aggressive bot detection may block the agent. |
| **Local LLMs** | Models below ~30B parameters struggle with structured JSON output required by browser-use. |
| **Shared browser beyond the pool** | Each of the first `browser_pool_max` users (default 2) gets their own browser container and runs in parallel (see [Per-user browser pool](#per-user-browser-pool-parallel-use)). Everyone beyond that shares one Chromium process, serialised by a queue (see [Concurrency](#concurrency--multi-user)). Raising the pool size raises the number of people who never meet there. |
| **Live view frame rate** | Chat runs stream the browser live (KasmVNC, watch-only). The screenshot lanes (workflow tile, spawned child) stay at ~1 frame/1.5 s - sufficient for monitoring. |

---

## Source Files

| File | Purpose |
|---|---|
| [vaf/tools/browser_agent.py](../../vaf/tools/browser_agent.py) | Tool implementation, `VAFLLMBridge`, `BrowserAgentTool`, screenshot loop |
| [vaf/core/browser_render.py](../../vaf/core/browser_render.py) | `render_page()` - the single-look probe behind `render_check` (navigate, observe, screenshot, park) |
| [vaf/tools/render_check.py](../../vaf/tools/render_check.py) | `render_check` tool face for the main agent and the coder (see [render_check](#render_check-the-single-look-probe)) |
| [vaf/core/web_interface.py](../../vaf/core/web_interface.py) | `emit_browser_frame()`/`emit_browser_step()` - WebSocket broadcast in-process; **HTTP-bridged to the main process when running in a sub-agent subprocess** |
| [web/components/SubAgentWindow.tsx](../../web/components/SubAgentWindow.tsx) | Live viewport panel (URL bar + screenshot) - standalone runs |
| [web/components/BrowserLiveTile.tsx](../../web/components/BrowserLiveTile.tsx) | Tiled live view left of the Workflow Runtime window (browser-in-workflow) |
| [web/app/page.tsx](../../web/app/page.tsx) | `browser_frame_update` handler, `subAgentState.browserFrame/browserUrl`, tile mount, the chat's browser button (`toggleBrowserWindow`) |
| [vaf/tools/_stealth_supplement.js](../../vaf/tools/_stealth_supplement.js) | Fingerprint supplement injected via CDP (WebGL renderer realism + canvas/audio noise) |
| [docker/browser/Dockerfile](../../docker/browser/Dockerfile) | Browser container image definition (Chromium + KasmVNC) |
| [docker/browser/entrypoint.sh](../../docker/browser/entrypoint.sh) | Xkasmvnc display + headed Chromium launch + anti-detection flags + optional proxy |
| [docker-compose.memory.yml](../../docker-compose.memory.yml) | `vaf-browser` service definition (`VAF_BROWSER_PROXY` / `VAF_BROWSER_TZ` env) |
