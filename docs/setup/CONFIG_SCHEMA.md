# Configuration Reference

Authoritative reference for VAF's configuration keys. The single source of truth is the
`DEFAULTS` dict in [vaf/core/config.py](../../vaf/core/config.py); this page organizes those
keys by area. Defaults shown here match `Config.DEFAULTS` (274 keys).

## How configuration is set

There are two ways to supply configuration, and they compose:

1. **On disk** — `~/.vaf/config.json` (in Docker: the `VAF-Config` volume). Written by the
   setup wizard, the Settings UI, and the CLI. Loaded by `Config.load()`.
2. **Programmatically** — when embedding VAF as a library:

   ```python
   from vaf import Agent
   agent = Agent(config={"provider": "openai", "api_key_openai": "sk-..."})
   ```

   The dict is merged on top of the on-disk config for that `Agent` instance only; nothing
   is written to `~/.vaf/config.json`. See [EMBEDDING.md](../EMBEDDING.md).

> **API keys — disk vs. programmatic.** On disk, `api_key_*` values are Base64-encoded
> (light obfuscation, not encryption). When you pass an `api_key_*` **programmatically**
> via `Agent(config={...})`, give the **raw** key (`"sk-..."`) — it is used as-is and never
> Base64-decoded.

The keys in the "Essential for embedding" section below are the ones most embedders
need; everything else has a sensible default.

---

## Essential for embedding

| Key | Default | Meaning |
|-----|---------|---------|
| `provider` | `"local"` | LLM provider: `local`, `veyllo`, `openai`, `anthropic`, `deepseek`, `google`, `openrouter`. |
| `model` | `"auto"` | Local GGUF model. `"auto"` = VRAM-adaptive default, or set `"repo/file.gguf"`. Ignored for API providers. |
| `api_key_<provider>` | `""` | API key for the chosen provider (e.g. `api_key_openai`). Raw when set programmatically; Base64 on disk. |
| `api_model_<provider>` | per provider (see below) | Model name for the API provider (e.g. `api_model_openai`). |
| `n_ctx` | `32768` | Context window in tokens. Values below 32768 are clamped up to it. |
| `temperature` | `0.7` | Sampling temperature (API + local). |
| `gpu_layers` | `-1` | Local model GPU offload layers. `-1` = all; `0` = CPU only. |
| `auto_start_local_server` | `True` | Start the local llama-server automatically. Set `False` when using only an API provider. |
| `router_max_tools` | `12` | Max tools handed to the model per turn (1–100). Lower = faster inference. |

Default API models (from `Config.PROVIDER_MODELS`):

| Provider | `api_model_*` default |
|----------|-----------------------|
| veyllo | `veyllo-chat` |
| openai | `gpt-4o` |
| anthropic | `claude-sonnet-4-6` |
| deepseek | `deepseek-v4-flash` |
| google | `gemini-2.5-flash` |
| openrouter | `anthropic/claude-sonnet-4.6` |

Minimal API-provider embedding example:

```python
from vaf import Agent
agent = Agent(config={
    "provider": "openai",
    "api_key_openai": "sk-...",      # raw key
    "api_model_openai": "gpt-4o-mini",
})
print(agent.run("In one sentence, what is Python?"))
```

---

## Providers & models

| Key | Default | Meaning |
|-----|---------|---------|
| `api_key_veyllo` | `""` | API key for the first-party Veyllo API. |
| `veyllo_base_url` | `"https://api.veyllo.app/v1"` | Veyllo API base URL (OpenAI-compatible wire protocol); override for staging or self-host. |
| `vision_mode` | `"description_tool"` | How attached images reach the model. `description_tool` (default): the main model is text-only — an image is run once through the vision backend to a base description that is injected as text, and the model calls the `analyze_image` tool to inspect it on demand (token-efficient; works even with a non-vision main provider). `inline_multimodal`: legacy — send the raw image straight to a multimodal main model. See the vision section in [API_INTEGRATION.md](../llm/API_INTEGRATION.md). |
| `vision_description_max_tokens` | `1024` | Output bound for the one-time base description and for each `analyze_image` call. |
| `vision_provider` | `""` | Provider used for vision (base description + `analyze_image`). Empty = use the main provider if it is vision-capable, else none. Set an API id (e.g. `google`) to use a different provider for seeing, or `local`: the llama server launches with the model's mmproj projector and sees images itself (no cloud). |
| `vision_model` | `""` | Model for the vision provider; empty = that provider's default. Unused for `local` (the loaded GGUF sees). |
| `vision_local_mmproj` | `""` | Local vision projector ref `owner/repo/file.gguf` (admin-only: server launch argument). Empty = derived from the model's known repo (`mmproj-F16.gguf`, e.g. from `unsloth/Qwen3.5-4B-GGUF`). |
| `voice_agent_provider` | `""` | LLM lane for the live-call voice agent (admin-only). Empty = ride the main provider (local main = time-share the one llama server). `local` = a dedicated local voice GGUF: the ONE server swaps models (voice model during the call, main model while a delegated task runs) - never two servers. Any API provider id = the call runs on that API regardless of the main provider. |
| `voice_agent_model` | `""` | For `local`: a downloaded model filename from `models/`, picked in Settings > Voice (empty = the recommended default in `voice_model.py`, Gemma 4 E4B, fetched on selection). A full GGUF ref `owner/repo/file.gguf` is still accepted (back-compat). For an API provider: model name (empty = provider default). |
| `vision_image_max_edge` | `2000` | Downscale an image before send if its longest edge exceeds this (px); prevents provider 500s on full-res photos and cuts tokens. Smaller images are sent unchanged. |
| `vision_image_jpeg_quality` | `85` | Re-encode quality (1–95) used when an image is downscaled. |
| `api_retry_attempts` | `2` | VAF-level retries on a transient error at request initiation — **HTTP 429 (rate limit)**, 5xx, timeout or connection drop — for **all** providers (atop each SDK's own retries; only before any token is streamed, so output is never duplicated). Admin-only. |
| `api_retry_after_max` | `30` | Cap (s) on a honored `Retry-After` header from a 429, so a large/hostile value cannot stall a worker. Admin-only. |
| `api_timeout_connect` | `20.0` | OpenAI-compatible client connect timeout (s). |
| `api_timeout_write` | `120.0` | Request-upload (body) timeout (s) — bounds large image uploads. |
| `api_timeout_read` | `600.0` | Read timeout (s); kept generous so long reasoning streams are not cut off. |
| `api_timeout_pool` | `20.0` | Connection-pool acquire timeout (s). |
| `anthropic_prompt_cache` | `True` | Anthropic only: send the system prompt as a `cache_control: ephemeral` block so the stable prefix is cached across multi-turn / tool loops (cost saver). Read with an inline default, not part of `DEFAULTS`. See [API_INTEGRATION.md](../llm/API_INTEGRATION.md). |
| `anthropic_thinking` | `True` | Anthropic only: adaptive (extended) thinking on supported models (reasoning streams wrapped in `<think>` tags); ignored on models without thinking support. Read with an inline default, not part of `DEFAULTS`. |
| `local_api_url` | `""` | OpenAI-compatible endpoint for the API-backend consumers of provider `local` (browser agent, local vision, cloud-to-local failover), e.g. an Ollama/vLLM URL. Empty = VAF's own llama-server. Does NOT redirect the main chat loop (see [EMBEDDING.md](../EMBEDDING.md)). Read with an inline default, not part of `DEFAULTS`. |
| `subagent_provider` | `"inherit"` | Provider for sub-agents; `inherit` = same as main. |
| `subagent_use_separate_provider` | `False` | Use `subagent_provider` instead of inheriting. |
| `subagent_model` | `""` | Model for tools/workflows (hybrid mode); empty = same as main chat. |
| `subagent_concurrent_chat_enabled` | `True` | Kill-switch for chat-while-a-sub-agent-runs (the SUB-AGENT ACTIVE prompt block). Renders only in API mode regardless (code gate on the main provider + an initialized API backend). Admin-only via the `subagent_` prefix. |

## Failover & resilience

Automatic provider failover: if the primary provider is unreachable or errors out **before the first token**, the request is retried down a chain of fallback providers. Once a real token has streamed, no switch happens (it would duplicate output). Off by default — behaviour is unchanged unless `failover_level` is set. Configured in the UI under Settings → Advanced → Failover. All keys are admin-only.

| Key | Default | Description |
|-----|---------|-------------|
| `failover_level` | `"off"` | Resilience level: `off` (primary only), `basic` (→ local model), `balanced` (→ backup API → local), `maximum` (full chain, more aggressive triggers). |
| `failover_backup_provider` | `""` | Provider id used as the backup API link (e.g. `anthropic`, `openai`). Empty = no backup-API link. Skipped automatically if its API key is missing. |
| `failover_backup_model` | `""` | Model for the backup link; empty = that provider's default. |
| `failover_local_model` | `""` | GGUF filename for the local link; empty = auto. |
| `failover_timeout_s` | `30` | First-token deadline (s) before failing over to the next link; `0` = no extra deadline (rely on the provider's own timeout). |
| `failover_triggers` | `[]` | Subset of `["timeout","rate_limit","server_error"]` that may trigger a switch; empty = any error. Connection/unknown errors always switch. |
| `failover_return_to_primary` | `True` | After a fallback, prefer the primary again on the next request; when off, stay on the working link until it also fails. |

## Local generation (llama-server)

These are sent only on the local path; cloud APIs ignore them.

| Key | Default | Meaning |
|-----|---------|---------|
| `auto_install_gpu` | `True` | On NVIDIA without CUDA, auto-install a CUDA build (no prompt). `False` = stay on CPU. |
| `n_parallel` | `0` | Parallel decode slots. `0` = auto by VRAM. |
| `llama_cache_ram` | `4096` | Prompt-cache size (MB). `0` = off, `-1` = auto. |
| `repeat_penalty` | `1.1` | Repetition penalty (anti-loop). |
| `top_p` | `0.95` | Nucleus sampling. |
| `top_k` | `40` | Top-k sampling. |
| `max_generation_tokens` | `10000` | Per-call output cap on local generation. |
| `model_unload_idle_minutes` | `30` | Unload the local model after this idle time. |
| `parallel_main_workers` | `1` | Concurrent main-agent workers (admin-only). `1` = serialized (default). When > 1, the effective count is clamped per provider (see the two keys below) and different users' turns run concurrently while a single user's turns stay serialized. Pair with `queue_policy: weighted_fair` for lane fairness. |
| `max_parallel_api_workers` | `5` | Effective worker cap for API providers (admin-only). |
| `max_parallel_local_workers` | `2` | Effective worker cap for `provider=local` (admin-only); also clamped to the llama-server `--parallel` slots (`n_parallel`) to avoid VRAM exhaustion. |

## Tool router & agent guardrails

| Key | Default | Meaning |
|-----|---------|---------|
| `action_tag_enabled` | `False` | The `<Action>` declaration tag. Off by default (small models stall on it). |
| `false_promise_detection_enabled` | `False` | Retry when a model claims a tool but emits none. Off (caused retry loops). |
| `empty_response_retry_enabled` | `False` | Local empty-response snapshot+retry. Off (noisy). |
| `plan_gate_enabled` | `True` | Block state-changing tools until a plan exists in working memory. |
| `plan_gate_max_blocks` | `3` | Blocks before proceeding without a plan. |
| `plan_step_reminder_enabled` | `True` | Surface the current plan step each turn. |
| `plan_without_tasks_reminder_enabled` | `True` | Nudge to break a plan into tracked tasks. |
| `plan_drift_nudge_enabled` | `True` | Soft hint when a later task is marked done before an earlier one. |
| `anti_spin_enabled` | `True` | Stop repeated bookkeeping-tool churn without real work. |
| `anti_spin_max_planning_calls` | `4` | Consecutive plan/intent calls before nudging. |
| `nonprogress_max_turns` | `6` | Consecutive read-only/verify-only tool turns (`list_*`/`read_*`/`get_*`, `list_automations`, …; not `web_search`/`memory_search`) before a nudge then a forced text answer. Catches a "verify forever" loop; any mutating/producing tool resets it. |
| `chat_step_wall_clock_seconds` | `3600` | Main-loop wall-clock **backstop** (1h): a single user turn can never grind past this (checked at each tool-turn boundary), independent of tool count or provider speed. Deliberately generous — never aborts legitimate long work; the no-progress guard + per-tool timeouts stop the common case far earlier. The 75-turn cap is a secondary guard. |
| `workflow_generation_timeout_seconds` | `30` | create_automation: time-bound the inline LLM workflow pre-generation (fast-fail to robust prompt-based execution). |
| `result_grounding_enabled` | `True` | Bounce a reply that claims a tool outcome the turn's results don't support. |
| `result_grounding_max_retries` | `2` | Corrections before proceeding anyway. |
| `team_await_enabled` | `True` | When a reply claims completion while a sub-agent still runs, keep the reply (never erased) and append a "work not finished" note for the next turn. |
| `autocontinue_pending_tasks_enabled` | `True` | Keep working within the turn while tasks remain pending. |
| `autocontinue_question_classifier_enabled` | `True` | LLM check whether a reply is a blocking question before auto-continuing. |
| `automation_run_timeout_seconds` | `600` | Wall-clock bound for a prompt-based automation run. On timeout the runner waits a bounded grace for the abandoned worker to finish (then treats it as a normal completion); otherwise one honest timeout note is delivered - never a partial result or a wrapped file. |
| `proactive_reply_mutation_gate_enabled` | `True` | A reply to a background question that is not a clear affirmative cannot mutate stored state or delegate destructive work (confirm-style block). |
| `ask_first_drain_gate_enabled` | `True` | While the agent awaits the user's answer to its own question, background drain turns cannot start new write-level tools or delegations. |
| `task_overwrite_guard_enabled` | `True` | Confirm before replacing the whole task list while steps are pending. |
| `task_overwrite_confirm_window_seconds` | `120` | Re-call within this window = confirmed. |
| `workflow_step_validation_enabled` | `True` | LLM check that a workflow step met its goal. |
| `workflow_step_validation_max_retries` | `3` | Retries before accepting the result. |
| `channel_tools_unrestricted` | `True` | Admin-only. When `True`, messaging-channel sessions (Telegram/WhatsApp/Discord) get the same tools as the main agent — `channel_restrictions` and the per-call confirmation gate are lifted. The `admin_only` check and the channel whitelist (`paired_only` by default) still apply. On by default; set to `False` to restrict channel sessions to non-channel-restricted tools. |
| `skills_rescan_interval_hours` | `5` | Periodic skill re-scan (post-install tamper detection): every N hours the security scanner re-checks all installed skills on disk, updates their manifest scan blocks, and raises a security event on the Overview dashboard when a skill's risk level worsened. `0` disables. |

## Sub-agents & timeouts

| Key | Default | Meaning |
|-----|---------|---------|
| `sub_agents_in_separate_terminals` | `True` | Run each sub-agent in its own terminal window. |
| `subagent_timeout_enabled` | `True` | Enable sub-agent timeouts. |
| `subagent_timeout_minutes` | `120` | Legacy IPC zombie-cleanup window. |
| `subagent_timeout_seconds` | `300` | Hard cap for a research/coding/document step. |
| `workflow_agent_step_timeout_seconds` | `1800` | Worst-case cap for a heavy agent step (coder/research/document) INSIDE a workflow - a floor over the generic cap, which killed a healthy coder mid-run at minute five. Dead children are caught much earlier by heartbeat liveness. |
| `subagent_liveness_timeout_seconds` | `60` | Kill a sub-agent after this long with no heartbeat (primary guard). |
| `tool_timeout_seconds` | `120` | Hard cap for a generic in-process tool call. |
| `librarian_timeout_seconds` | `60` | Hard cap for the filesystem/document agent. |
| `browser_timeout_seconds` | `1800` | Worst-case browser cap (liveness is the real guard). |
| `tool_stop_poll_seconds` | `0.5` | How often the bounded wait checks stop/deadline. |

## Memory & RAG

PostgreSQL (pgvector) + Redis back the memory system; both are optional for embedders.

| Key | Default | Meaning |
|-----|---------|---------|
| `memory_enabled` | `True` | Enable the self-learning RAG memory. |
| `memory_db_url` | `postgresql://vaf:...@localhost:5432/vaf_memory` | Memory DB DSN for per-user data. Default uses the owner role; set it to a non-superuser role (e.g. `vaf_app`) to enforce Row-Level Security on `memories` (see USER_ISOLATION.md). |
| `memory_db_owner_url` | `""` | Owner/superuser DSN for DDL, migrations and global stats. Empty falls back to `memory_db_url`; set it to the owner role (e.g. `vaf`) when `memory_db_url` is the non-superuser app role. |
| `memory_db_echo` | `False` | SQLAlchemy echo (debug). |
| `memory_embedding_model` | `all-MiniLM-L6-v2` | Sentence-transformer embedding model. |
| `memory_encryption_key` | `""` | Managed; memory-at-rest encryption key. |
| `memory_auto_capture` | `False` | Auto-store memories from conversation. |
| `memory_auto_connect_threshold` | `0.7` | Similarity to auto-link memories. |
| `memory_chunk_size` | `512` | Chunk size (tokens) for indexing. |
| `memory_chunk_overlap` | `50` | Chunk overlap. |
| `memory_rag_k` | `5` | Top-k memories retrieved per query. |
| `memory_rag_threshold` | `0.3` | Min similarity to include. |
| `memory_rag_refine_query` | `True` | LLM query refinement before search. |
| `memory_hybrid_enabled` | `True` | Hybrid vector + lexical retrieval. |
| `memory_hybrid_lexical_k` | `20` | Lexical candidates. |
| `memory_hybrid_lexical_min_score` | `0.05` | Min lexical score. |
| `memory_hybrid_lexical_scan_limit` | `400` | Lexical scan cap. |
| `memory_hybrid_rrf_k` | `60` | Reciprocal-rank-fusion constant. |
| `memory_compaction_enabled` | `True` | Compact long histories. |
| `memory_compaction_interval` | `15` | Turns between compaction checks. |
| `memory_compaction_max_tokens` | `4000` | Target size of a compaction summary. |
| `resume_compaction_enabled` | `True` | Compact on session resume. |
| `attachment_rag_*` | (12 keys) | Per-attachment RAG: `attachment_rag_enabled` (`True`), `attachment_rag_k` (`4`), `attachment_rag_threshold` (`0.28`), `attachment_rag_ttl_hours` (`24`), plus hierarchical/lexical/size tuning. See config.py. |

## Web search

| Key | Default | Meaning |
|-----|---------|---------|
| `web_search_cache_enabled` | `True` | Serve identical `web_search` queries from a short-lived cache. |
| `web_search_cache_ttl_seconds` | `900` | Cache lifetime (15 min). |
| `api_key_brave_search` | `""` | Brave Search key (used before scrape/DDG when set). |
| `api_key_google_search` | `""` | Google Programmable Search key. |
| `google_search_engine_id` | `""` | Google Programmable Search engine ID. |

## Git attribution

| Key | Default | Meaning |
|-----|---------|---------|
| `git_coauthor_enabled` | `True` | Append a `Co-authored-by` trailer to commits VAF authors itself (project versioning, coder final commit, GitHub file commits). User-initiated commits (`vaf git commit`) are never touched. Toggle from chat via the `set_git_coauthor` tool ("stop adding yourself as co-author"). |
| `git_coauthor_identity` | `VAF Agent <noreply@veyllo.app>` | Trailer identity in `Name <email>` form; an empty string disables the trailer. |

## MCP

| Key | Default | Meaning |
|-----|---------|---------|
| `mcp_native_tools_enabled` | `True` | Register each MCP server tool as a native tool at startup. |
| `mcp_discovery_timeout_seconds` | `5` | Per-batch MCP discovery deadline. |

## Document tools (Librarian)

| Key | Default | Meaning |
|-----|---------|---------|
| `librarian_auto_chunk_large_files` | `True` | Auto-chunk large documents. |
| `librarian_max_doc_size_mb` | `20` | Max generic document size. |
| `librarian_max_excel_size_mb` | `30` | Max Excel size. |
| `librarian_max_pdf_size_mb` | `50` | Max PDF size. |
| `librarian_max_text_size_kb` | `500` | Max plain-text size. |
| `librarian_pdf_max_pages_preview` | `50` | PDF preview page cap. |
| `document_conversion_docker_url` | `http://localhost:5005` | Gotenberg (Office→PDF) endpoint. |

## Speech (STT / TTS)

| Key | Default | Meaning |
|-----|---------|---------|
| `stt_enabled` | `False` | Legacy STT toggle (ORed with `speech_stt_enabled`; admin-only). |
| `speech_stt_enabled` | `False` | Enable speech-to-text (canonical key). |
| `speech_stt_engine` | `"docker"` | `docker` or `local` (faster-whisper). |
| `speech_stt_docker_url` | `http://localhost:5003` | STT container URL. |
| `speech_stt_whisper_model` | `"base"` | Local Whisper model size. |
| `speech_tts_enabled` | `False` | Enable text-to-speech. |
| `speech_tts_engine` | `"docker"` | TTS engine. |
| `speech_tts_docker_url` | `http://localhost:5002` | Default TTS container URL. |
| `speech_tts_docker_url_de` | `http://localhost:5002` | German TTS URL (optional). |
| `speech_tts_docker_url_en` | `http://localhost:5004` | English TTS URL (optional). |
| `speech_tts_docker_url_fr` | `http://localhost:5006` | French TTS URL (optional). |
| `speech_tts_chatterbox_url` | `http://localhost:4123` | Chatterbox-style HTTP TTS server (engine `chatterbox`). |
| `speech_tts_provider` | `""` | Cloud TTS provider: `""` (use the local engine), `elevenlabs`, or `openai`. Takes precedence over `speech_tts_engine`; falls back to the local engine on API errors. |
| `speech_tts_api_model` | `""` | Model for the cloud TTS provider (`""` = default: ElevenLabs `eleven_flash_v2_5`, OpenAI `gpt-4o-mini-tts`). |
| `speech_tts_api_voice` | `""` | Voice for the cloud TTS provider: ElevenLabs voice ID or OpenAI voice name (`""` = default). |
| `speech_stt_provider` | `""` | Cloud STT provider: `""` (use the local engine), `veyllo`, `elevenlabs`, or `openai`. Takes precedence over `speech_stt_engine`; falls back to the local engine on API errors. Seeded to `veyllo` the first time a Veyllo key is added (onboarding OR later in Settings) while no STT provider was chosen (`Config.apply_veyllo_stt_default`); an explicit later choice overwrites it. |
| `speech_stt_api_model` | `""` | Model for the cloud STT provider (`""` = default: Veyllo `veyllo-transcribe`, ElevenLabs `scribe_v2`, OpenAI `whisper-1`). |
| `api_key_elevenlabs` | `""` | ElevenLabs API key (speech only, not an LLM provider). Base64 on disk; redacted for non-admin reads. |
| `speaker_id_enabled` | `True` | Speaker identification kill-switch. Inert until a voice profile is enrolled (enrollment is the real opt-in; no model loads without a profile). |
| `speaker_id_threshold` | `0.60` | Cosine score at or above this labels the enrolled user. |
| `speaker_id_band` | `0.05` | Band below the threshold labeled "unsure" (triggers confirmation). |
| `speaker_id_confirmation_enabled` | `true` | On "unsure": ask the owner to confirm (main messenger, else web card). "No, that's NAME" stores a named third-party profile. |
| `voice_awareness_activity` | `0.5` | Voice reflex chime-in dial, `0.0`..`1.0` (quiet..active); a single control for how readily the agent chimes in on interesting OVERHEARD talk during a call; at `0.0` it only takes notes and never interrupts. Scales a local policy threshold only (no inference redirect, no billing), so it is user-writable. See [docs/agents/VOICE_REFLEX.md](../agents/VOICE_REFLEX.md). |
| `voice_awareness_topics` | `[]` | The owner's interest topics (list of short strings). A proactive chime-in must embedding-match one of these to fire, so an empty list means the agent never chimes in unprompted (conservative default). User-writable. |
| `speaker_id_adaptive_enabled` | `true` | Owner-approved adaptive learning: a YES answer to the confirmation (authenticated web/messenger channel) feeds the confirmed segment into the owner profile as an adaptive sample (similarity floor, 10-sample FIFO cap, enrollment centroid keeps 70% weight; re-enrollment resets all adaptive state). The voice itself can never trigger a profile write. |
| `tts_auto_speak` | `False` | Auto-speak replies. |

## Network & server mode

See [docs/setup/SERVER_MODE.md](SERVER_MODE.md) and
[docs/setup/NETWORK_FEATURES.md](NETWORK_FEATURES.md).

| Key | Default | Meaning |
|-----|---------|---------|
| `server_mode` | `False` | Run as a standalone server. |
| `server_persistence_enabled` | `False` | Persist the server process. |
| `server_idle_timeout` | `15` | Idle minutes before idle handling. |
| `local_network_enabled` | `False` | Allow LAN access. |
| `local_network_force_enabled` | `False` | Force-enable LAN access. |
| `local_network_firewall_enabled` | `True` | Manage OS firewall rules. |
| `local_network_port` | `8001` | Backend API port. |
| `local_network_port_frontend` | `3000` | Web UI port. |
| `local_network_https_port` | `443` | HTTPS port. |
| `local_network_tls_enabled` | `False` | Enable TLS. |
| `local_network_ssl_cert` / `_ssl_key` | `""` | TLS cert/key paths (auto-generated if empty). |
| `local_network_jwt_secret` | `""` | Managed; JWT signing secret. |
| `local_network_jwt_expiry_hours` | `24` | JWT lifetime. |
| `local_network_require_2fa` | `True` | Require 2FA for network logins. |
| `local_network_rate_limit_attempts` | `5` | Login attempts per window. |
| `local_network_rate_limit_window_minutes` | `15` | Rate-limit window. |

## Docker & system

| Key | Default | Meaning |
|-----|---------|---------|
| `use_docker` | `True` | Use Docker-backed services (DB/Redis/TTS/...). |
| `web_ui_enabled` | `True` | Serve the web UI. |
| `tray_autostart` | `False` | Start the desktop tray on login. |
| `ux_auto_open_links` | `False` | Auto-open `web_search` source links as browser tabs (skipped in non-interactive runs, `VAF_NONINTERACTIVE`). |
| `ux_auto_open_outputs` | `True` | Auto-open finished outputs: HTML reports in the browser, other output files via their folder in the file manager, created project folders (skipped in non-interactive runs). |
| `ux_auto_open_max_tabs` | `8` | Cap on browser tabs auto-opened per search; clamped to 1-20. |
| `debug_logs_enabled` | `True` | Write the domain/debug log families (queue metrics, backend, rag, timeline, ...). Toggleable in Settings → Advanced (the Logs page's audit timeline depends on it). Location resolves via `VAF_LOG_DIR`, then repo `logs/`, then the data dir (`~/.vaf/logs/` is a later fallback) - see [DEBUGGING.md](../DEBUGGING.md). |
| `redis_enabled` | `True` | Use Redis (cache/queues). |
| `redis_url` | `redis://localhost:6379/0` | Redis DSN. |
| `gc_enabled` | `True` | Background garbage collection of stale data. |
| `gc_interval_hours` | `12` | GC interval. |
| `gc_max_age_hours` | `48` | Max age before GC. |
| `queue_policy` | `"legacy"` | Request queue policy (admin-only): `legacy` (single priority heap) or `weighted_fair` (lane fairness across interactive/automation/background). Recommended `weighted_fair` when `parallel_main_workers > 1`. |
| `queue_weight_interactive/automation/background` | `5` / `3` / `1` | Queue priorities. |
| `update_check_on_start` | `True` | One-line "update available" hint at startup. |
| `update_include_prereleases` | `null` | `vaf update` prerelease tracking. `null` = auto (track prereleases only when the installed build is itself a prerelease), `true` = always, `false` = stable-only. Also overridable per command via `vaf update --pre`/`--stable`. See [RELEASING.md](RELEASING.md). |
| `config_format_version` | `1` | Bumped by config migrations. |
| `default_language` | `""` | Fallback language for backend spoken/canned phrases (vocab book) when the user identity has no `preferred_language` (empty = `en` there). Also the live-call base language: `preferred_language` > `default_language` > UI locale, with per-turn STT language follow on top. Not a UI-language override. |

## Thinking mode (background idle reasoning)

See [docs/agents/Thinking-Mode.md](../agents/Thinking-Mode.md). All keys are `thinking_*`;
highlights:

| Key | Default | Meaning |
|-----|---------|---------|
| `thinking_enabled` | `True` | Master switch for background thinking. |
| `thinking_provider` | `"inherit"` | Provider for thinking runs. |
| `thinking_model` | `None` | Model override; `None` = inherit. |
| `thinking_idle_minutes` | `10` | Idle time before a thinking pass. |
| `thinking_cooldown_minutes` | `110` | Cooldown between passes. |
| `thinking_max_duration_minutes` | `30` | Hard cap per pass. |
| `thinking_proactive_enabled` | `True` | Allow proactive follow-up questions. |
| `thinking_quiet_hours_enabled` | `False` | Suppress thinking during quiet hours. |
| `thinking_quiet_hours_start/end` | `23:00` / `07:00` | Quiet-hours window. |
| `thinking_question_dedup_enabled` | `True` | Semantic (embedding) de-duplication of proactive questions so they vary in topic instead of repeating the same subject. Kill-switch; reuses the existing embedding singleton, fail-open. Tuning keys: `thinking_question_similarity_threshold` (`0.80`), `thinking_question_similarity_runs`/`_max_compare` (`12`), `thinking_getto_max_attempts` (`3`). |
| `thinking_reply_wait_ttl_hours` | `12` | Safety net: a waiting-for-reply latch older than this is expired at read time, so a stale background question can never claim the user's next message as its "reply" long after the fact (the 10-min skip only runs when a thinking run fires). `0` disables. |

(~24 more `thinking_*` tuning keys exist — see config.py.)

## Connections (messaging, email, cloud)

Most of these are populated by the setup wizard / Connections UI, not hand-edited. See
[docs/integrations/CONNECTIONS.md](../integrations/CONNECTIONS.md).

| Key | Default | Meaning |
|-----|---------|---------|
| `telegram_config` | `None` | Telegram bot config (set via UI). |
| `telegram_debounce_seconds` | `5` | Telegram message debounce. |
| `telegram_idle_timeout` | `120` | Telegram session idle timeout. |
| `whatsapp_config` | `None` | WhatsApp bridge config. |
| `email_config*` | `None` | Email account config (by scope/user). |
| `email_oauth_*_client_id` | `""` | Email OAuth client IDs (Google/Microsoft/Apple). |
| `email_allow_private_hosts` | `False` | SSRF guard for IMAP/SMTP. When false (default), VAF refuses to connect to a mail host that resolves to loopback, RFC-1918 private, or link-local addresses (incl. the `169.254.169.254` cloud-metadata endpoint); set true only to use a legitimate LAN / self-hosted mail server. Multicast/reserved addresses are always refused. |
| `cloud_config` / `cloud_config_by_user` | `None` / `{}` | Cloud storage config. |
| `cloud_oauth_*_client_id` | (shipped) / `""` | Cloud OAuth client IDs (Google Drive / OneDrive / Dropbox). Google Drive falls back to the email Google client. |
| `cloud_oauth_callback_base_url` | `""` | Override for the cloud OAuth redirect_uri base. Empty = derive automatically (effective HTTPS proxy port in network+TLS mode, else the local backend), same logic as email. |
| `cloud_sync_enabled` | `False` | Enable cloud sync. |
| `cloud_sync_interval_minutes` | `15` | Cloud sync interval. |
| `cloud_sync_max_file_size_mb` | `100` | Max synced file size. |
| `cloud_sync_conflict_resolution` | `"last_write_wins"` | Conflict policy. |
| `channel_ingress_policy` | `{...}` | Inbound-channel pairing/throttle policy. |
| `connection_enabled_by_scope` | `None` | Per-scope connection toggles. |
| `front_office_contact_reply_require_approval` | `False` | Require approval before auto-replying to contacts. |

## Internal / managed (do not hand-edit)

These are secrets or identity values managed by VAF; setting them by hand can break auth or
decryption. In addition, these keys (and every other credential matched by
`Config.is_secret_config_key`: `api_key_*`, `*_client_secret`, `*_secret`,
`*_credentials_key`, `*_encryption_key`, `*_kek`, `*_password`, `memory_db_url`,
`redis_url`) are redacted from `GET /api/config` for non-admin users; only admins receive
their values.

`secure_store_kek`, `memory_encryption_key`, `email_credentials_key`, `cloud_credentials_key`,
`local_network_jwt_secret`, `local_admin_scope_id`, `local_admin_username`,
all `*_oauth_*_client_secret`, `cloud_credentials_key`, `cloud_oauth_callback_base_url`,
`email_oauth_callback_base_url`.

---

For the exhaustive list with inline rationale, read `DEFAULTS` in
[vaf/core/config.py](../../vaf/core/config.py) directly — it is the single source of truth.
