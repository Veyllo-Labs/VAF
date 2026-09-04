# Configuration Reference

Authoritative reference for VAF's configuration keys. The single source of truth is the
`DEFAULTS` dict in [vaf/core/config.py](../../vaf/core/config.py); this page organizes those
keys by area. Defaults shown here match `Config.DEFAULTS` (343 keys).

## How configuration is set

There are two ways to supply configuration, and they compose:

1. **On disk** - `~/.vaf/config.json` (in Docker: the `VAF-Config` volume). Written by the
   setup wizard, the Settings UI, and the CLI. Loaded by `Config.load()`.
2. **Programmatically** - when embedding VAF as a library:

   ```python
   from vaf import Agent
   agent = Agent(config={"provider": "openai", "api_key_openai": "sk-..."})
   ```

   The dict is merged on top of the on-disk config for that `Agent` instance only; nothing
   is written to `~/.vaf/config.json`. See [EMBEDDING.md](../EMBEDDING.md).

> **API keys - where they live.** They are no longer written to `config.json` at all: a key
> set through the product goes into the same envelope-encrypted store that already holds mail,
> GitHub and cloud credentials. `config.json` is still READ for `api_key_*`, because installs
> that predate the move still carry a Base64 value there, and such a key is migrated into the
> store the first time it is used, and the plaintext entry is blanked once the store reads the
> value back. Be precise about what the encryption buys: the master key that opens the store is
> the OS keyring on Windows and an owner-only file (`secure_store.kek`, mode 0600) on
> Linux and macOS - `secure_store_kek_backend` overrides that per install, and the split
> exists because `chmod` cannot restrict read access on Windows at all. So it protects against a copied directory, a config
> backup, a support archive and other local accounts, and against a stolen disk only as far as
> the disk encryption underneath reaches.
>
> Two consequences worth knowing. `GET /api/config` therefore answers `api_key_<provider>`
> with the empty default even when a key is configured; `GET /api/config/api-keys` reports
> which providers have one, as booleans, and never returns a value. And **a blank value never
> deletes**: it means "not re-sent", which is what keeps a partially filled form from wiping a
> key. Removing one is an explicit call, `DELETE /api/config/api-keys/{provider}`.
>
> When you pass an `api_key_*` **programmatically** via `Agent(config={...})`, give the **raw**
> key (`"sk-..."`). It is used as-is, never decoded, never stored, and takes precedence over
> both locations above.

The keys in the "Essential for embedding" section below are the ones most embedders
need; everything else has a sensible default.

---

## Essential for embedding

| Key | Default | Meaning |
|-----|---------|---------|
| `provider` | `"local"` | LLM provider: `local`, `veyllo`, `openai`, `anthropic`, `deepseek`, `google`, `openrouter`. |
| `model` | `"auto"` | Local GGUF model. `"auto"` = VRAM-adaptive default, or set `"repo/file.gguf"`. Ignored for API providers. |
| `api_key_<provider>` | `""` | API key for the chosen provider (e.g. `api_key_openai`). Raw when set programmatically; kept in the encrypted store, not in this file (see the note above). |
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
| `vision_mode` | `"description_tool"` | How attached images reach the model. `description_tool` (default): the main model is text-only - an image is run once through the vision backend to a base description that is injected as text, and the model calls the `analyze_image` tool to inspect it on demand (token-efficient; works even with a non-vision main provider). `inline_multimodal`: legacy - send the raw image straight to a multimodal main model. See the vision section in [API_INTEGRATION.md](../llm/API_INTEGRATION.md). |
| `vision_description_max_tokens` | `1024` | Output bound for the one-time base description and for each `analyze_image` call. |
| `vision_provider` | `""` | Provider used for vision - the base description and `analyze_image`, plus every other lane that turns an image into text: PDF OCR, the Image Viewer, and the browser agent's page descriptions and CAPTCHA reads. Empty = use the main provider if it is vision-capable, else none. Set an API id (e.g. `google`) to use a different provider for seeing, or `local`: the llama server launches with the model's mmproj projector and sees images itself (no cloud). |
| `vision_model` | `""` | Model for the vision provider; empty = that provider's default. Unused for `local` (the loaded GGUF sees). |
| `vision_local_mmproj` | `""` | Local vision projector ref `owner/repo/file.gguf` (admin-only: server launch argument). Empty = derived from the model's known repo (`mmproj-F16.gguf`, e.g. from `unsloth/Qwen3.5-4B-GGUF`). |
| `browser_agent_provider` | `""` | LLM lane for `browser_agent` runs (vision_provider pattern). Empty = ride the main provider. Any API provider id = browser runs use that provider regardless of the chat model - the browser loop needs strict structured output and gains more from native vision than chat does. A lane model that accepts images gets screenshots on demand (`use_vision` auto) plus an unrequested one whenever a run stalls; a text-only one degrades gracefully to descriptions from whatever vision backend resolves, or to DOM-only. |
| `browser_agent_model` | `""` | Model for the browser lane; empty = that provider's default chain. Only read together with `browser_agent_provider` (alone it overrides the model on the main provider). |
| `voice_agent_provider` | `""` | LLM lane for the live-call voice agent (admin-only). Empty = ride the main provider (local main = time-share the one llama server). `local` = a dedicated local voice GGUF: the ONE server swaps models (voice model during the call, main model while a delegated task runs) - never two servers. Any API provider id = the call runs on that API regardless of the main provider. |
| `voice_agent_model` | `""` | For `local`: a downloaded model filename from `models/`, picked in Settings > Voice (empty = the recommended default in `voice_model.py`, Gemma 4 E4B, fetched on selection). A full GGUF ref `owner/repo/file.gguf` is still accepted (back-compat). For an API provider: model name (empty = provider default). |
| `vision_image_max_edge` | `2000` | Downscale an image before send if its longest edge exceeds this (px); prevents provider 500s on full-res photos and cuts tokens. Smaller images are sent unchanged. |
| `vision_image_jpeg_quality` | `85` | Re-encode quality (1–95) used when an image is downscaled. |
| `api_retry_attempts` | `2` | VAF-level retries on a transient error at request initiation - **HTTP 429 (rate limit)**, 5xx, timeout or connection drop - for **all** providers (atop each SDK's own retries; only before any token is streamed, so output is never duplicated). Admin-only. |
| `api_retry_after_max` | `30` | Cap (s) on a honored `Retry-After` header from a 429, so a large/hostile value cannot stall a worker. Admin-only. |
| `api_rate_limit_wait_max` | `60` | Admin-only. Total wall-clock (s) a rate-limited call (429) may spend waiting and retrying before the error surfaces; `0` disables 429 retries. Deliberately a TIME budget, distinct from `api_retry_attempts` (which counts 5xx/timeout retries): a per-org token window drains on its own schedule, and the provider names the wait itself (`Retry-After` header, or "Please try again in 186ms" in the body) - counting attempts against that is how a live turn died after 3s of patience against a request that asked for 186ms. Both the SDK lane and the coder's raw-HTTP lane honor it. |
| `api_timeout_connect` | `20.0` | OpenAI-compatible client connect timeout (s). |
| `api_timeout_write` | `120.0` | Request-upload (body) timeout (s) - bounds large image uploads. |
| `api_timeout_read` | `600.0` | Read timeout (s); kept generous so long reasoning streams are not cut off. |
| `api_timeout_pool` | `20.0` | Connection-pool acquire timeout (s). |
| `anthropic_prompt_cache` | `True` | Anthropic only: send the system prompt as a `cache_control: ephemeral` block so the stable prefix is cached across multi-turn / tool loops (cost saver). Read with an inline default, not part of `DEFAULTS`. Admin-only by an explicit entry, because it decides what every request on the instance sends and therefore what everyone's input tokens cost. See [API_INTEGRATION.md](../llm/API_INTEGRATION.md). |
| `anthropic_thinking` | `True` | Anthropic only: adaptive (extended) thinking on supported models (reasoning streams wrapped in `<think>` tags); ignored on models without thinking support. Read with an inline default, not part of `DEFAULTS`. Admin-only by an explicit entry: thinking tokens are billed, so this is instance spend. |
| `google_thinking` | `True` | Google only: surface model reasoning on thinking-capable Gemini models, wrapped in `<think>` tags like DeepSeek. Read with an inline default, not part of `DEFAULTS`. Admin-only by an explicit entry, for the same reason as `anthropic_thinking`. |
| `local_api_url` | `""` | OpenAI-compatible endpoint for the API-backend consumers of provider `local` (browser agent, local vision, cloud-to-local failover), e.g. an Ollama/vLLM URL. Empty = VAF's own llama-server. Does NOT redirect the main chat loop (see [EMBEDDING.md](../EMBEDDING.md)). Read with an inline default, not part of `DEFAULTS`. Admin-only by an explicit entry: it decides where those lanes send their prompts, which is an egress decision, not a preference. |
| `subagent_provider` | `"inherit"` | Provider for sub-agents; `inherit` = same as main. Half of a PAIR: read it with `config.subagent_provider_override()` and write it with `config.set_subagent_provider()`, never alone. Set without the gate below it is silently inert. |
| `subagent_use_separate_provider` | `False` | The gate on `subagent_provider`. Written by `set_subagent_provider()`; nothing else should touch it. |
| `subagent_model` | `""` | Model for tools/workflows (hybrid mode); empty = same as main chat. |
| `subagent_concurrent_chat_enabled` | `True` | Kill-switch for chat-while-a-sub-agent-runs (the SUB-AGENT ACTIVE prompt block). Renders only in API mode regardless (code gate on the main provider + an initialized API backend). Admin-only via the `subagent_` prefix. |

## Failover & resilience

Automatic provider failover: if the primary provider is unreachable or errors out **before the first token**, the request is retried down a chain of fallback providers. Once a real token has streamed, no switch happens (it would duplicate output). Off by default - behaviour is unchanged unless `failover_level` is set. Configured in the UI under Settings → Advanced → Failover. All keys are admin-only.

A link that fails for an outage reason is then remembered as dead for `failover_recheck_after_s` and **skipped** rather than waited for again on every request; when that window passes it is simply a candidate again, so the next ordinary request is the probe. Nothing pings the provider in the background: a liveness ping would cost a real completion (`test_connection`) or could not tell "down" from "no discovery endpoint" (`list_models`), and it would have to run in every process rather than only where a scheduler happens to live. The state is per `APIBackendManager`, so it is shared by everything one agent does and is not carried across a restart.

| Key | Default | Description |
|-----|---------|-------------|
| `failover_level` | `"off"` | Resilience level: `off` (primary only), `basic` (→ local model), `balanced` (→ backup API → local), `maximum` (full chain, more aggressive triggers). |
| `failover_backup_provider` | `""` | Provider id used as the backup API link (e.g. `anthropic`, `openai`). Empty = no backup-API link. Skipped automatically if its API key is missing. |
| `failover_backup_model` | `""` | Model for the backup link; empty = that provider's default. |
| `failover_local_model` | `""` | GGUF filename for the local link; empty = auto. |
| `failover_timeout_s` | `30` | First-token deadline (s) before failing over to the next link; `0` = no extra deadline (rely on the provider's own timeout). |
| `failover_triggers` | `[]` | Subset of `["timeout","rate_limit","server_error"]` that may trigger a switch; empty = any error. Connection/unknown errors always switch. |
| `failover_return_to_primary` | `True` | After a fallback, prefer the primary again on the next request; when off, stay on the working link until it also fails **or until an earlier link's `failover_recheck_after_s` window has passed**. With the re-check disabled (`0`) the off position pins the working link permanently, as it did before that key existed. |
| `failover_recheck_after_s` | `300` | Seconds a link that just failed is skipped for, instead of being waited for again on every request; the first request after the window is the re-check, and a failed re-check re-arms it. `0` = never skip (every request pays the failed link again, the behaviour before this key). A 4xx never arms it (that is the request, not the provider), and nothing is skipped while the outbound history carries provider-bound `tool_call` ids or when every link in the chain is cooling down. |

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
| `api_max_response_tokens` | `16384` | Per-call output cap on the API lanes, and the in-process local lane reads `max_generation_tokens` beside it. Admin-only: how long one reply may be is measured in tokens the instance pays for. A reasoning model spends this budget on thinking before it writes a word, so a figure sized for an answer cuts the answer off. A provider that refuses the value is retried once at 8192 and the lower figure is remembered for the process, so raising this cannot break a model whose own ceiling is lower. |
| `model_unload_idle_minutes` | `30` | Unload the local model after this idle time. |
| `parallel_main_workers` | `1` | Concurrent main-agent workers (admin-only). `1` = serialized (default). When > 1, the effective count is clamped per provider (see the two keys below) and turns from different SESSIONS run concurrently while turns within one session stay serialized. The queue keys on the session id, not the user, so one person's web and messaging sessions can run at the same time. Pair with `queue_policy: weighted_fair` for lane fairness. |
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
| `room_unattended_report_enabled` | `True` | Admin-only. Tell the owner when an A2A room keeps running without them. |
| `room_unattended_report_every_turns` | `20` | Admin-only. Room-driven turns for ONE room without a message from a real person between notices to the owner (20, 40, 60, ...). The work is never stopped: halting unattended but legitimate work only moves the damage, and the hard ceiling is `spend_budget_usd_per_day`. A timer or an automation does not count as the person being back. |
| `a2a_room_ping_minutes` | `60` | Admin-only. A room checks in on a member that has neither read nor written in it for this long; `0` turns it off. Addressed to that ONE peer, so a check-in never wakes the whole room, and the text is shaped by that peer's role - what a leader is told differs from what an idle worker is told. It is an invitation and never an order: a room is input, not authority, and saying nothing is a valid answer. The timer runs on the machine that HOLDS the room. |
| `a2a_room_invite_directory` | `True` | Admin-only. Whether every signed-in account may see the NAMES of the other accounts in a room's invite picker (names only, never email or role). `False` keeps the picker for admins; anybody else invites by typing the exact account name. A directory of who has an account on this machine is the operator's call. |
| `nonprogress_max_turns` | `6` | Consecutive read-only/verify-only tool turns (`list_*`/`read_*`/`get_*`, `list_automations`, …; not `web_search`/`memory_search`) before a nudge then a forced text answer. Catches a "verify forever" loop; any mutating/producing tool resets it. |
| `chat_step_wall_clock_seconds` | `3600` | Main-loop wall-clock **backstop** (1h): a single user turn can never grind past this (checked at each tool-turn boundary), independent of tool count or provider speed. Deliberately generous - never aborts legitimate long work; the no-progress guard + per-tool timeouts stop the common case far earlier. The 75-turn cap is a secondary guard. |
| `max_tool_turns_per_step` | `75` | Admin-only. Hard stop: tool turns one user turn may use before the loop protection ends it. The soft goal-reminder keeps its distance below the cap (min(50, cap-3), so 50 at the default). Clamped to at least 5. |
| `tool_loop_unlimited` | `False` | Admin-only. Disables the hard stop AND the wall-clock backstop entirely; the spend budget (`spend_budget_usd_per_day`) still applies. The soft goal-reminder still fires, worded without a hard-stop promise. |
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
| `workflow_identity_injection` | `declared` | Who a workflow's tools think is calling. Seven places construct a `WorkflowEngine`; three always passed an identity (chat user for a temporary workflow, task owner for an automation, the paused record on resume), four passed none - including the main saved-template lane - so a workflow ran as the machine owner whoever started it, and every tool keyed on the caller (memory, messaging, mail, calendar, contacts) followed. `declared` (the default since this release) makes those four pass the real identity and distributes it by each tool's `identity_kwargs`; `legacy` restores the old behaviour, in which the four pass nothing and the rest is distributed by a hardcoded name list. Three values and not a boolean on purpose: `off` is not the old state, and the three lanes that always passed one are unaffected either way. **Only `legacy` and `off` count as a rollback** - any other value, including a typo, means `declared`, because "as before" is now the state in which a workflow acts as the machine owner. Switching to `declared` gives 47 further tools an identity they never had in a workflow (files, GitHub, automations, skills, messenger reads) and takes none away; the reach is pinned by `tests/test_workflow_identity_blast_radius.py`. The switch now governs the WHOLE modern step lane: under `declared`, non-spawn steps dispatch through the shared funnel (hard policy, account tool allowlist, embedder authorizer; confirmation gate off); `legacy`/`off` restore the ENTIRE pre-funnel lane - name-list identity AND absence of per-step policy - and the funnel is not even constructed. The saved-template START gate (per-user workflow list) is NOT behind this switch. The legacy lane retires after the funnel lane has survived one released version with the policy stages on and no rollback report. |
| `workflow_step_validation_enabled` | `True` | LLM check that a workflow step met its goal. |
| `workflow_step_validation_max_retries` | `3` | Retries before accepting the result. |
| `channel_tools_unrestricted` | `True` | Admin-only. When `True`, messaging-channel sessions (Telegram/WhatsApp/Discord) get the same tools as the main agent - `channel_restrictions` and the per-call confirmation gate are lifted. The `admin_only` check and the channel whitelist (`paired_only` by default) still apply. On by default; set to `False` to restrict channel sessions to non-channel-restricted tools. |
| `tool_confirmation_bypass_admins` | `False` | Admin-only. Hands-off mode for the machine owner: an identity that counts as admin skips the per-call confirmation dialog. It skips only the QUESTION - `admin_only`, the account allowlist and an authorizer's explicit `ask()` are decided earlier in the funnel and are untouched - and every bypass emits a `gate_bypassed` event, so hands-off never means unobserved. Off by default. |
| `spend_budget_usd_per_day` | `0` | Admin-only. Daily API spend cap per USER, in USD; `0` (default) means no cap. The estimate is recorded either way in `~/.vaf/spend/<scope>.json`, so an instance can measure before it caps. Prices come from a bundled table; an unrecognised model is deliberately priced at the expensive end, so a cap trips early rather than late, and the day's entry counts how many calls were priced that way. Local models cost nothing here. When the cap is reached the turn ends with a `[LOOP_PROTECTION]` message naming this key. |
| `upload_threat_scan_enabled` | `True` | Every lane that accepts a file from someone else (chat attachments, workspace and room uploads, Telegram/Discord/WhatsApp, mail attachments, cloud downloads, skill imports) hashes the content and checks it against the machine-wide known-bad list. A hit is refused outright and audited as `upload_blocked`. `False` turns the lookup off everywhere at once; the list itself is untouched. See [SECURITY_DASHBOARD.md](../security/SECURITY_DASHBOARD.md). |
| `upload_scan_advisory_enabled` | `True` | The non-binding half: the static scanner's opinion on arriving text (dynamic execution, pipe-to-shell, embedded keys, hidden bidi characters). It NEVER blocks - it raises an `upload_flagged` event and, where the lane has a text channel, appends a note. `False` silences it. |
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
| `coder_tool_allowlist` | `""` | Admin-only. WHICH tools the coding agent is offered, as a comma-separated list of tool names. Empty = the built-in whitelist in [vaf/core/coder_tools.py](../../vaf/core/coder_tools.py) (`CODER_ALLOWED_TOOLS`): files, code, git, shell, tests and lookups, deliberately without mail, messengers, calendars or contacts. A non-empty value REPLACES that set. The names a run cannot work without (`set_todos`, `write_file`, `read_file`, `edit_file`, `list_files`, `task_done`, `ask_user`, `request_clarification`) are added back regardless, so a typo costs optional tools rather than the coder. Admin-only because it decides what a build step may reach for, and `coder_` is not a global prefix. |
| `coder_tool_allowlist_extra` | `""` | Admin-only. Tool names ADDED to whichever allow-list is in force, same format. This is the key to reach for when the coder should gain one specific tool; it survives changes to the built-in list. |
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
| `memory_embedding_model` | `intfloat/multilingual-e5-small` | Embedding model for memory vectors (384-dim, multilingual, ONNX-backed). `all-MiniLM-L6-v2` is the supported English-only alternative. Changing this strands existing vectors in the old model's space; the app start detects the divergence via the per-row `embedding_model` stamp and re-embeds the store in the background (`vaf memory reembed`) before switching queries over. |
| `memory_encryption_key` | `""` | Managed; memory-at-rest encryption key (AES-256-GCM, Base64). Held in the data keyring (`<data_dir>/data_keys.enc`), not in this file: a value left here by an older install is adopted byte-identically on first use and the plaintext entry is then blanked. Minted only when neither the keyring nor a cleanly-parsed config has one; an unreadable config refuses key resolution instead of minting a replacement. Protected (a save that omits it keeps the stored value) and redacted for non-admins. Losing it orphans every encrypted memory - back it up; recovery from a key rotation: `vaf memory rekey`. |
| `memory_auto_capture` | `False` | Auto-store memories from conversation. |
| `memory_auto_connect_threshold` | `0.7` | Similarity to auto-link memories. |
| `memory_chunk_size` | `512` | Chunk size (tokens) for indexing. |
| `memory_chunk_overlap` | `50` | Chunk overlap. |
| `memory_rag_k` | `5` | Top-k memories retrieved per query. |
| `memory_rag_threshold` | `0.3` | Min similarity to include. |
| `memory_rag_refine_query` | `True` | LLM query refinement before search. |
| `context_archive_max_age_days` | `14` | Age sweep for the pre-compression conversation snapshots in `~/.vaf/context_archive` (`0` keeps them forever). Their old cleanup only ran on a clean shutdown, so they accumulated indefinitely. |
| `context_compress_tokens` | `45000` | **"Context effort"** in Settings. API providers only: compress the conversation history once it exceeds this token budget, instead of waiting for a share of the model window. An API resends and bills the whole history on every LLM round-trip, so the 128k window is the wrong compression ceiling (a ~65k-token chat fit it forever while every one-line question paid the full ~65k again) - the budget is the price of ONE reply. The effective limit is `min(model window, this budget)`, floored at 8000; local models ignore it (their tokens are free, the window is the limit). `0` restores window-based triggering. The settings surfaces offer fixed rungs from 8000 up to the configured model's real window (`vaf/core/context.py`, `context_effort_steps`), so a 128k model shows seven rungs and a 32 768-token local model four. **Admin-only/global.** |
| `prompt_log_full_enabled` | `False` | Write the ENTIRE assembled system prompt (user profile, retrieved memories, working memory, contacts) into `prompt_*.log`. Debugging only: it is the richest plaintext copy of the user's data on the machine. |
| `cli_password_gate` | `True` | The interactive terminal (`vaf run`, the TUI) and the whole `vaf session` group (`list`, `load`, `export`, `search`, `delete`) ask for the admin password before running. Scripts, `-p`, the tray, the headless runner and automations never do - they run inside the shield. Verified offline against a hash mirrored into the keyring, so a sleeping database does not lock you out. |
| `secure_store_kek_backend` | `"auto"` | Where the master key that opens the keyring is stored. `auto` picks per platform: the **OS keyring on Windows**, because `chmod` there cannot restrict read access to a key file and the Credential Manager is reachable from the user's Startup-folder autostart; an owner-only **file on Linux and macOS**, because `chmod` is real there and both OS keyrings can lock the app out (Linux: a supervisor-started tray has no session bus; macOS: a Keychain item is bound to the requesting binary, so an interpreter upgrade re-prompts). `file` and `keyring` force one. Reading finds the key wherever an earlier version put it, so changing this never relocates an existing key. |
| `allow_plaintext_at_rest` | `True` | Accept files WITHOUT the encryption header when reading. Needed while a store still holds pre-encryption records; the startup sweep turns it off automatically after a pass that found nothing plain left. Left on forever it is a downgrade path: anyone who can write into the store can replace a record with plaintext and the reader takes it. |
| `file_encryption_enabled` | `True` | Encrypt the file stores at rest (chats, context archives, handoff bundles, sub-agent queue, working memory) with the machine-held key from the data keyring. It decides what NEW writes look like; whether a file WITHOUT the header is still accepted on read is `allow_plaintext_at_rest` above. Switching this off reopens that tolerance even on a store the sweep has already enforced, because a store that writes plaintext by choice has to be able to read it - so nothing is stranded either way, and files already encrypted keep opening as long as the key is in the keyring. See [ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md). |
| `cross_chat_hint_enabled` | `True` | Cross Chat Hint: append pointers from this user's other chats below the retrieved memories. Lexical, reads the session files, needs no database. |
| `cross_chat_hint_k` | `2` | Max cross-chat hints per turn. `0` disables the lane without touching the switch. |
| `cross_chat_hint_min_terms` | `2` | Distinct query terms a chat must match to qualify; a single term that is rare across the scanned chats also qualifies. |
| `cross_chat_hint_min_score` | `0.45` | Min share of the question's informative terms a chat must cover. Raise it if hints feel loosely related, lower it for more recall. |
| `cross_chat_hint_max_age_days` | `30` | Chats not touched within this many days are not scanned. |
| `memory_hybrid_enabled` | `True` | Hybrid vector + lexical retrieval. |
| `memory_hybrid_lexical_k` | `20` | Lexical candidates. |
| `memory_hybrid_lexical_min_score` | `0.05` | Min lexical score. |
| `memory_hybrid_lexical_scan_limit` | `2000` | Lexical scan cap (clamped to 2000). Rows are scanned unordered, so a store larger than this cap gets an arbitrary partial lexical lane; the cap is a per-query decrypt+score cost ceiling. |
| `memory_profile_cache_chars` | `4000` | Admin-only. Ceiling on the `known_facts` block, the retrieved user-profile summary injected into EVERY system prompt. Measured before it existed: 9,471 characters, more than a third of the whole system message, growing 38% in 20 days with nothing bounding it. Cut at a line boundary and marked as cut, at the writer AND at the reader, so a cache from before the ceiling is bounded on the next turn rather than the next refresh. `0` turns the ceiling off. |
| `memory_hybrid_rrf_k` | `60` | Reciprocal-rank-fusion constant. |
| `memory_compaction_enabled` | `True` | Compact long histories. |
| `memory_compaction_interval` | `15` | Turns between compaction checks. |
| `memory_compaction_max_tokens` | `4000` | Target size of a compaction summary. |
| `resume_compaction_enabled` | `True` | Compact on session resume. |
| `attachment_rag_*` | (12 keys) | Per-attachment RAG: `attachment_rag_enabled` (`True`), `attachment_rag_k` (`4`), `attachment_rag_threshold` (`0.28`), `attachment_rag_ttl_hours` (`24`), plus hierarchical/lexical/size tuning. See config.py. |
| `learn_document_max_pages` | `0` | PDF pages extracted for document learning; `0` = the whole document (deliberate default). A positive value is an opt-in spend cap; when it fires, the tool reply names it. Admin-only (spend control). |
| `learn_max_sections` | `0` | Sections stored per learned document; `0` = all. Positive = opt-in spend cap, named in the reply when it fires. Admin-only (spend control). |
| `learn_batch_pages` | `10` | Pages per learn batch (clamped 2-100): one batch = one progress tick and one DB commit, so a crash loses at most one batch. Admin-only (spend control). |
| `memory_document_extraction_max_tokens` | `1200` | Max tokens for each per-section extraction LLM call (clamped 400-4000). Admin-only (spend control). |
| `ocr_engine` | `"auto"` | OCR for scanned PDFs: `auto` (Tesseract if its binary answers, else the vision model when the vision lane resolves), `tesseract`, or `vision`. An explicit pick never silently runs the other engine. Admin-only (spend control: the vision engine is one model call per page). |
| `ocr_vision_max_pages_per_call` | `10` | Per-call page budget for vision OCR; when it cuts, the output names the key and the continuation page. The batched learn job stays under it by design. Admin-only (spend control). |

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
| `librarian_pdf_max_pages_preview` | `50` | PDF preview page cap (an explicit page range in the read request bypasses it). |
| `librarian_ocr_fallback_for_pdf` | `True` | OCR scanned PDFs in the librarian read path (the engine comes from `ocr_engine`). |
| `document_conversion_docker_url` | `http://localhost:5005` | Gotenberg (Office→PDF) endpoint. |

## Speech (STT / TTS)

| Key | Default | Meaning |
|-----|---------|---------|
| `stt_enabled` | `False` | Legacy STT toggle (ORed with `speech_stt_enabled`; admin-only). |
| `speech_stt_enabled` | `False` | Enable speech-to-text (canonical key). |
| `speech_stt_engine` | `"docker"` | `docker` or `local` (faster-whisper, `pip install vaf[speech]`). Read through `speech_api.resolve_stt_engine()`, never raw: only the literal `local` selects faster-whisper, anything else means `docker`, and a resolving `speech_stt_provider` outranks both. |
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
| `speech_stt_provider` | `""` | Cloud STT provider: `""` (use the local engine), `veyllo`, `elevenlabs`, or `openai`. Takes precedence over `speech_stt_engine` when it resolves (known provider, STT-capable, key present); falls back to the Docker Whisper container on API errors, not to faster-whisper. Seeded to `veyllo` the first time a Veyllo key is added (onboarding OR later in Settings) while no STT provider was chosen (`Config.apply_veyllo_stt_default`); an explicit later choice overwrites it. |
| `speech_stt_api_model` | `""` | Model for the cloud STT provider (`""` = default: Veyllo `veyllo-transcribe`, ElevenLabs `scribe_v2`, OpenAI `whisper-1`). |
| `api_key_elevenlabs` | `""` | ElevenLabs API key (speech only, not an LLM provider). Kept in the encrypted store; redacted for non-admin reads. |
| `speaker_id_enabled` | `True` | Speaker identification kill-switch. Inert until a voice profile is enrolled (enrollment is the real opt-in; no model loads without a profile). |
| `speaker_id_threshold` | `0.60` | Cosine score at or above this labels the enrolled user. Lowering it moves BOTH bars (the unsure floor is `threshold - band`), so it makes it easier for a stranger to pass as the owner - tune it only against measured scores, which the `[SPEAKER_SCORE]` line in `backend_*.log` prints per turn when Debug Logs are on. |
| `speaker_id_band` | `0.05` | Band below the threshold labeled "unsure" (triggers confirmation). |
| `speaker_id_confirmation_enabled` | `true` | On "unsure": ask the owner to confirm (main messenger, else web card). "No, that's NAME" stores a named third-party profile. |
| `voice_awareness_activity` | `0.5` | Voice reflex chime-in dial, `0.0`..`1.0` (quiet..active); a single control for how readily the agent chimes in on interesting OVERHEARD talk during a call; at `0.0` it only takes notes and never interrupts. Scales a local policy threshold only (no inference redirect, no billing), so it is user-writable. See [docs/agents/VOICE_REFLEX.md](../agents/VOICE_REFLEX.md). |
| `voice_awareness_topics` | `[]` | The owner's interest topics (list of short strings). A proactive chime-in must embedding-match one of these to fire, so an empty list means the agent never chimes in unprompted (conservative default). User-writable. |
| `voice_semantic_endpoint_enabled` | `False` | Server-side semantic turn-end for voice calls: the browser streams mic PCM (16 kHz mono, ~43 kB/s while listening) and the server proposes the endpoint from prosody (Smart Turn v3, 8 MB int8 ONNX, CPU, BSD-2, downloaded on first use) instead of waiting out the fixed silence timer. Off (default) keeps the browser-only silence timer; the timer always remains as the fallback. Admin-only/global. |
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
| `local_network_jwt_secret` | `""` | Managed; JWT signing secret. Held in the data keyring, not in this file: a value left here by an older install is adopted on first use and the plaintext entry is then blanked. |
| `local_network_jwt_expiry_hours` | `24` | JWT lifetime. |
| `local_network_require_2fa` | `True` | Require 2FA for network logins. |
| `local_network_rate_limit_attempts` | `5` | Login attempts per window. |
| `local_network_rate_limit_window_minutes` | `15` | Rate-limit window. |

## Docker & system

| Key | Default | Meaning |
|-----|---------|---------|
| `use_docker` | `True` | Use Docker-backed services (DB/Redis/TTS/...). |
| `browser_pool_max` | `2` | Admin-only. How many people may have a browser CONTAINER of their own at the same time, each with its own profile and its own container network. `0` switches the pool off and sends everyone back to the one shared browser, where a lease and a handover scrub stand in for the partition. Every instance costs roughly 1-2 GB of RAM, so raising this is a memory decision: budget about 2 GB per concurrently active user and keep `browser_pool_min_free_mb` beneath what stays free. Overridden by `VAF_BROWSER_POOL_MAX`. See [BROWSER_AGENT.md](../agents/BROWSER_AGENT.md#per-user-browser-pool-parallel-use). |
| `browser_pool_min_free_mb` | `2500` | Admin-only. Free-memory floor: below it no NEW instance is started and the caller falls back to the shared browser. Raising `browser_pool_max` without headroom above this floor changes nothing, which is the usual reason a raised pool appears to do nothing. Overridden by `VAF_BROWSER_POOL_MIN_FREE_MB`. |
| `browser_pool_idle_seconds` | `900` | Admin-only. How long an unused instance stays up before it is stopped to give the RAM back. The container and its profile volume survive, so the person's history and logins come back with them. Minimum 60. Overridden by `VAF_BROWSER_POOL_IDLE_S`. |
| `browser_pool_strict` | `False` | Admin-only. Strict pool: a user who cannot get a DEDICATED browser instance (pool at capacity, low memory, docker trouble) is answered busy instead of silently sharing the fallback browser. Every fallback, strict or not, is recorded as a `browser_pool_fallback` security event. Off by default: a solo install would rather time-share than see busy. Overridden by `VAF_BROWSER_POOL_STRICT`. |
| `browser_image_max_age_days` | `14` | Admin-only. Freshness budget for the browser image: older than this, the next stack start rebuilds it with `--pull --no-cache` so the unpinned Debian Chromium inside actually receives security updates (the ordinary cached `--build` never re-runs that layer). A failed fresh build never blocks the start; it is recorded as a `browser_image_stale` security event instead. `0` disables the gate. Overridden by `VAF_BROWSER_IMAGE_MAX_AGE_DAYS`. |
| `web_ui_enabled` | `True` | Serve the web UI. |
| `tray_autostart` | `False` | Start the desktop tray on login. |
| `theme` | `vaf` | Terminal colour theme for both terminal lanes; catalog in `vaf/cli/themes.py`. The default is monochrome. Changed by `t` / `theme <name>` in the app, or the Theme row in `vaf settings`. |
| `tui_mode` | `app` | Terminal UI lane for `vaf run`: `app` (full-screen terminal app), `modern` (prompt-toolkit lane), `classic` (plain prompt). `vaf run --classic` overrides per invocation. |
| `ux_auto_open_links` | `False` | Auto-open `web_search` source links as browser tabs (skipped in non-interactive runs, `VAF_NONINTERACTIVE`). |
| `ux_auto_open_outputs` | `True` | Auto-open finished outputs: HTML reports in the browser, other output files via their folder in the file manager, created project folders (skipped in non-interactive runs). |
| `ux_auto_open_max_tabs` | `8` | Cap on browser tabs auto-opened per search; clamped to 1-20. |
| `ux_voice_review` | `False` | Terminal-app voice capture: `False` sends the transcript immediately (classic flow), `True` puts it into the input box for editing first. |
| `debug_logs_enabled` | `True` | Write the domain/debug log families (queue metrics, backend, rag, timeline, ...). No UI toggle; opt out by setting it to `false` in `config.json` (the Logs page's audit timeline depends on it, and its empty states name this key). Location resolves via `VAF_LOG_DIR`, then the platform data dir, then `~/.vaf/logs/`; the checkout's own `logs/` is a candidate only when `VAF_DEV_LOGS` is set, so logs stay under the same home as the other stores - see [DEBUGGING.md](../DEBUGGING.md). |
| `redis_enabled` | `True` | Use Redis (cache/queues). |
| `redis_url` | `redis://localhost:6379/0` | Redis DSN. |
| `gc_enabled` | `True` | Background garbage collection of stale data. |
| `gc_interval_hours` | `12` | GC interval. |
| `gc_max_age_hours` | `48` | Max age before GC. |
| `security_log_retention_days` | `14` | How long security logs are kept. They share the log directory and the dated-name convention with ordinary logs, so without this they were swept up by `gc_max_age_hours` after two days - an audit trail that erased itself. |
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
| `thinking_question_dedup_enabled` | `True` | Semantic (embedding) de-duplication of proactive questions so they vary in topic instead of repeating the same subject. Kill-switch; reuses the existing embedding singleton, fail-open. |
| `thinking_question_similarity_percentile` | `90` | The reject cutoff is DERIVED per run: this percentile of the recent-question pool's own nearest-neighbour cosines. An absolute cutoff has no stable meaning across embedding models, and on an anisotropic one (all-MiniLM-L6-v2, the default `memory_embedding_model`) unrelated same-language questions all score high. Measured on a real 12-question pool: unrelated candidates `0.872`-`0.912`, pool self-similarity from `0.800` - so the previously fixed `0.80` rejected every question. |
| `thinking_question_similarity_threshold` | `0.80` | FLOOR for that derived cutoff, so a very broad pool cannot drag it down to where genuinely different questions get rejected. No longer a threshold on its own. |
| `thinking_question_similarity_max` | `0.97` | Absolute ceiling, checked before the pool-size stand-down: a cosine this high is near-identical TEXT in any model, which is the one property the narrow-cone effect does not distort. |
| `thinking_question_similarity_min_pool` | `3` | Below this many recent questions there is no distribution to calibrate against, so the derived half of the gate stands down and only the ceiling applies. A fresh user therefore gets no topic-level dedup for their first few questions; the text-based recent/declined prompts cover that window. |
| `thinking_question_similarity_runs` / `_max_compare` | `12` / `12` | How many recent questions are considered, and the hard cap on how many are embedded per turn. |
| `thinking_getto_max_attempts` | `3` | Dedup rejections allowed per RUN before the next question is delivered as it stands. Spent inside the gate, where the retry actually happens: the model re-calls `ask_user` within a single step, so a budget one level up never fired (a run once spent 12 tool turns on rejected questions). |
| `thinking_max_turns` | `8` | Outer run-loop turns. Turn 0 gathers, the rest walk the proactive ladder. Clamped to 1-10 and never below `thinking_no_progress_turns` + 2. |
| `thinking_max_tool_turns` | `15` | Tool-result cycles allowed inside ONE background step; the main chat uses a far higher cap (`max_tool_turns_per_step`). |
| `thinking_automation_review_enabled` | `true` | Ladder rung: once the user has several automations, a clear-floor run reviews the EXISTING ones instead of proposing another. Findings are computed in code from the stored record (never ran, no recent success, disabled and forgotten, slot collision, near-duplicate instructions, and repeated recorded errors once a run log exists); the model only phrases one and proposes a fix. It cannot edit an automation from this rung - `update/create/delete_automation` are refused there. |
| `thinking_automation_review_min_automations` | `3` | Enabled automations from which that rung takes over from "propose a new one". |
| `thinking_relevance_enabled` | `true` | Ladder rung: build a watchlist from what memory holds about the user's plans, commitments and interests, check ONE item with `web_search`, and speak only if the finding CHANGES something concrete for them. A news summary is a failure of this rung, not an output - falling through silently is its normal case. Its message is recorded as an FYI (`kind="relevance"`), so it is never nudged and never re-asked as an unanswered question. Health is deliberately not a watchlist category. |
| `thinking_relevance_cooldown_hours` | `6` | Minimum gap between two relevance notices - a FREQUENCY bound only. It is deliberately not what stops the same thing being reported twice: the declined-questions log (30 days), the semantic dedup gate (the rung delivers in mode `grounded`, and its comparison pool includes that log) and the self-disable below all do that already. At `72` this key was doing a job it does not have and doing it bluntly - a finding about tomorrow waits three days. The rung also disables itself once 2 of its last 10 notices were DECLINED. Declined, not merely unanswered: an FYI is never replied to, so counting silence as rejection would switch the rung off for good on exactly the behaviour it is designed for. |
| `thinking_reply_wait_ttl_hours` | `12` | How long a background question stays understandable: a waiting-for-reply latch older than this is expired at read time, so a stale question can never claim the user's next message as its "reply" long after the fact. This is the record's real lifetime - `thinking_wait_skip_minutes` only ends the CHASING (nudges/escalation), because the main agent still needs to know what a late answer is answering. `0` disables. |

(~16 more `thinking_*` tuning keys exist - see config.py.)

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
| `email_oauth_*_client_id` | `""` | Email OAuth client IDs (Google/Microsoft). |
| `email_oauth_*_client_secret` | `""` | Email OAuth client secrets (Google/Microsoft). Read-redacted for non-admins. |
| `email_allow_private_hosts` | `False` | SSRF guard for IMAP/SMTP. When false (default), VAF refuses to connect to a mail host that resolves to loopback, RFC-1918 private, or link-local addresses (incl. the `169.254.169.254` cloud-metadata endpoint); set true only to use a legitimate LAN / self-hosted mail server. Multicast/reserved addresses are always refused. Admin-only. |
| `email_agent_phishing_filter_enabled` | `True` | Hide suspicious (phishing-like) mail from the agent's mail tools while the Web UI still shows it with a warning (prompt-injection defense). Admin-only. |
| `email_agent_phishing_score_threshold` | `3` | Risk score (1-10) at/above which a message is hidden from the agent. Admin-only. |
| `email_agent_trusted_sender_domains` | `None` | List of sender From-domains that bypass the phishing filter. Note: the From header is not authenticated; use sparingly. Admin-only. |
| `mail_engine_write_enabled` | `False` | Allow the mail engine to perform server-side writes (flags/move/append). The standalone safety valve for mailbox writes: the engine stays read-only against mailboxes until this is set. Admin-only. |
| `mail_body_retention_days` | `365` | How long cached message bodies are kept in the per-user mail store. Headers/envelopes are kept forever. Admin-only. |
| `mail_store_encryption_key` | `""` | AES key (Base64) for encrypting cached mail bodies at rest; held in the data keyring and auto-generated there on first use, with a value left here by an older install adopted once and the plaintext entry then blanked. Protected (never overwritten from the UI) and redacted for non-admins. |
| `mail_composer_enabled` | `True` | Offer the Mail Composer (draft / rewrite buttons) in the mail window's compose box. The lane is inert until a user clicks it, makes exactly one model call with NO tools, and only ever fills the textarea - it can never send. Admin-only. |
| `mail_composer_max_context_chars` | `12000` | Total budget for thread text handed to the Mail Composer, in characters (clamped to 2000-40000). Characters rather than tokens because no real tokenizer exists on this path; at the repo's 2.5-3.6 chars-per-token estimates this is roughly 3.5-4.5k tokens, well inside the 32768 `n_ctx` floor. Bounds how much attacker-controlled mail text reaches a prompt, so admin-only. |
| `mail_composer_max_message_chars` | `4000` | Per-message cap inside that budget; the message being replied to keeps at least 2000 characters regardless. Admin-only. |
| `mail_composer_max_messages` | `8` | Hard cap on how many thread messages are included in full. What does not fit degrades to a one-line summary rather than vanishing. Admin-only. |
| `mail_composer_max_output_tokens` | `800` | Output cap for the Mail Composer's single model call. Admin-only. |
| `mail_composer_memory_enabled` | `True` | Let the Mail Composer consult the user's own long-term memory when drafting. Retrieval is keyed on the user's instruction ONLY, never on mail content, so a message cannot steer what is retrieved; with no instruction nothing is retrieved. Set false where a drafted reply must never be able to repeat stored notes. Admin-only. |
| `mail_composer_mailbox_search_enabled` | `False` | Let the Mail Composer quote older mail from OTHER conversations when drafting, found by keyword over the local full-text index (no vector store, no second copy of the mailbox). Off by default because it widens what untrusted correspondence can reach a prompt from "the open thread" to "anything matching a word the user typed"; hits are phishing-filtered, capped at 4 snippets, and placed in the untrusted section of the prompt. The query is the user's instruction ONLY, never mail text. Admin-only. |
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
[vaf/core/config.py](../../vaf/core/config.py) directly - it is the single source of truth.
