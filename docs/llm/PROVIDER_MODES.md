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
| `self.provider` | `local` vs an API provider (`openai`/`anthropic`/`google`/`deepseek`/`openrouter`) | config `provider`; `agent.py.__init__` |
| `self.api_backend` / `self.use_server` / `self.llm` | the active backend (API / llama-server 8080 / in-process library) | `load_model()` (`agent.py`) |
| `APIProvider.provider_name` | which API provider inside `api_backend.py` | `APIBackendManager` |
| `self.is_gemma_local` / `self.model_mode` | local Gemma (any version) / `"gemma4"` \| `"gemma3n"` \| `None` | `agent.py.__init__` |

Backend selection itself is documented in [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#which-backend-is-used).

---

## API providers

All API providers go through `vaf/core/api_backend.py`. `openai`, `deepseek` and `openrouter` share the
OpenAI-compatible `OpenAIProvider` (differing only by `base_url`); `anthropic` and `google` use their
own SDK provider classes.

| Provider | What is provider-specific | Where | Detail doc |
|----------|---------------------------|-------|-----------|
| **DeepSeek** | `reasoning_content` is streamed alongside `content`, and must be passed back as a **separate field** on the next call (else `400`); answer is often in `reasoning_content` only | `api_backend.py` (stream ~87-133), `_prepare_messages` (`agent.py`), `clean_history` (`coder.py`) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#deepseek-reasoning_content-in-conversation-history) |
| **DeepSeek** | `base_url = https://api.deepseek.com/v1` | `api_backend.py:443` | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **DeepSeek** | `deepseek-auto` resolves to `deepseek-v4-pro` (sub-agent) / `deepseek-v4-flash` (default); 1M-token context | `api_backend.py` (~488-500, 547) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **DeepSeek** | deprecated model names (`deepseek-chat`/`-coder`/`-reasoner`) auto-migrate to `deepseek-v4-flash` on config load; `-reasoner` also dropped because it rejects `tool_choice` | `config.py` (~368-374), `coder.py` (`tool_choice` downgrade) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#deepseek-tool_choice-restrictions) |
| **Anthropic** | own SDK; tool schemas converted to Anthropic's `tools` shape | `api_backend.py` (~168-236) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **Google (Gemini)** | own `google.generativeai` SDK | `api_backend.py` (~285+) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **OpenRouter** | OpenAI-compatible via `base_url`; provider-specific request handling | `api_backend.py` (~444, 583) | [API_INTEGRATION.md](API_INTEGRATION.md) |
| **All API** | a stale local GGUF `model` value falls back to `api_model_<provider>`; context defaults to 128K when `provider != local` | `api_backend.py`, `agent.py` (~248, 2374) | [API_INTEGRATION.md](API_INTEGRATION.md) |

Available models per provider are fetched live — see [DYNAMIC_MODEL_SELECTION.md](DYNAMIC_MODEL_SELECTION.md).
Switching provider (Local ↔ API) and its memory handling — see [MODEL_AND_PROVIDER_SWITCHING.md](MODEL_AND_PROVIDER_SWITCHING.md).

---

## Local models (provider = `local`)

| Area | Behavior | Where | Detail doc |
|------|----------|-------|-----------|
| Default model | `model: "auto"` → `unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-UD-Q8_K_XL.gguf` | `gpu_detection.recommended_default_model`, `agent.py`, `backend.py.get_model_path` | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#default-local-model-model-auto) |
| Server launch | `--jinja` (the GGUF template parses tool calls), KV cache `q8_0`/`q4_0`, `n_ctx` floor 32768 | `backend.py` (~600-650) | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#context-window-configuration) |
| CUDA | auto-install of CUDA `llama-cpp-python` only on the in-process library path (`auto_install_gpu`, default `true`) | `agent.py.load_model` | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#cuda-auto-install-nvidia-gpu-without-cuda) |
| Server vs library | `force_server` / Windows default / `use_server` | `agent.py.load_model` | [LLM_BACKEND_FACTS.md](LLM_BACKEND_FACTS.md#when-is-server-8080-vs-library-used) |
| OpenAI-compatible URL (browser agent / `APIBackendManager` local) | Defaults to VAF's own llama-server via `get_llama_server_url("/v1")` (port 8080, Docker/env-aware) — **not** Ollama's `:11434`. The browser agent's `VAFLLMBridge` uses this path; the old 11434 default made it fail with a connection error. An explicit `local_api_url` (e.g. a real Ollama) overrides. | `api_backend.py` `_create_provider`, `browser_agent.py` `VAFLLMBridge` | — |
| Single system turn | For local models, **leading** system messages are merged into ONE system turn at the front; a system message that appears **mid-conversation** (a runtime nudge: empty-retry, loop block, plan-required, correction) is converted to a **user turn in place**. Strict local chat templates (Qwen, Gemma) reject a non-leading system turn (`System message must be at the beginning`) — and hoisting a trailing nudge to the front would leave the turn ending on an assistant message, which Qwen rejects as `Assistant response prefill is incompatible with enable_thinking`. Converting mid-run nudges to user turns fixes both. API providers keep multiple system messages. | `agent.py` `_consolidate_system_messages` / `_prepare_messages` | — |
| Reasoning models — internal LLM calls (`query_llm`) | The local server splits output into `reasoning_content` (chain-of-thought) and `content` (answer); `query_llm` reads `content`. A reasoning model (e.g. Qwen) that exhausts a small `max_tokens` **while still reasoning** leaves `content` empty (`finish_reason="length"`) — this is a token-budget issue, **not** a reason to disable thinking. Mitigations: generous `max_tokens` + real `timeout` on internal calls (e.g. web_search synthesis), and `query_llm` falls back to `reasoning_content` (logging `finish_reason`/`reasoning_len`) instead of returning empty. | `tools/base.py` `query_llm`, `tools/search.py` (synthesis) | — |
| Generation sampling (anti-loop) | The main local generation sends `repeat_penalty` (1.1), `top_p` (0.95), `top_k` (40) and a `max_tokens` cap (`max_generation_tokens`, 10000) — all config-driven. Without a repetition penalty (llama.cpp default is 1.0 = off) a reasoning model can degenerate into a verbatim loop, repeating the same paragraph until it fills the context (observed: a 60k-token `<think>` that then tripped the overflow). These are llama.cpp extensions and are sent **only** on the local `:8080` path; cloud APIs are untouched. | `agent.py` (local generation payload), `config.py` DEFAULTS | — |
| web_search time budget | `web_search` does a per-page LLM synthesis for each fetched page plus a final synthesis — slow with a reasoning model, so it used to hit the 120s tool-timeout (`tool_timeout_seconds`) and get hard-killed with **everything discarded**. It now self-bounds against that budget: snippets are always in the result, per-page synthesis stops once the per-page deadline passes, and the final synthesis is skipped (gathered answers returned raw) once the deadline passes — so it always returns what it has, well before the kill. Per-call synthesis budgets are small (per-page 600 tok / 30s, final 1000 tok / 35s) and lean on the `query_llm` `reasoning_content` fallback. | `tools/search.py`, `core/bounded_run.py` `agent_timeout_seconds` | — |

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
