# VAF API Integration Guide

VAF now supports multiple AI providers through API integration, allowing you to use commercial AI services alongside or instead of local models.

## Supported Providers

- **Local** - llama-server (default, runs locally)
- **OpenAI** - GPT-4, GPT-4o, GPT-3.5-turbo
- **Anthropic** - Claude Sonnet 4.6, Claude Opus 4.8, Claude Haiku 4.5 (native Messages API: tool use, streaming, adaptive thinking, prompt caching)
- **DeepSeek** - DeepSeek V4 Flash, DeepSeek V4 Pro
- **Google AI Studio** - Gemini 2.5 Flash/Pro, Gemini 3.5 Flash (native google-genai SDK: tool use, streaming, thinking)
- **OpenRouter** - Multi-provider access (Claude, GPT-4, Llama, etc.)

## Configuration

### 1. Set AI Provider

Run VAF and open settings:

```bash
vaf run
# Press 's' for settings
# Select "AI Provider: LOCAL"
```

Choose your provider and enter API key when prompted.

### 2. API Key Management

**Best Practices Implemented:**

- API keys are **Base64 encoded** for basic obfuscation (not encryption)
- Keys are stored in `~/.vaf/config.json`
- Keys are **masked** when displayed (e.g., `sk-proj1...ab2c`)
- **Connection testing** before saving to verify validity

**Manual Configuration:**

Edit `~/.vaf/config.json`:

```json
{
  "provider": "anthropic",
  "api_key_anthropic": "BASE64_ENCODED_KEY_HERE"
}
```

**Using Python to encode keys:**

```python
import base64
from vaf.core.config import Config

# Set API key (automatically encodes)
Config.set_api_key("anthropic", "sk-ant-your-actual-key-here")

# Get API key (automatically decodes)
key = Config.get_api_key("anthropic")
```

### 3. Model Selection

Each provider has default models, but you can customize:

```json
{
  "provider": "openai",
  "api_model_openai": "gpt-4o",
  "api_model_anthropic": "claude-sonnet-4-6",
  "api_model_deepseek": "deepseek-v4-flash",
  "api_model_google": "gemini-2.5-flash",
  "api_model_openrouter": "anthropic/claude-sonnet-4.6"
}
```

**Guardrail for mixed local/API setups:** If an API provider is active but a local GGUF-style model value is still present (for example `gemma-4-E2B-it-Q8_0.gguf` or `"auto"`), VAF automatically falls back to the provider-specific `api_model_<provider>` value. This prevents API requests from failing with provider-side "model not found" errors.

### Single source of truth for provider models

The per-provider default model and the static fallback list shown in the dropdown live in one place: `PROVIDER_MODELS` in `vaf/core/config.py`. To change a default or a fallback, edit that dict only — every Python call site reads it (`Config.get_default_model()` / `Config.get_fallback_models()`, `APIBackendManager`, the agent, the browser agent), and the web UI reads it through `GET /api/provider-models`.

The dropdown is otherwise populated dynamically: when you enter an API key, VAF fetches the provider's live model list (`/v1/models`) and the live list takes precedence. The static fallback is used only when no key is set or the fetch fails (offline, rate limit). `local` is not in `PROVIDER_MODELS` — GGUF models are discovered from disk, not a fixed list.

## Sub-Agent Provider Configuration

**Sub-agents can use a different provider than the main agent!**

Example use cases:
- **Main:** Claude API (high quality) | **Sub-Agents:** Local (free, fast)
- **Main:** Local (privacy) | **Sub-Agents:** GPT-4 (code generation)

### Configuration

1. Open Settings → "Sub-Agent Provider"
2. Choose provider for sub-agents:
   - **Inherit** - Use same provider as main agent (default)
   - **Local** - Always use local model for sub-agents
   - **API Provider** - Use specific API for sub-agents

**Config File:**

```json
{
  "provider": "anthropic",
  "subagent_provider": "local",
  "subagent_use_separate_provider": true
}
```

## Server Auto-Start

When using API providers, you may want to disable automatic llama-server startup:

```json
{
  "provider": "openai",
  "auto_start_local_server": false
}
```

This saves resources when not using local models.

## Web Search API Keys

The `web_search` tool can use optional search APIs when keys are set. This avoids reliance on scraping and improves reliability.

**Order of use:** Brave Search API (if key set) → Google Custom Search API (if key and search engine ID set) → scrape Google → DuckDuckGo → **internal knowledge (RAG)**.

The final fallback consults VAF's own long-term memory when every web provider fails (rate limit, missing keys, network down) or genuinely finds nothing — useful for internal topics the web cannot know. Memory hits are labeled honestly: `memory://` hrefs, titles prefixed "Internes Wissen" with the relevance score, `source: internal_knowledge`. Provider failures are also collected (`get_search_provider_errors()` in `vaf/tools/search.py`) so callers like the research agent can report "search unavailable" instead of pretending there were no results.

**Where to set:** Web UI → Settings → General → "Web Search (API)", or in `~/.vaf/config.json`:

| Key | Description |
| :--- | :--- |
| `api_key_brave_search` | Brave Search API key. Create at [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com/app/keys). |
| `api_key_google_search` | Google Custom Search API key (Cloud Console, Custom Search API enabled). |
| `google_search_engine_id` | Programmable Search Engine ID (cx). Create a search engine at [programmablesearchengine.google.com](https://programmablesearchengine.google.com/controlpanel/create) and set it to search the entire web. |

If no API keys are set, the tool uses the built-in path (scrape Google, then DuckDuckGo on failure). The same order applies to the agent’s internal `perform_web_search` (e.g. deep research).

## Local Server: Prompt Cache (Memory)

When using the local provider, the llama-server reserves RAM for a prompt cache so it can reuse conversation context across turns instead of re-evaluating the full history each time. You can tune this in `~/.vaf/config.json` or in the Web UI under **AI & Model** → **Local Model Settings**:

| Key | Default | Description |
| :--- | :--- | :--- |
| `llama_cache_ram` | `4096` | Cache size in MB. `0` disables the cache. `-1` uses 40% of free system RAM, capped at 8192 MB. Valid range: 0–16384. |

On systems with limited RAM (e.g. 32 GB total), a lower value (e.g. 2048 or 0) reduces the risk of swapping or out-of-memory errors. On systems with more RAM, a higher value (e.g. 8192) can improve response time in long conversations. Changes apply after the next server start.

## Local Server: Request Timeouts

The agent talks to the local llama-server at `http://127.0.0.1:8080/v1/chat/completions` with a **streamed** response. To avoid the chat queue hanging when the server stops sending data (e.g. model stuck, long “thinking”, or overload), timeouts are applied:

- **Connect:** 30 seconds to establish the connection.
- **Read:** 60 seconds for blocking reads from the stream.
- **Heartbeat stall guard:** During streaming, an additional heartbeat guard aborts when no usable chunks arrive for ~30 seconds.

On timeout:

- If the timeout happens **before** any response body is received, the agent retries (same as for connection errors), then gives up after retries.
- If the timeout happens **during** streaming, the agent stops waiting, appends a short message to the user (e.g. “Answer aborted due to timeout. Please try again.”), and ends the step so the queue can process the next message.

Logs: `backend.log` may show `server(8080) read_timeout no_data_60s`, `server(8080) heartbeat_timeout no_data_30s`, or `server(8080) read_timeout_during_stream`. If you see these often, the model or machine may be overloaded or the prompt may be too large.

## API Features

### Streaming

All API providers support **streaming responses** for real-time output.

### Tool Calling

- ✅ **OpenAI** - Full support (native function calling)
- ✅ **Anthropic** - Full support (`tool_use`/`tool_result` roundtrip via the native SDK)
- ✅ **Google** - Full support (`function_call`/`function_response` via the native google-genai SDK)
- ✅ **OpenRouter** - Provider-dependent
- ⚠️ **DeepSeek** - `tool_choice` limited to `auto`/`none` (see LLM_BACKEND_FACTS.md)

### Vision / Image Input

Providers that support multimodal (image) input:

- ✅ **OpenAI** (`gpt-4o`, `gpt-4-turbo`, `gpt-4o-mini`) — `image_url` with data URIs
- ✅ **Anthropic** (`claude-sonnet-4*`, `claude-opus-4*`, `claude-haiku-4*`, `claude-3*`) — converted to `source.base64` format
- ✅ **Google** (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-3.5-flash`, etc.) — converted to `Part.from_bytes` (inline_data) parts
- ✅ **OpenRouter** — provider-dependent (model must support vision)
- ❌ **DeepSeek** — the commercial API (`api.deepseek.com/v1`) does **not** support image input. The API schema only accepts `type: text` content blocks and returns a 400 error if image data is sent. Use Anthropic or OpenAI for vision tasks.
- ⚠️ **Local (Ollama)** — only if the loaded model supports vision (e.g. `llava`)

**How it works internally:**

1. The UI encodes images as base64 data URIs and sends them in the `files` array of the WebSocket `chat` message (`mimeType` starts with `image/`).
2. `web_server.py` separates image files from document/text files. Images are stored as `{data, mime_type, name}` in `task.metadata["images"]`; text files continue through the existing Librarian extraction path.
3. `headless_runner.py` extracts images from `task.metadata` and passes them to `agent.chat_step(images=...)`.
4. `agent.chat_step` stores images in the history dict entry alongside the text content.
5. `_prepare_messages()` converts the history entry to OpenAI multimodal list-content format: `[{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]`.
6. Each provider class converts this intermediate format to its own wire format:
   - `OpenAIProvider` (also DeepSeek, OpenRouter, local): passes list content as-is.
   - `AnthropicProvider._convert_content()`: converts `image_url` → `{"type":"image","source":{"type":"base64","media_type":"...","data":"..."}}`.
   - `GoogleProvider`: converts `image_url` → `types.Part.from_bytes(data=..., mime_type=...)` (google-genai inline_data part).

**Limitation:** Images are held in memory for the duration of the session. They are not persisted to `session.json` (only the text content is saved), so image thumbnails will not appear in the chat after a page reload.

**Non-vision model degradation:** `_prepare_messages()` checks `provider + model_name` against a known-vision list before building multimodal content blocks. If the active model does not support vision (e.g. any DeepSeek model), images are silently stripped and replaced with `[Image attached: filename]` text so the agent still runs without hanging. A `⚠️ Vision not supported` warning is emitted to the WebUI. The check lives in `vaf/core/agent.py` → `_model_supports_vision()`.

**Image downscaling:** Before an image is sent, oversized images are downscaled — the longest edge is capped (default 2000 px; OpenAI internally caps high-detail at ~2048 px) and the image is re-encoded (JPEG by default; PNGs with transparency stay PNG). This runs in `vaf/core/image_utils.py` → `downscale_image_b64()`, applied inside `_prepare_messages()` for both the primary and the vision-fallback path, so every provider benefits. It prevents OpenAI returning **HTTP 500** on full-resolution photos (multi-MB base64 payloads) and lowers token cost; images already within the cap are passed through byte-for-byte. The helper never raises — on any decode error it sends the original. Tunable via `vision_image_max_edge` and `vision_image_jpeg_quality`.

### Request Timeouts & Retries (API providers)

The OpenAI-compatible client (OpenAI, DeepSeek, OpenRouter, local) is created with explicit `httpx` timeouts: `connect`/`write` are bounded so a large upload cannot hang, while `read` stays generous (default 600 s) so long reasoning streams are not cut off. On a transient failure at request initiation — HTTP 5xx, timeout, or connection drop — VAF retries the call a few times with backoff. The retry wraps only the request initiation (before any token is streamed), so it can never duplicate output, and it sits on top of the OpenAI SDK's own retries to ride out longer transient outages. Tunable via `api_retry_attempts` and `api_timeout_*`.

### Multi-Tool Wrapper Compatibility

Some models emit a wrapper call named `multi_tool_use.parallel` with a `tool_uses` array.
VAF accepts this wrapper and executes each entry as a normal tool call.

**Accepted fields per entry:**
- `recipient_name` (example: `functions.web_search`)
- `parameters` (arguments passed to the tool)

Execution is sequential to preserve tool gating and interactive prompts.

### Context Windows

Provider context limits are respected:
- **GPT-4o**: ~128K tokens
- **Claude Sonnet/Opus 4.x**: ~1M tokens (Claude Haiku 4.5: ~200K)
- **Gemini 1.5 Pro**: ~2M tokens
- **DeepSeek V4**: ~1M tokens

## Error Handling

### Common Issues

**1. Invalid API Key**

```
API Backend Error: API key not set for provider: anthropic
```

**Solution:** Configure API key in settings or manually in config.json

**2. Rate Limiting**

```
API request failed: 429 - Rate limit exceeded
```

**Solution:** Wait or upgrade API plan

**3. Network Timeout**

```
API request timed out for openai
```

**Solution:** Check internet connection, API may be overloaded

### Best Practices

1. **Test API keys** after entry (automatic in settings menu)
2. **Monitor token usage** - APIs charge per token
3. **Use local for development** - Switch to API for production
4. **Sub-agent optimization** - Use cheaper/faster model for sub-tasks

## Security Considerations

**Important Security Notes:**

1. **Base64 is NOT encryption** - API keys in `config.json` are Base64-encoded (obfuscation), not encrypted.
2. **Owner-only config** - VAF writes `config.json` with `0600` permissions automatically where the OS supports it, so other local users cannot read it. On Windows, ensure only the running user can read `~/.vaf/`.
3. **For stronger protection**, consider:
   - Environment variables: `export ANTHROPIC_API_KEY="..."`
   - System keyring for API keys (OAuth and IMAP credentials already use it — see [CONNECTIONS.md](../integrations/CONNECTIONS.md))
   - Secret management services (AWS Secrets Manager, etc.)

**Verify file permissions (Unix):**

```bash
ls -l ~/.vaf/config.json   # expect -rw------- (600)
```

## API Provider Details

### OpenAI

**Models:**
- `gpt-4o` - Multimodal (recommended, default)
- `gpt-4-turbo` - Fast, large context
- `gpt-3.5-turbo` - Cheaper, faster

Reasoning models (`o1`/`o3`/`o4` series, `gpt-5`) are supported. VAF detects them and
adjusts the request automatically: it sends `max_completion_tokens` instead of `max_tokens`
and omits `temperature` (these models accept only the default), since the direct OpenAI API
rejects the standard parameters otherwise. This applies to the direct OpenAI provider only;
OpenRouter normalizes parameters itself, so its routes are not adjusted.

**Get API Key:** https://platform.openai.com/api-keys

### Anthropic (Claude)

**Models:**
- `claude-sonnet-4-6` - Best balance of speed, intelligence and cost (recommended, default)
- `claude-opus-4-8` - Most capable Opus model, for demanding agentic work
- `claude-haiku-4-5` - Fast, cheaper, for simple tasks

VAF calls Anthropic through the native Messages API (official SDK): tool use, streaming,
and adaptive thinking. Two optional config flags (`~/.vaf/config.json`):

| Key | Default | Description |
| :--- | :--- | :--- |
| `anthropic_thinking` | `true` | Adaptive (extended) thinking on supported models (Sonnet 4.6, Opus 4.6/4.7/4.8, Fable). Reasoning is shown wrapped in `<think>…</think>` like DeepSeek. Set `false` to disable. |
| `anthropic_prompt_cache` | `true` | Caches the system prompt prefix (`cache_control: ephemeral`) to cut cost on multi-turn / tool loops. Note: VAF's system prompt contains volatile parts (date etc.), so the cache hit rate may be limited until the prefix is stable. |

Sampling note: `temperature` is omitted automatically when thinking is active or on models
that reject sampling params (Opus 4.7/4.8, Fable) — otherwise the request would 400.

**Get API Key:** https://console.anthropic.com/

### DeepSeek

**Models:**
- `deepseek-v4-flash` - General purpose, fast, supports function calling (default)
- `deepseek-v4-pro` - More capable, supports function calling
- `deepseek-chat` - Legacy alias for V3; deprecated 2026-07-24
- `deepseek-reasoner` - Legacy R1 alias; deprecated 2026-07-24; no function calling

**Get API Key:** https://platform.deepseek.com/

**Note:** The commercial API (`api.deepseek.com/v1`) is text-only — image input is not supported for any model. Use Anthropic or OpenAI for vision tasks.

### Google AI Studio

**Models:**
- `gemini-2.5-flash` - Best price-performance, 1M context (recommended, default)
- `gemini-3.5-flash` - Most intelligent flash, strong for agentic/coding
- `gemini-2.5-pro` - Most advanced, deep reasoning
- `gemini-2.5-flash-lite` - Fastest, most budget-friendly

VAF uses the native **google-genai** SDK (the deprecated `google-generativeai` package
is no longer used). Tool use, streaming, vision, and thinking are supported. One optional
config flag (`~/.vaf/config.json`):

| Key | Default | Description |
| :--- | :--- | :--- |
| `google_thinking` | `true` | Surface model reasoning on thinking-capable models (Gemini 2.5/3.x), shown wrapped in `<think>…</think>` like DeepSeek. Set `false` to hide it. |

**Note:** Gemini 1.5 models are retired (return 404) and `gemini-2.0-flash` is being
shut down — use the 2.5/3.x models above.

**Get API Key:** https://makersuite.google.com/app/apikey

### OpenRouter

**Multi-provider access** through single API key.

**Popular Models** (OpenRouter uses dotted ids, e.g. `claude-sonnet-4.6`):
- `anthropic/claude-sonnet-4.6`
- `openai/gpt-4o`
- `google/gemini-2.5-flash`
- `meta-llama/llama-3.1-405b-instruct`

Note: OpenRouter normalizes parameters (e.g. `max_tokens`) across all models, so VAF's
OpenAI reasoning-model gating is intentionally applied only to the direct OpenAI provider.

**Get API Key:** https://openrouter.ai/keys

## Example Workflows

### 1. High-Quality Research with Budget Optimization

**Setup:**
- Main Agent: Claude 3.5 Sonnet (API) - Best reasoning
- Sub-Agents: Local Model - Free, handle simple tasks

**Use Case:** Research reports where main analysis needs high quality, but file processing can use local model.

### 2. Code Generation with Privacy

**Setup:**
- Main Agent: Local Model - Keep prompts private
- Sub-Agents: GPT-4 (API) - Generate code in isolated sub-agents

**Use Case:** Sensitive project with public code generation needs.

### 3. All-API Setup

**Setup:**
- Main Agent: OpenRouter (multi-model access)
- Sub-Agents: DeepSeek (cheap, fast)
- Local Server: Disabled (`auto_start_local_server: false`)

**Use Case:** Cloud/server deployment, no local GPU.

## Troubleshooting

### Test API Connection

```python
from vaf.core.api_backend import APIBackendManager

# Test specific provider
result = APIBackendManager.test_connection("anthropic")
print(f"Connection test: {'✓ Success' if result else '✗ Failed'}")
```

### Check Current Provider

```python
from vaf.core.config import Config

print(f"Provider: {Config.get('provider')}")
print(f"Sub-Agent Provider: {Config.get('subagent_provider')}")
```

### Reset to Local

```python
from vaf.core.config import Config

Config.set("provider", "local")
Config.set("auto_start_local_server", True)
```

## Performance Tips

1. **Use streaming** - Faster perceived response time
2. **Choose right model** - Balance cost vs. quality
3. **Local for iteration** - Develop with local, deploy with API
4. **Sub-agent optimization** - Use cheaper models for simple tasks
5. **Monitor token usage** - APIs charge per token

## Next Steps

- Configure your first API provider in Settings
- Test with a simple query
- Experiment with sub-agent provider separation
- Monitor costs and optimize model selection

For more help, see:

## Network API Endpoints

These endpoints support the Local Network Hosting feature.

### 1. Get Access URL
**GET** `/api/network/access-url`

Returns the URL other devices on the LAN should use. The port is the integrated HTTPS proxy's effective bound port — 443 when bindable, otherwise 8443 after the automatic cross-platform fallback (Linux/macOS/Windows); the response always uses `https` (server mode is always TLS). `backend_port` is informational — the FastAPI backend binds `127.0.0.1` and is not reachable from the LAN.

**Response (TLS on):**
```json
{
  "host": "192.168.1.50",
  "port": 8443,
  "backend_port": 8001,
  "ports": { "access": 8443, "backend": 8001 },
  "url": "https://192.168.1.50:8443"
}
```

### 2. Get Active Connections
**GET** `/api/network/connections`

Returns a list of currently connected devices for the Network Topology map.

**Response:**
```json
[
  {
    "id": "ws_123456",
    "type": "websocket",
    "ip": "192.168.1.102",
    "device_type": "mobile",
    "username": "Guest (Connecting...)",
    "connected_at": 1700000000.0
  }
]
```

### 3. Get Network Status
**GET** `/api/network/status`

Returns the real runtime state of LAN hosting — whether the integrated HTTPS proxy actually bound and on which port (after any 443->8443 fallback), the resulting LAN URL, and the last bind error if it failed. The Local Network status dot in the UI reads this.

**Response:**
```json
{
  "enabled": true,
  "tls": true,
  "host": "192.168.1.50",
  "configured_https_port": 443,
  "effective_https_port": 8443,
  "proxy_bound": true,
  "error": null,
  "url": "https://192.168.1.50:8443"
}
```

`effective_https_port` is the port the proxy actually bound; `proxy_bound`/`error` report whether binding succeeded (e.g. `error` is set with `proxy_bound: false` when the port is taken or blocked).

### 4. Get WebSocket Config
**GET** `/api/network/ws-config`

Tells the caller which WebSocket transport to use; the answer differs per client so the same frontend build works on the desktop and over the LAN. Three branches:

- TLS off -> `{ "useWss": false, "port": 8001 }` (plain backend port).
- TLS on, request carries `X-Forwarded-Proto: https` (a LAN client behind the proxy) -> `{ "useWss": true, "port": <effective proxy port> }`.
- TLS on, no such header (the local desktop on `http://127.0.0.1:3000`) -> `{ "useWss": false, "port": 8005 }` — the internal plain channel, since the desktop's QtWebEngine rejects the proxy's self-signed certificate.
