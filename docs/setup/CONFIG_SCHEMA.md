# Configuration Reference

Authoritative reference for VAF's configuration keys. The single source of truth is the
`DEFAULTS` dict in [vaf/core/config.py](../../vaf/core/config.py); this page organizes those
keys by area. Defaults shown here match `Config.DEFAULTS` (223 keys).

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

Keys marked ★ are the ones most embedders need; everything else has a sensible default.

---

## ★ Essential for embedding

| Key | Default | Meaning |
|-----|---------|---------|
| `provider` ★ | `"local"` | LLM provider: `local`, `openai`, `anthropic`, `deepseek`, `google`, `openrouter`. |
| `model` ★ | `"auto"` | Local GGUF model. `"auto"` = VRAM-adaptive default, or set `"repo/file.gguf"`. Ignored for API providers. |
| `api_key_<provider>` ★ | `""` | API key for the chosen provider (e.g. `api_key_openai`). Raw when set programmatically; Base64 on disk. |
| `api_model_<provider>` ★ | per provider (see below) | Model name for the API provider (e.g. `api_model_openai`). |
| `n_ctx` ★ | `32768` | Context window in tokens. Values below 32768 are clamped up to it. |
| `temperature` ★ | `0.7` | Sampling temperature (API + local). |
| `gpu_layers` ★ | `-1` | Local model GPU offload layers. `-1` = all; `0` = CPU only. |
| `auto_start_local_server` ★ | `True` | Start the local llama-server automatically. Set `False` when using only an API provider. |
| `router_max_tools` | `12` | Max tools handed to the model per turn (1–100). Lower = faster inference. |

Default API models (from `Config.PROVIDER_MODELS`):

| Provider | `api_model_*` default |
|----------|-----------------------|
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
| `api_key_veyllo` | `""` | Reserved (Veyllo API server, future). |
| `vision_provider` | `""` | Fallback provider for image input when the primary has no vision (e.g. `google`). Empty = strip images. |
| `vision_model` | `""` | Model for the vision fallback; empty = provider default. |
| `vision_image_max_edge` | `2000` | Downscale an image before send if its longest edge exceeds this (px); prevents provider 500s on full-res photos and cuts tokens. Smaller images are sent unchanged. |
| `vision_image_jpeg_quality` | `85` | Re-encode quality (1–95) used when an image is downscaled. |
| `api_retry_attempts` | `2` | VAF-level retries on a transient error at request initiation — **HTTP 429 (rate limit)**, 5xx, timeout or connection drop — for **all** providers (atop each SDK's own retries; only before any token is streamed, so output is never duplicated). Admin-only. |
| `api_retry_after_max` | `30` | Cap (s) on a honored `Retry-After` header from a 429, so a large/hostile value cannot stall a worker. Admin-only. |
| `api_timeout_connect` | `20.0` | OpenAI-compatible client connect timeout (s). |
| `api_timeout_write` | `120.0` | Request-upload (body) timeout (s) — bounds large image uploads. |
| `api_timeout_read` | `600.0` | Read timeout (s); kept generous so long reasoning streams are not cut off. |
| `api_timeout_pool` | `20.0` | Connection-pool acquire timeout (s). |
| `subagent_provider` | `"inherit"` | Provider for sub-agents; `inherit` = same as main. |
| `subagent_use_separate_provider` | `False` | Use `subagent_provider` instead of inheriting. |
| `subagent_model` | `""` | Model for tools/workflows (hybrid mode); empty = same as main chat. |

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
| `result_grounding_enabled` | `True` | Bounce a reply that claims a tool outcome the turn's results don't support. |
| `result_grounding_max_retries` | `2` | Corrections before proceeding anyway. |
| `team_await_enabled` | `True` | Don't let the agent finish while a sub-agent is genuinely still running. |
| `team_await_max_blocks` | `3` | Bounces before proceeding anyway. |
| `autocontinue_pending_tasks_enabled` | `True` | Keep working within the turn while tasks remain pending. |
| `autocontinue_question_classifier_enabled` | `True` | LLM check whether a reply is a blocking question before auto-continuing. |
| `task_overwrite_guard_enabled` | `True` | Confirm before replacing the whole task list while steps are pending. |
| `task_overwrite_confirm_window_seconds` | `120` | Re-call within this window = confirmed. |
| `workflow_step_validation_enabled` | `True` | LLM check that a workflow step met its goal. |
| `workflow_step_validation_max_retries` | `3` | Retries before accepting the result. |

## Sub-agents & timeouts

| Key | Default | Meaning |
|-----|---------|---------|
| `sub_agents_in_separate_terminals` | `True` | Run each sub-agent in its own terminal window. |
| `subagent_timeout_enabled` | `True` | Enable sub-agent timeouts. |
| `subagent_timeout_minutes` | `120` | Legacy IPC zombie-cleanup window. |
| `subagent_timeout_seconds` | `300` | Hard cap for a research/coding/document step. |
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
| `stt_enabled` | `False` | Enable speech-to-text. |
| `speech_stt_engine` | `"docker"` | `docker` or `local` (faster-whisper). |
| `speech_stt_docker_url` | `http://localhost:5003` | STT container URL. |
| `speech_stt_whisper_model` | `"base"` | Local Whisper model size. |
| `speech_tts_enabled` | `False` | Enable text-to-speech. |
| `speech_tts_engine` | `"docker"` | TTS engine. |
| `speech_tts_docker_url` | `http://localhost:5002` | Default TTS container URL. |
| `speech_tts_docker_url_de/en/fr` | ports 5002/5004/5006 | Per-language TTS URLs. |
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
| `debug_logs_enabled` | `True` | Verbose logs in `~/.vaf/logs/`. |
| `redis_enabled` | `True` | Use Redis (cache/queues). |
| `redis_url` | `redis://localhost:6379/0` | Redis DSN. |
| `gc_enabled` | `True` | Background garbage collection of stale data. |
| `gc_interval_hours` | `12` | GC interval. |
| `gc_max_age_hours` | `48` | Max age before GC. |
| `queue_policy` | `"legacy"` | Request queue policy (admin-only): `legacy` (single priority heap) or `weighted_fair` (lane fairness across interactive/automation/background). Recommended `weighted_fair` when `parallel_main_workers > 1`. |
| `queue_weight_interactive/automation/background` | `5` / `3` / `1` | Queue priorities. |
| `update_check_on_start` | `True` | One-line "update available" hint at startup. |
| `config_format_version` | `1` | Bumped by config migrations. |
| `default_language` | `""` | Forced UI language; empty = auto. |

## Thinking mode (background idle reasoning)

See [docs/agents/Thinking-Mode.md](../agents/Thinking-Mode.md). All keys are `thinking_*`;
highlights:

| Key | Default | Meaning |
|-----|---------|---------|
| `thinking_enabled` | `True` | Master switch for background thinking. |
| `thinking_provider` | `"inherit"` | Provider for thinking runs. |
| `thinking_model` | `None` | Model override; `None` = inherit. |
| `thinking_idle_minutes` | `10` | Idle time before a thinking pass. |
| `thinking_cooldown_minutes` | `60` | Cooldown between passes. |
| `thinking_max_duration_minutes` | `30` | Hard cap per pass. |
| `thinking_proactive_enabled` | `True` | Allow proactive follow-up questions. |
| `thinking_quiet_hours_enabled` | `False` | Suppress thinking during quiet hours. |
| `thinking_quiet_hours_start/end` | `23:00` / `07:00` | Quiet-hours window. |

(~20 more `thinking_*` tuning keys exist — see config.py.)

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
| `cloud_config` / `cloud_config_by_user` | `None` / `{}` | Cloud storage config. |
| `cloud_sync_enabled` | `False` | Enable cloud sync. |
| `cloud_sync_interval_minutes` | `15` | Cloud sync interval. |
| `cloud_sync_max_file_size_mb` | `100` | Max synced file size. |
| `cloud_sync_conflict_resolution` | `"last_write_wins"` | Conflict policy. |
| `channel_ingress_policy` | `{...}` | Inbound-channel pairing/throttle policy. |
| `connection_enabled_by_scope` | `None` | Per-scope connection toggles. |
| `front_office_contact_reply_require_approval` | `False` | Require approval before auto-replying to contacts. |

## Internal / managed (do not hand-edit)

These are secrets or identity values managed by VAF; setting them by hand can break auth or
decryption:

`secure_store_kek`, `memory_encryption_key`, `email_credentials_key`, `cloud_credentials_key`,
`local_network_jwt_secret`, `local_admin_scope_id`, `local_admin_username`,
all `*_oauth_*_client_secret`, `cloud_credentials_key`, `cloud_oauth_callback_base_url`,
`email_oauth_callback_base_url`.

---

For the exhaustive list with inline rationale, read `DEFAULTS` in
[vaf/core/config.py](../../vaf/core/config.py) directly — it is the single source of truth.
