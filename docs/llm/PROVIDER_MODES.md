# Provider & Model-Specific Behavior

This is the catalog of everything in VAF that branches on **which provider or model** is active. It
is an index: each entry says *what* is special, *where* it is implemented, and links to the doc with
the full detail. Add a row here whenever you introduce provider- or model-specific behavior.

## Principle: additive and gated

VAF keeps one shared code path and isolates every provider/model specialization behind a gate. New
support is **added** as a gated branch; the shared path and other providers are never altered.

The gates:

| Gate | Meaning | Set in |
|------|---------|--------|
| `self.provider` | `local` vs an API provider (`veyllo`/`openai`/`anthropic`/`google`/`deepseek`/`openrouter`) | config `provider`; `agent.py.__init__` |
| `self.api_backend` / `self.use_server` / `self.llm` | the active backend (API / llama-server 8080 / in-process library) | `load_model()` (`agent.py`) |
| `APIProvider.provider_name` | which API provider inside `api_backend.py` | `APIBackendManager` |
| `self.is_gemma_local` / `self.model_mode` | local Gemma (any version) / `"gemma4"` \| `"gemma3n"` \| `None` | `agent.py.__init__` |

Backend selection itself is documented in [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#which-backend-is-used).

---

## API providers

All API providers go through `vaf/core/api_backend.py`. `veyllo`, `openai`, `deepseek` and `openrouter` share the
OpenAI-compatible `OpenAIProvider` (differing only by `base_url`); `anthropic` and `google` use their
own SDK provider classes.

| Provider | What is provider-specific | Where | Detail doc |
|----------|---------------------------|-------|-----------|
| **DeepSeek** | `reasoning_content` is streamed alongside `content`, and must be passed back as a **separate field** on the next call (else `400`); answer is often in `reasoning_content` only | `api_backend.py` (stream ~87-133), `_prepare_messages` (`agent.py`), `clean_history` (`coder.py`) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#deepseek-reasoning_content-in-conversation-history) |
| **DeepSeek** | `base_url = https://api.deepseek.com/v1` | `api_backend.py` (~798) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **DeepSeek** | `deepseek-auto` resolves to `deepseek-v4-pro` in a pro-context (`VAF_IN_WORKFLOW_TERMINAL`/`VAF_IN_AUTOMATION`/`VAF_COMPACTION_IN_PROGRESS`/`VAF_BACKGROUND_PRO`/`VAF_TOOL_MODEL=deepseek-auto`) / `deepseek-v4-flash` (default main chat); 1M-token context. The thinking run sets `VAF_BACKGROUND_PRO` so background runs use pro | `api_backend.py` (~837-849), `thinking_mode.py` (`_run_thinking_for_user`) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **DeepSeek** | `tool_choice="required"` and specific function-forcing are rejected with a 400 (both `v4-flash`/`v4-pro` are internally reasoning models). Universal downgrade to `"auto"` for every manager-routed caller; the HTTP-direct coder path keeps its own guard | `api_backend.py` (`APIBackendManager.chat_completion`, ~857-871), `coder.py` (`tool_choice` downgrade) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#deepseek-tool_choice-restrictions) |
| **DeepSeek** | deprecated model names (`deepseek-chat`/`-coder`/`-reasoner`) auto-migrate to `deepseek-v4-flash` on config load; `-reasoner` also dropped because it rejects `tool_choice` | `config.py` (~368-374) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#deepseek-tool_choice-restrictions) |
| **OpenAI** | reasoning models (`o1`/`o3`/`o4` series, `gpt-5`) use `max_completion_tokens` instead of `max_tokens` and omit `temperature` (only the default is accepted) and `parallel_tool_calls`; the gpt-4o family is unchanged. Direct OpenAI API only — gated on `provider_name == "openai"` | `OpenAIProvider._is_reasoning_model` / `chat_completion` (`api_backend.py`) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **Anthropic** | native Messages API (`anthropic` SDK). OpenAI tool history (`assistant.tool_calls` + `role:"tool"`) is converted to `tool_use`/`tool_result` content blocks; leading system messages merge into the top-level `system` and mid-run nudges become user turns; `temperature` is omitted when thinking is on or the model removed sampling (Opus 4.7+/Fable); adaptive thinking is surfaced as `<think>` (`anthropic_thinking`); the system prompt is prompt-cached (`anthropic_prompt_cache`); `stop_reason` refusal/pause_turn is handled; raw assistant blocks are replayed via the `_anthropic_blocks` history key so a thinking tool loop preserves signed thinking blocks | `AnthropicProvider` (`api_backend.py`), `_prepare_messages`/`chat_step` (`agent.py`) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **Google (Gemini)** | native `google-genai` SDK (the deprecated `google-generativeai` package is no longer used). OpenAI tool history is converted to `function_call` / `function_response` parts; system consolidation as for Anthropic; `tool_choice` maps to `FunctionCallingConfig`; thinking is surfaced as `<think>` on Gemini 2.5/3.x (`google_thinking`); images use `Part.from_bytes` | `GoogleProvider` (`api_backend.py`) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **OpenRouter** | OpenAI-compatible via `base_url`; uses dotted model ids (`anthropic/claude-sonnet-4.6`); **excluded** from the OpenAI reasoning-param gating because OpenRouter normalizes around `max_tokens` for every model; context window and model list are fetched live from `openrouter.ai/api/v1/models` | `OpenAIProvider` (base_url), `get_model_context_window` (`api_backend.py`) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **Veyllo** | OpenAI-compatible via `base_url` = `https://api.veyllo.app/v1` (configurable via `veyllo_base_url`); `veyllo-chat` is multimodal, so one model serves both chat and image input — no separate vision provider is required when Veyllo is primary | `OpenAIProvider` (base_url), `api_backend.py` factory | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **All API** | per-provider default and fallback model lists come from one source — `Config.PROVIDER_MODELS` (`config.py`) — read by every call site and served to the web UI via `GET /api/provider-models`; a stale local GGUF `model` value falls back to `api_model_<provider>`; context window is resolved per model family (Claude/Gemini 1M, Haiku 4.5 200K), live-fetched for OpenRouter, else 128K | `config.py` `PROVIDER_MODELS`/`get_default_model`, `api_backend.py`, `config_routes.py` | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **All API / local (concurrency)** | concurrent multi-user execution is capped per provider — API providers up to `max_parallel_api_workers` (default 5) effective workers, `provider=local` to `max_parallel_local_workers` (default 2, clamped to the llama-server `--parallel` slots) to avoid VRAM exhaustion; default is 1 (serialized). 429/rate-limit retries (honoring `Retry-After`) apply to every provider | `headless_runner.py` (worker spawn), `api_backend.py` (`_with_retry`/`_is_retryable_error`) | [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md), [API_INTEGRATION.md](API_INTEGRATION.md) |
| **All API (chat while a sub-agent runs)** | while a sub-agent genuinely runs for the session, the main agent gets a SUB-AGENT ACTIVE prompt block (keep replies light, no re-delegation, hands off the workspace) and the UI shows a passive "you can keep chatting" hint — gated on the MAIN provider != `local` AND an initialized `api_backend` (closes the API-init-failure fallback hole), plus NOT `_background_run` (automations) and NOT front-office. On `provider=local` the block/hint are simply absent (chatting itself is not newly blocked): the single llama server would serve two inferences at once. Hybrid main=local + subagent=API stays gated OFF (the gate keys on the main provider — it is the main agent's extra inference that hits the llama server). Kill-switch: `subagent_concurrent_chat_enabled` | `system_prompt.py` (`<subagent_active>` block), `agent.py` (`get_live_session_subagents`, re-delegation guard), `web_server.py` (scoped stop) | [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md), [SUBAGENT_IPC.md](../agents/SUBAGENT_IPC.md) |

Available models per provider are fetched live — see [DYNAMIC_MODEL_SELECTION.md](DYNAMIC_MODEL_SELECTION.md).
Switching provider (Local ↔ API) and its memory handling — see [MODEL_AND_PROVIDER_SWITCHING.md](MODEL_AND_PROVIDER_SWITCHING.md).

---

## Local models (provider = `local`)

| Area | Behavior | Where | Detail doc |
|------|----------|-------|-----------|
| Default model | `model: "auto"` → **VRAM-adaptive Qwen3.5**: 4B (`unsloth/Qwen3.5-4B-GGUF`) at ≤ 10 GB, 9B (`unsloth/Qwen3.5-9B-GGUF`) above; **quant leaves room for desktop + KV cache** — 4B: Q4_K_M (<8 GB) / Q6_K (8) / UD-Q8_K_XL (9–10); 9B: Q5_K_M (11) / Q6_K (12–15) / Q8_0 (16–19) / UD-Q8_K_XL (20–23) / BF16 (≥24) | `gpu_detection.recommended_default_model`, `agent.py`, `backend.py.get_model_path` | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#default-local-model-model-auto) |
| Server launch | `--jinja` (the GGUF template parses tool calls), KV cache `q8_0`/`q4_0`, `n_ctx` floor 32768 | `backend.py` (~600-650) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#context-window-configuration) |
| CUDA | auto-install of CUDA `llama-cpp-python` only on the in-process library path (`auto_install_gpu`, default `true`) | `agent.py.load_model` | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#cuda-auto-install-nvidia-gpu-without-cuda) |
| Server vs library | `force_server` / Windows default / `use_server` | `agent.py.load_model` | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#when-is-server-8080-vs-library-used) |
| OpenAI-compatible URL (browser agent / `APIBackendManager` local) | Defaults to VAF's own llama-server via `get_llama_server_url("/v1")` (port 8080, Docker/env-aware) — **not** Ollama's `:11434`. The browser agent's `VAFLLMBridge` uses this path; the old 11434 default made it fail with a connection error. An explicit `local_api_url` (e.g. a real Ollama) overrides. | `api_backend.py` `_create_provider`, `browser_agent.py` `VAFLLMBridge` | — |
| Single system turn | For local models, **leading** system messages are merged into ONE system turn at the front; a system message that appears **mid-conversation** (a runtime nudge: empty-retry, loop block, plan-required, correction) is converted to a **user turn in place**. Strict local chat templates (Qwen, Gemma) reject a non-leading system turn (`System message must be at the beginning`) — and hoisting a trailing nudge to the front would leave the turn ending on an assistant message, which Qwen rejects as `Assistant response prefill is incompatible with enable_thinking`. Converting mid-run nudges to user turns fixes both. API providers keep multiple system messages. | `agent.py` `_consolidate_system_messages` / `_prepare_messages` | — |
| Reasoning models — internal LLM calls (`query_llm`) | The local server splits output into `reasoning_content` (chain-of-thought) and `content` (answer); `query_llm` reads `content`. A reasoning model (e.g. Qwen) that exhausts a small `max_tokens` **while still reasoning** leaves `content` empty (`finish_reason="length"`) — this is a token-budget issue, **not** a reason to disable thinking. Mitigations: generous `max_tokens` + real `timeout` on internal calls (e.g. web_search synthesis), and `query_llm` falls back to `reasoning_content` (logging `finish_reason`/`reasoning_len`) instead of returning empty. | `tools/base.py` `query_llm`, `tools/search.py` (synthesis) | — |
| Generation sampling (anti-loop) | The main local generation sends `repeat_penalty` (1.1), `top_p` (0.95), `top_k` (40) and a `max_tokens` cap (`max_generation_tokens`, 10000) — all config-driven. Without a repetition penalty (llama.cpp default is 1.0 = off) a reasoning model can degenerate into a verbatim loop, repeating the same paragraph until it fills the context (observed: a 60k-token `<think>` that then tripped the overflow). These are llama.cpp extensions and are sent **only** on the local `:8080` path; cloud APIs are untouched. | `agent.py` (local generation payload), `config.py` DEFAULTS | — |
| web_search time budget | `web_search` does a per-page LLM synthesis for each fetched page plus a final synthesis — slow with a reasoning model, so it used to hit the 120s tool-timeout (`tool_timeout_seconds`) and get hard-killed with **everything discarded**. It now self-bounds against that budget: snippets are always in the result, per-page synthesis stops once the per-page deadline passes, and the final synthesis is skipped (gathered answers returned raw) once the deadline passes — so it always returns what it has, well before the kill. Per-call synthesis budgets are small (per-page 600 tok / 30s, final 1000 tok / 35s) and lean on the `query_llm` `reasoning_content` fallback. | `tools/search.py`, `core/bounded_run.py` `agent_timeout_seconds` | — |
| Text tool-call fallback | A reasoning model sometimes writes a tool call **as text inside `<think>`** instead of making a native call, so the server never converts it and the call is silently dropped (observed on Qwen: `update_working_memory` written this way → the plan was never set → the `[PLAN REQUIRED]` gate looped). When the native parse finds nothing, VAF searches the content **and** the reasoning for known text formats — JSON `<tool_call>{…}</tool_call>`, ` ```json `, `name(args)`, the Gemma-4 native format, and the Qwen/Hermes `<tool_call><function=NAME><parameter=KEY>VALUE</parameter>…</function></tool_call>` form — and executes any match (filtered to known tool names). | `agent.py` `_parse_qwen_tool_calls` / `_parse_gemma4_tool_calls` + the fallback block | — |

Tool input validation & repair (provider-agnostic, but most relevant for weak local models): arguments that are valid JSON but the wrong shape — a bare string for an array, a stringified array, `null` on an optional field — are validated against the tool schema and repaired before dispatch; unrepairable cases return a localized `Tool Error:`. See [TOOL_INPUT_REPAIR.md](../agents/TOOL_INPUT_REPAIR.md).

### Gemma local mode (`model_mode` = `gemma4` / `gemma3n`)

Detected once in `agent.py.__init__` (`is_gemma_local`, `model_mode`), so the parser, message prep and
display all read one source. All Gemma handling is gated; non-Gemma local models and all API providers
are unaffected.

| Aspect | Gemma 4 | Gemma 3n | Where |
|--------|---------|----------|-------|
| System prompt | native `system` role (one `<\|turn>system` block; memory context merged into it, not a second system turn) | merged into the first user turn (no native system role) | `_prepare_messages` (`agent.py`) |
| Tool-call parsing | server normally converts `<\|tool_call>call:NAME{…}<tool_call\|>` via `--jinja`; an **additive** fallback parser (`_parse_gemma4_tool_calls`) catches an unconverted raw call. Never replaces the shared parser | — (`tool_code` block: TODO) | `agent.py` (fallback parser) |
| Thinking tags | doubled/nested `<think><think>…` from the model are collapsed to one clean block (UI parser + `_clean_reasoning`); a no-op for well-formed output | same | `web/app/page.tsx` (`parseThinkBlocks`), `_clean_reasoning` (`agent.py`) |

---

## Cross-cutting toggles (not provider-specific, but affect tool/reasoning behavior)

| Config | Default | Effect |
|--------|---------|--------|
| `false_promise_detection_enabled` | `false` | Forced retry when a model claims a tool but emits none. Off by default (caused retry loops / false positives, esp. on weak local models). Applies to all models when on. |
| `result_grounding_enabled` | `true` | Bounces a reply that asserts a tool OUTCOME the turn's actual tool results don't support. See [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md). |

---

## See also

- [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md) — backend selection, local model/server facts, DeepSeek reasoning round-trip
- [API_INTEGRATION.md](API_INTEGRATION.md) — API keys, providers, the mixed local/API guardrail
- [DYNAMIC_MODEL_SELECTION.md](DYNAMIC_MODEL_SELECTION.md) — live model discovery per API provider
- [MODEL_AND_PROVIDER_SWITCHING.md](MODEL_AND_PROVIDER_SWITCHING.md) — switching Local ↔ API at runtime
