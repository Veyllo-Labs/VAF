# Browser Agent

VAF's `browser_agent` tool gives the AI agent the ability to control a real Chromium browser — navigating pages, clicking elements, filling forms, handling JavaScript-heavy sites, and extracting structured content from live web applications.

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
    └── Chromium --headless=new --remote-debugging-port=9222
```

**Chromium runs in a dedicated Docker container** (`vaf-browser`). VAF connects to it via the [Chrome DevTools Protocol (CDP)](https://chromedevtools.github.io/devtools-protocol/) over a local WebSocket. No browser is ever installed on the host machine.

The browser container is part of `docker-compose.memory.yml` and starts automatically alongside all other VAF services.

---

## Setup

### Start the browser container

```bash
docker compose -f docker-compose.memory.yml up -d
```

On first run, Docker builds the `vaf-browser` image from `docker/browser/Dockerfile` (~30 seconds). Subsequent starts are instant.

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
| "What's the current price of X on amazon.com?" | `browser_agent` — prices are loaded by JS |
| "Search for Python tutorials" | `web_search` — static content, faster |
| "Log into my dashboard and get the usage report" | `browser_agent` — requires login + navigation |
| "Get the plain text of this Wikipedia article" | `webfetch` — static HTML, no interaction needed |
| "Fill out the contact form on example.com" | `browser_agent` — form interaction required |

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
5. browser-use Agent loop starts (use_vision='auto'):
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

### Result format

The tool always returns a plain string — the VAF agent reads it and incorporates it into its response to the user.

```
Browser task completed.

Result:
1. "Show HN: I built a local-first AI framework" — 342 points
2. "Ask HN: What are you working on? (May 2026)" — 287 points
3. "Postgres 18 released" — 241 points
...
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `task` | string | ✅ | — | Plain-language description of what the browser should do. Include URLs, credentials, and what data to extract. |
| `max_steps` | integer | ❌ | 25 | Maximum number of browser actions before stopping. Cap: 100. |
| `allowed_domains` | string[] | ❌ | unrestricted | Whitelist of domains the browser may visit. Prevents the agent from navigating outside the intended scope. |
| `persistent` | boolean | ❌ | `false` | If `true`, cookies and login state are saved after the task and restored on the next call with the same `session` name. Use for sites that require login. |
| `session` | string | ❌ | `"default"` | Named cookie store. Only used when `persistent=true`. Use a descriptive name like `"tipico"`, `"amazon"`, `"banking"`. Each name is independent. |

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

By default every `browser_agent` call starts with a completely clean browser profile — no cookies, no login state. This is safe but means the agent must re-login on every task.

With `persistent=true`, VAF saves the browser's cookies and storage state to `~/.vaf/browser_sessions/{session}.json` after each task and restores it at the start of the next call with the same session name.

### First call — login

```json
{
  "task": "Go to tipico.de/login, log in with user@example.com / mypassword, confirm I am logged in",
  "persistent": true,
  "session": "tipico"
}
```

After this call, `~/.vaf/browser_sessions/tipico.json` contains the session cookies.

### Subsequent calls — already logged in

```json
{
  "task": "Go to tipico.de and return my current open bets",
  "persistent": true,
  "session": "tipico"
}
```

The agent navigates directly to the page — no login step needed.

### Session file location

```
~/.vaf/browser_sessions/
├── tipico.json
├── amazon.json
└── banking.json
```

Each file is a Playwright `storage_state` JSON — contains cookies, localStorage, and sessionStorage for all domains visited during the session.

### Security note

Session files contain login cookies in plain text. They are stored in `~/.vaf/browser_sessions/` which is only accessible to the OS user running VAF. Do not commit these files to version control.

---

## LLM Bridge

`browser_agent` does **not** use a separate AI service. It routes all reasoning through VAF's own configured LLM — the same model used for everything else (local Ollama, OpenAI, Anthropic, DeepSeek, etc.).

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

The bridge (`VAFLLMBridge`) implements browser-use's `BaseChatModel` protocol and delegates every LLM call to `APIBackendManager` — the same class used by all other VAF tools.

### On-demand vision

The browser agent uses **`use_vision='auto'`** — browser-use decides per-step whether to attach a screenshot. Screenshots are only sent to the LLM when the page cannot be understood from DOM text alone (e.g. image-heavy pages, CAPTCHAs, visual challenges).

When a screenshot is sent, `VAFLLMBridge` handles it based on the configured provider:

| Main provider | Vision handling |
|---|---|
| Anthropic, Google, GPT-4o | Screenshot passed natively — provider sees the image directly |
| DeepSeek, non-vision models | Screenshot sent to `vision_provider` (configured in Settings → AI & Model) → text description injected into the message |
| No vision provider configured | Screenshot skipped — DOM-only fallback |

The agent also has a `describe_page_visually()` action it can call explicitly when stuck — for example, if it detects a CAPTCHA in the DOM and needs to understand what type it is before deciding how to proceed. Vision cost is only paid when actually needed — not on every step.

### LLM model recommendation

browser-use requires the LLM to produce structured JSON on every reasoning step. This works well with:

| Provider | Recommended model |
|---|---|
| OpenAI | `gpt-4o` or `gpt-4o-mini` |
| Anthropic | `claude-3-5-sonnet-20241022` or newer |
| DeepSeek | `deepseek-v4-flash` |
| Local (Ollama) | `llama3.1:70b` or `qwen2.5:32b` minimum |

⚠️ **Local models below ~30B parameters** frequently produce malformed JSON for complex browser tasks and will cause step failures. For production use, an API provider or a large local model is strongly recommended.

---

## Live View in WebUI

When `browser_agent` is running, the **SubAgent Window** in the WebUI opens automatically and shows a live browser view:

```
┌─────────────────────────────────────┐
│ Browser Agent  ● Running            │
│─────────────────────────────────────│
│ 🌐 https://news.ycombinator.com  ● LIVE │
│ ┌─────────────────────────────────┐ │
│ │   [Live Screenshot ~1.5 fps]    │ │
│ └─────────────────────────────────┘ │
│─────────────────────────────────────│
│ Console                             │
│ [12:34:01] Start: browser_agent … │
└─────────────────────────────────────┘
```

- **URL bar** — shows the current page URL, updated with every screenshot
- **Live indicator** — red pulsing dot disappears when the task ends
- **Frame rate** — ~1 frame per 1.5 seconds (JPEG quality 55, ~30–80 KB/frame)
- **Console** — tool start/end events and log lines still shown below the viewport

The viewport is visible in both **dock mode** (right side panel) and **overlay mode** (full-screen modal, triggered by clicking the SubAgent bubble in the chat).

### How frames reach the UI (subprocess bridge)

The browser agent runs as its own **child process** (in workflows, and via `subagent run`). That
process has no local WebSocket clients, so `web_interface.emit_browser_frame()` / `emit_browser_step()`
**bridge each frame over HTTP** (off-thread, non-blocking) to the main process's
`POST /api/subagent/stream` whenever `VAF_IN_SUBAGENT_TERMINAL=1`. The generic endpoint then
broadcasts a `browser_frame_update` to the session's WebSocket, which the WebUI already handles. So
the path is: child screenshot loop → HTTP → main process → WebSocket → `subAgentState.browserFrame`.

### Inside a workflow: tiled live view

When the browser runs as a **workflow step**, the SubAgent dock is normally suppressed (its output
goes to the Workflow Runtime terminal). Frames are visual, though, so they are shown in a dedicated
`BrowserLiveTile` **docked to the left of the Workflow Runtime window** (side by side, not
overlapping). Standalone (outside a workflow) the browser view still renders in the SubAgent dock.
See [Window Tiling](WINDOW_TILING_DESIGN.md) and [Workflow UI Components](WORKFLOW_UI_COMPONENTS.md).

---

## Security

### Permission level: `write`

`browser_agent` is classified as a `write` tool — it can navigate, click, and submit forms, but does not require a separate destructive-action confirmation gate. Actions visible in the live view give the user real-time oversight of what the agent is doing.

### Network isolation

The CDP port (`9222`) is bound to `127.0.0.1` only — it is **never exposed** to the network or other machines.

The `vaf-browser` container runs on its own isolated Docker network (`vaf-browser-network`) and is **not** on `vaf-network`. This means the browser container cannot reach `postgres` or `redis` by hostname — a compromised browser (e.g. via SSRF or a malicious page) has no direct path to VAF's database.

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

`browser_agent` is **blocked** on Telegram, WhatsApp, and Discord channels. These channels have no interactive confirmation flow, so dangerous tools cannot run there by design.

---

## Configuration

### Concurrency / Multi-user

By default, `browser_agent` serialises all calls — only **one** browser session runs at a time. If a second user (or a second concurrent workflow) triggers `browser_agent` while a session is already running, the call waits in a queue for up to **120 seconds** before giving up with a "Browser agent is busy" message.

This avoids memory exhaustion and tab interference in the shared Chromium container.

To allow **2 parallel sessions** on a machine with sufficient RAM (≥ 8 GB):

```bash
# .env  (or system environment before starting VAF)
VAF_BROWSER_MAX_PARALLEL=2
```

The practical limits are:

| `VAF_BROWSER_MAX_PARALLEL` | Recommended host RAM | Notes |
|---|---|---|
| `1` (default) | 4 GB+ | Safe for single-user or small teams |
| `2` | 8 GB+ | Handles two concurrent users |
| `3+` | 16 GB+ | Not recommended; Chromium memory adds up fast |

---

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

Make sure the CDP port is not exposed publicly — restrict access at the firewall level.

---

## Docker Container Details

**Image:** built from `docker/browser/Dockerfile`  
**Base:** `debian:bookworm-slim` + Chromium from Debian repos  
**Container name:** `vaf-browser`  
**Internal port:** `9222` (CDP)  
**Memory limit:** 2 GB  
**User:** non-root (`browser:browser`)  
**Health check:** `curl http://localhost:9222/json/version` every 10 seconds

The container runs a single persistent Chromium process. browser-use opens new tabs per task and cleans them up on completion.

**Default behaviour:** each task starts with a clean browser profile — no cookies, no login state.

**Persistent mode** (`persistent=true`): cookies and storage are saved to `~/.vaf/browser_sessions/{session}.json` after each task and restored at the start of the next. See [Persistent Sessions](#persistent-sessions).

**Chrome flags applied:** `--disable-http2 --disable-quic` (forces HTTP/1.1, bypasses HTTP/2 fingerprinting used by Cloudflare Bot Management).

### Rebuild after Dockerfile changes

```bash
docker compose -f docker-compose.memory.yml build vaf-browser
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

### Agent keeps failing steps / `VAFLLMBridge: cannot parse`

The configured LLM is not producing valid structured JSON. Switch to a larger model or an API provider. See [LLM model recommendation](#llm-model-recommendation) above.

### `max_steps` reached without result

The task was too complex for the step budget, or the site uses techniques that defeat DOM-based navigation (heavy CAPTCHAs, aggressive bot detection). Try:

- Increasing `max_steps` (up to 100)
- Breaking the task into smaller subtasks
- Adding `allowed_domains` to prevent the agent from getting lost on redirect chains

### `ERR_HTTP2_PROTOCOL_ERROR` on certain sites

Some sites (e.g. Cloudflare Bot Management) terminate the HTTP/2 connection when they detect a headless browser fingerprint — Chromium reports this as `ERR_HTTP2_PROTOCOL_ERROR`. This is a network-layer bot detection, not a CAPTCHA.

VAF's browser container launches Chromium with `--disable-http2 --disable-quic`, which forces HTTP/1.1 and bypasses HTTP/2 fingerprinting. This is applied automatically — no action needed.

If you still see this error, the site may be doing TLS fingerprinting (JA3) — a separate, deeper layer of detection.

### CAPTCHA / bot detection

VAF injects a stealth script (`vaf/tools/_stealth_payload.js`) that masks common headless browser signals (navigator.webdriver, WebGL vendor, etc.), which reduces detection on most sites.

When a CAPTCHA is encountered, the agent uses on-demand vision (`describe_page_visually`) to understand the challenge visually. For image-based CAPTCHAs (reCAPTCHA v2 "click all traffic lights"), a vision-capable model (Anthropic, GPT-4o, Gemini) can attempt to solve them. Behavioral CAPTCHAs (reCAPTCHA v3, Cloudflare Turnstile) depend on browser fingerprint and session trust — the stealth script and HTTP/1.1 bypass help here.

---

## Known Limitations (v1)

| Limitation | Notes |
|---|---|
| **On-demand vision** | Vision is used only when browser-use determines it's needed (`use_vision='auto'`). Configure a Vision Model in Settings → AI & Model for providers that don't support vision natively (e.g. DeepSeek). |
| **Session persistence** | Available via `persistent=true` + `session` parameter. Default mode still clears state between calls. |
| **CAPTCHA** | No solver integrated. Sites with aggressive bot detection may block the agent. |
| **Local LLMs** | Models below ~30B parameters struggle with structured JSON output required by browser-use. |
| **Single browser instance** | All tasks share one Chromium process. Concurrent requests are serialised by a queue (see [Concurrency](#concurrency--multi-user)). |
| **Live view frame rate** | ~1 frame/1.5 s — sufficient for monitoring, not a real-time stream. |

---

## Source Files

| File | Purpose |
|---|---|
| [vaf/tools/browser_agent.py](../vaf/tools/browser_agent.py) | Tool implementation, `VAFLLMBridge`, `BrowserAgentTool`, screenshot loop |
| [vaf/core/web_interface.py](../vaf/core/web_interface.py) | `emit_browser_frame()`/`emit_browser_step()` — WebSocket broadcast in-process; **HTTP-bridged to the main process when running in a sub-agent subprocess** |
| [web/components/SubAgentWindow.tsx](../web/components/SubAgentWindow.tsx) | Live viewport panel (URL bar + screenshot) — standalone runs |
| [web/components/BrowserLiveTile.tsx](../web/components/BrowserLiveTile.tsx) | Tiled live view left of the Workflow Runtime window (browser-in-workflow) |
| [web/app/page.tsx](../web/app/page.tsx) | `browser_frame_update` handler, `subAgentState.browserFrame/browserUrl`, tile mount |
| [docker/browser/Dockerfile](../docker/browser/Dockerfile) | Browser container image definition |
| [docker-compose.memory.yml](../docker-compose.memory.yml) | `vaf-browser` service definition (search for `vaf-browser`) |
