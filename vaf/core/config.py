# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
import json
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# Single source of truth for legacy local-admin scope (before bootstrap sets real admin UUID)
LEGACY_LOCAL_ADMIN_SCOPE_ID = "00000000-0000-0000-0000-000000000001"


# ── Single source of truth for per-provider API models ────────────────────────
# `default` = used when the user hasn't picked a model; `fallback` = the static
# dropdown list shown when no live model fetch has happened (no key / offline /
# rate-limited). The live list (provider /v1/models) still takes precedence in the
# UI. Change a model HERE ONCE — every Python call site and the web UI read this
# (UI via GET /api/provider-models). `local` is intentionally absent (GGUF models
# are discovered from disk, not a fixed list).
PROVIDER_MODELS: dict[str, dict] = {
    "openai": {
        "default": "gpt-4o",
        "fallback": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
    },
    "anthropic": {
        "default": "claude-sonnet-4-6",
        "fallback": ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5"],
    },
    "deepseek": {
        "default": "deepseek-v4-flash",
        "fallback": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-auto"],
    },
    "google": {
        "default": "gemini-2.5-flash",
        "fallback": ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
    },
    "openrouter": {
        # OpenRouter uses DOTTED ids (claude-sonnet-4.6), unlike Anthropic's dashed ids.
        "default": "anthropic/claude-sonnet-4.6",
        "fallback": ["anthropic/claude-sonnet-4.6", "openai/gpt-4o", "google/gemini-2.5-flash"],
    },
    # First-party Veyllo API (OpenAI-compatible). `veyllo-chat` is multimodal — it handles both
    # text chat and image input, so the same provider/model serves chat and vision.
    "veyllo": {
        "default": "veyllo-chat",
        "fallback": ["veyllo-chat"],
    },
}


class Config:
    # In Docker mode, use dedicated config volume (NOT VAF-Space!)
    # VAF-Space = User data (NAS-like storage)
    # VAF-Config = System secrets (encryption keys, JWT) - admin only
    _docker_mode = os.environ.get("VAF_DOCKER_MODE", "").lower() == "true"
    _config_dir = os.environ.get("VAF_CONFIG_DIR", "/vaf-config")
    
    if _docker_mode and os.path.exists(_config_dir):
        APP_DIR = Path(_config_dir)
    else:
        APP_DIR = Path.home() / ".vaf"
    
    CONFIG_FILE = APP_DIR / "config.json"
    
    DEFAULTS = {
        "config_format_version": 1,  # bumped by vaf/core/migrations.py when the config format changes
        "update_check_on_start": True,  # one-line "update available" hint at startup (vaf update)
        "update_include_prereleases": None,  # `vaf update` prerelease tracking: None=auto (track prereleases iff the installed build is itself a prerelease), True=always, False=stable-only
        "web_search_cache_enabled": True,        # serve identical web_search queries from a short-lived cache
        "web_search_cache_ttl_seconds": 900,     # 15 minutes
        "git_coauthor_enabled": True,            # Co-authored-by trailer on commits VAF authors (project versioning, coder final commit, GitHub file commits); toggled from chat via set_git_coauthor
        "git_coauthor_identity": "VAF Agent <noreply@veyllo.app>",  # trailer identity; empty string disables the trailer like git_coauthor_enabled=False
        "model": "auto",  # "auto" = VRAM-adaptive local default: Qwen3.5-4B (<=10 GB VRAM) or Qwen3.5-9B (>10 GB), unsloth GGUF, quant auto-picked. Or set an explicit "repo/file.gguf".
        "provider": "local",
        "gpu_layers": -1,
        "auto_install_gpu": True,  # On an NVIDIA GPU without CUDA, auto-install CUDA llama-cpp-python (no terminal prompt). Set false to stay on CPU.
        "false_promise_detection_enabled": False,  # Forced retry when a model claims a tool but emits none. OFF by default (caused retry loops / false positives); set true to re-enable.
        "empty_response_retry_enabled": False,  # Local "Empty response detected -> snapshot and retry". OFF by default (noisy + false positives from messy <think>, esp. in background thinking runs). API empty-handling is unaffected.
        "action_tag_enabled": False,  # The <Action> declaration tag (model announces the tool before calling it; UI Action panel). OFF by default: not needed currently, and small local models (e.g. Qwen/Gemma 4B) tend to emit the <Action> block and then stop instead of calling the tool. Soft/optional convention -- nothing breaks when off (code + parser stay). See docs/agents/ACTION_TAG.md.
        "n_ctx": 32768,  # Minimum supported context window; load() clamps lower values up to this.
        "n_parallel": 0, # 0 = Auto-detect based on VRAM (1 or 2); Set to 1 to force sequential if crashing
        "llama_cache_ram": 4096,  # Prompt cache size in MB. 0 = disabled. -1 = auto (40% free RAM, cap 8192).
        "temperature": 0.7,
        # Local-generation sampling (llama-server). A repetition penalty + top_p/top_k prevent degenerate
        # loops where a reasoning model repeats the same text until it fills the context. Cloud APIs ignore
        # these (they are sent only on the local path).
        "repeat_penalty": 1.1,
        "top_p": 0.95,
        "top_k": 40,
        "max_generation_tokens": 10000,  # per-call output cap on local generation; bounds a runaway loop


        # AI Provider Settings
        # Options: "local", "veyllo", "openai", "anthropic", "deepseek", "google", "openrouter"
        "provider": "local",
        # Base URL for the Veyllo API (OpenAI-compatible). Overridable for staging/self-host.
        "veyllo_base_url": "https://api.veyllo.app/v1",

        # API Keys (Base64 encoded for basic obfuscation - NOT encryption!)
        # For production, consider using system keyring for API keys and tokens.
        "api_key_veyllo": "",  # Veyllo API server coming later
        "api_key_openai": "",
        "api_key_anthropic": "",
        "api_key_deepseek": "",
        "api_key_google": "",
        "api_key_openrouter": "",
        # ElevenLabs: speech only (TTS/STT via speech_api.py). NOT an LLM
        # provider - never add it to PROVIDER_MODELS or the LLM provider UI.
        "api_key_elevenlabs": "",
        # Web Search API Keys (optional; when set, used before scrape/DDG)
        "api_key_brave_search": "",
        "api_key_google_search": "",
        "google_search_engine_id": "",
        
        # API Model Selection per Provider
        # Defaults derive from PROVIDER_MODELS (single source of truth — see top of file).
        "api_model_veyllo": PROVIDER_MODELS["veyllo"]["default"],
        "api_model_openai": PROVIDER_MODELS["openai"]["default"],
        "api_model_anthropic": PROVIDER_MODELS["anthropic"]["default"],
        "api_model_deepseek": PROVIDER_MODELS["deepseek"]["default"],  # deepseek-chat deprecated 2026-07-24
        "api_model_google": PROVIDER_MODELS["google"]["default"],
        "api_model_openrouter": PROVIDER_MODELS["openrouter"]["default"],

        # Vision Model Fallback — used when the primary provider does not support image input.
        # Example: primary = deepseek (no vision) → vision_provider = google / openai / anthropic.
        # Leave empty to keep current behavior (strip images + show error to user).
        "vision_provider": "",   # "veyllo"/"google"/"openai"/"anthropic"/"openrouter", or "local":
                                 # the llama server is launched with the model's mmproj projector
                                 # (backend.resolve_mmproj_for) and sees images itself.
        "vision_model": "",      # e.g. "gemini-2.5-flash", "gpt-4o" — leave empty for provider default
        # Local vision projector ref "owner/repo/file.gguf"; empty = derived from the
        # model's known repo (mmproj-F16.gguf). Admin-only: it is a server LAUNCH argument.
        "vision_local_mmproj": "",
        # Voice-agent LLM lane (live call first layer) - vision_provider pattern:
        # "" = ride the main provider (local main = time-share the one llama server);
        # "local" = a DEDICATED local GGUF for the call (the one server SWAPS models:
        #           voice model during the call, main model while a delegated task runs
        #           - never two servers, never two concurrent inferences);
        # any API provider id = the call runs on that API regardless of the main provider.
        # Owner-approved adaptive voice learning: a YES to "was that your
        # voice?" (authenticated web/messenger answer) feeds the confirmed
        # segment into the owner profile (speaker_id.add_owner_sample:
        # similarity floor, sample cap, enrollment-weighted blend). The
        # voice itself can never trigger this. Kill switch, default on.
        "speaker_id_adaptive_enabled": True,
        "voice_agent_provider": "",
        # For "local": a downloaded model filename from models/, picked in
        # Settings > Voice (empty = the recommended default, see voice_model.py;
        # fetched on selection). A full HF ref "owner/repo/file.gguf" is still
        # accepted (back-compat). For an API provider: model name (empty = provider default).
        "voice_agent_model": "",
        # Image downscaling before send: full-res photos make providers 500 and waste tokens.
        # Only images whose longest edge exceeds max_edge are shrunk (small images untouched).
        "vision_image_max_edge": 2000,      # px; OpenAI internally caps high-detail at ~2048
        "vision_image_jpeg_quality": 85,    # re-encode quality when downscaling
        # Vision strategy for chat images (token-efficient by default):
        #   "description_tool" — the main model is TEXT-ONLY. An attached image is run once
        #       through the vision backend → a base description that is injected as text; the
        #       model calls the analyze_image tool to inspect the image on demand. No raw bytes
        #       are ever re-sent to the main model. Works even with non-vision main providers.
        #   "inline_multimodal" — legacy: send the raw image straight to a multimodal main model.
        "vision_mode": "description_tool",
        "vision_description_max_tokens": 1024,  # output bound for the base description + analyze_image

        # OpenAI-compatible request resilience (openai/deepseek/openrouter/local).
        "api_retry_attempts": 2,            # VAF-level retries on transient 5xx/timeout (atop the SDK's own)
        "api_timeout_connect": 20.0,        # s — bound connect so a huge upload can't hang
        "api_timeout_write": 120.0,         # s — bound the upload (request body) phase
        "api_timeout_read": 600.0,          # s — KEEP generous: reasoning models stream for minutes
        "api_timeout_pool": 20.0,           # s — connection-pool acquire
        "api_retry_after_max": 30,          # s — cap honored on a 429 Retry-After header (avoid huge sleeps)

        # Provider failover (Settings → Advanced → Failover). Off by default → no behaviour change.
        # On a failure BEFORE the first token, the request is retried down a provider chain.
        "failover_level": "off",            # "off" | "basic" (→local) | "balanced" (→backup→local) | "maximum"
        "failover_backup_provider": "",     # provider id for the backup API link (e.g. "anthropic"); "" = none
        "failover_backup_model": "",        # model for the backup link; "" = that provider's default
        "failover_local_model": "",         # GGUF filename for the local link; "" = auto
        "failover_timeout_s": 30,           # s — first-token deadline before failing over (0 = no extra deadline)
        "failover_triggers": [],            # subset of ["timeout","rate_limit","server_error"]; [] = any error
        "failover_return_to_primary": True, # prefer the primary again on the next request after a failover

        # Sub-Agent Provider Configuration
        "subagent_provider": "inherit",  # Options: "inherit", or any provider name
        "subagent_use_separate_provider": False,
        "subagent_model": "",  # Hybrid mode: model for tools/workflows (empty = same as main chat)
        # Global kill-switch for the chat-while-subagent-runs behavior (the SUB-AGENT ACTIVE
        # prompt block + UI hint). Renders only in API mode regardless (code gate: the MAIN
        # provider must not be "local" AND api_backend must be initialized — on local the
        # single llama server would serve two inferences at once). Admin-only via the
        # "subagent_" prefix in GLOBAL_CONFIG_KEY_PREFIXES.
        "subagent_concurrent_chat_enabled": True,
        
        # Auto-start local llama-server (disable if only using APIs)
        "auto_start_local_server": True,

        # Tool Router cap — max number of tools passed to the agent per turn.
        # list_tools and search_tools are always included on top of this limit.
        # Lower = faster LLM inference + less context pollution. Range: 1–100.
        "router_max_tools": 12,

        # Terminal colour theme, shared by both terminal lanes. The catalog is
        # vaf/cli/themes.py; `t` in the app and `theme <name>` write this key.
        "theme": "vaf",

        # Terminal UI lane for `vaf run`: "app" opens the full-screen terminal
        # app, "modern" the prompt-toolkit lane, "classic" the plain prompt.
        # The `vaf run --classic` flag overrides this per invocation.
        "tui_mode": "app",

        # UX toggles (opt-in; off by default – user must enable)
        # Auto open web search source links in the user's default browser (tabs)
        "ux_auto_open_links": False,
        # Auto open created output folders/files (file explorer / browser for html)
        "ux_auto_open_outputs": True,
        # Safety cap for tabs opened automatically
        "ux_auto_open_max_tabs": 8,
        # Voice capture in the terminal app: False = the transcript is sent
        # immediately (the classic flow); True = it lands in the input box for
        # editing first. A ux_ key on purpose - per-user preference, not an
        # admin-gated speech_stt_* engine setting.
        "ux_voice_review": False,
        # Run each sub-agent in its own terminal window
        "sub_agents_in_separate_terminals": True,
                # Sub-Agent timeout settings
                "subagent_timeout_enabled": True,      # Enable/disable timeout for sub-agents
                "subagent_timeout_minutes": 120,       # Legacy IPC zombie-cleanup window (NOT the in-line wait)
                # Bounded tool execution: hard wall-clock limits for a single in-line
                # tool/sub-agent call so one blocking call can never freeze the worker.
                # Enforced by vaf.core.bounded_run.run_bounded.
                "tool_timeout_seconds": 120,           # generic in-process tool call
                "subagent_timeout_seconds": 300,       # research/coding/document sub-agent step
                "workflow_generation_timeout_seconds": 30,   # create_automation: bound the inline workflow-gen Agent (fast-fail to prompt-based; was 90s, too slow on reasoning providers)
                # Prompt-based automation runs: bound the whole turn (runaway guard). 180 was
                # unrealistic for real tasks (mails + searches + coder + delivery need minutes)
                # and produced timeout-then-double-delivery incidents; on timeout the runner now
                # waits a bounded grace for the abandoned worker before giving up honestly.
                "automation_run_timeout_seconds": 600,
                "librarian_timeout_seconds": 60,       # filesystem agent — should be fast
                "browser_timeout_seconds": 1800,       # worst-case hard cap (30 min); liveness is the real guard
                "tool_stop_poll_seconds": 0.5,         # how often the bounded wait checks stop/deadline
                # Liveness, not hard caps: a spawned sub-agent pulses a heartbeat every ~3 s.
                # If none arrives for this long, it's dead/stuck → kill the child + fail fast
                # (don't wait out the hard cap). This is the primary guard; the timeouts above
                # are only the worst-case ceiling.
                "subagent_liveness_timeout_seconds": 60,

                # Per-step workflow output validation: an opt-in LLM check that a content/agent
                # step's output actually fulfils the step's goal, retried with a correction hint
                # up to N times, then the last version is accepted and the workflow continues.
                "workflow_step_validation_enabled": True,      # global kill-switch
                "workflow_step_validation_max_retries": 3,     # retries before accepting the result

                # Result grounding: catch a reply that claims a concrete tool OUTCOME (succeeded /
                # failed / saved / "N results" / a specific error) the turn's actual tool results do
                # not support — including a result for a tool that was never run this turn. On a
                # mismatch the reply is bounced back for correction (capped, then it proceeds).
                "result_grounding_enabled": True,              # global kill-switch
                "result_grounding_max_retries": 2,             # corrections before proceeding anyway

                # Current-step reminder: each turn, surface the agent's current plan step (the first
                # pending task in working memory) with the index to mark it done, so any model
                # follows its plan step by step instead of skipping or abandoning it. Silent when no
                # pending task exists (no nagging on plain chat).
                "plan_step_reminder_enabled": True,            # global kill-switch

                # Plan gate (main agent only): a state-changing tool (permission_level write or
                # dangerous, except python_sandbox) is blocked until a plan exists in working memory
                # — "explore freely (read/search), plan before you act". Satisfied in the same turn
                # by calling update_working_memory(plan=[...]) first; after plan_gate_max_blocks
                # consecutive blocks it proceeds anyway so nothing hard-locks. Sub-agents are never
                # gated (their own loops are untouched).
                "plan_gate_enabled": True,                     # global kill-switch
                "plan_gate_max_blocks": 3,                     # blocks before proceeding without a plan

                # Team-await note (main agent): when a reply claims the task is complete while a
                # sub-agent is genuinely still running (fresh heartbeat), the reply is KEPT as-is
                # (a streamed answer is never erased or regenerated) and a system note is appended
                # so the next turn does not build on a false "done". Crashed/stale sub-agents are
                # reaped first (check_zombies) so they never trigger the note.
                "team_await_enabled": True,                    # global kill-switch for the note

                # Anti-spin guard (main agent): a weak model can churn the bookkeeping tools
                # (update_working_memory / update_intent / add_task) over and over — re-planning
                # the same task with slightly varying text — without ever calling the tool that
                # does the actual work. The redundant-call block needs EXACT args and the emergency
                # breaker needs <5s, so neither catches this slow near-duplicate planning spin. We
                # count CONSECUTIVE bookkeeping calls (any other tool resets it): nudge at the
                # threshold, then disable tools for one turn so the model must act or answer.
                "anti_spin_enabled": True,                     # global kill-switch
                "anti_spin_max_planning_calls": 4,             # consecutive plan/intent calls before nudging
                "nonprogress_max_turns": 6,                    # consecutive read-only/verify-only tool turns before nudging then forcing an answer (catches the "verify forever" loop)
                "chat_step_wall_clock_seconds": 3600,          # MAIN-loop wall-clock BACKSTOP (1h): a single user turn can never grind past this (checked at each tool-turn boundary), independent of tool count/provider speed. Deliberately generous — the no-progress guard + per-tool timeouts stop the common case far earlier; this only catches a true infinite/zombie loop without ever aborting legitimate long work. Configurable.

                # Out-of-order drift nudge: when the agent marks a later task done while an earlier
                # one is still pending, update_working_memory appends a soft "did you skip it?" hint
                # to its result (a reminder, never a block).
                "plan_drift_nudge_enabled": True,              # global kill-switch

                # Plan-without-tasks reminder: plan = the high-level approach, tasks = the concrete
                # tracked steps (steps never belong in the plan). When the agent has a plan but no
                # tasks, a per-turn line tells it to break the plan into tasks so each step is tracked
                # and enforced. Silent once any task exists (the current-step reminder takes over) or
                # when there is no plan (plain chat).
                "plan_without_tasks_reminder_enabled": True,   # global kill-switch

                # Pending-task auto-continue (main agent): when the model gives a final text answer
                # but still has pending tasks in working memory, re-inject the current-step nugget as a
                # system "continue" message and keep working INSIDE the same user turn instead of
                # yielding (otherwise the nugget only re-fires on the next user message and the task
                # list sits unworked). Shares the tool_turn_count budget (soft 50 / hard 75) — no
                # parallel counter. Brakes: a genuine question to the user (answer ends with "?"),
                # background thinking pass, and this kill-switch.
                "autocontinue_pending_tasks_enabled": True,    # global kill-switch
                # Stage-3 brake for the above: in the foreground Web UI a clarifying question is plain
                # text (no tool signal), so a tiny validation LLM judges whether the reply is a
                # blocking question to the user before auto-continuing. Off -> last-line "?" heuristic.
                "autocontinue_question_classifier_enabled": True,

                # Incident 2026-07-13 gates (both kill-switches, default on):
                # (a) a reply to a tracked background question that is not a CLEAR
                # affirmative must not mutate stored state or delegate destructive work;
                "proactive_reply_mutation_gate_enabled": True,
                # (c) once the agent's reply asked the user a blocking question, synthetic
                # drain turns must not launch new write-level tools until the user answers.
                "ask_first_drain_gate_enabled": True,

                # Task-overwrite guard: replacing the whole task list (tasks=[...]) while steps are
                # still pending can silently drop work in progress. The first such replace is bounced
                # once with the pending steps listed ("are you sure?"); a re-call within the window
                # confirms and proceeds. Never a hard lock.
                "task_overwrite_guard_enabled": True,          # global kill-switch
                "task_overwrite_confirm_window_seconds": 120,  # re-call within this window = confirmed

                # MCP native tools: discover the tools of servers in mcp_servers.json at startup and
                # register each as a native tool (mcp_<server>_<tool>). The raw mcp_call tool stays
                # available regardless. Discovery is parallel with a per-server timeout; a slow/hung
                # server is skipped and never blocks startup.
                "mcp_native_tools_enabled": True,              # global kill-switch
                "mcp_discovery_timeout_seconds": 5,            # per-batch discovery deadline

                # Voice / STT Settings
                "stt_enabled": False,                  # Legacy STT toggle (kept: ORed with speech_stt_enabled in speech.py)
                "speech_stt_enabled": False,           # Enable Speech-to-Text (canonical; admin-only via speech_stt_ prefix)
                "speech_stt_engine": "docker",         # STT engine: "docker" (default) or "local" (faster-whisper)
                "speech_stt_docker_url": "http://localhost:5003",  # When engine=docker; STT container port 5003 (maps to 9000)

                # Cloud STT provider lane (mirrors vision_provider). "" = local engine above.
                # Consulted BEFORE speech_stt_engine; on API failure (402/429/timeout)
                # the local lane is used automatically (speech_api.py never raises).
                "speech_stt_provider": "",             # "" | "elevenlabs" | "openai"
                "speech_stt_api_model": "",            # "" = provider default (scribe_v2 / whisper-1)

                # STT (Whisper) - only when engine=local; keep "base" to avoid 20GB+ spikes
                "speech_stt_whisper_model": "base",    # faster-whisper: tiny, base, small, medium, large-v3

                # TTS Settings (Web UI uses Docker TTS by default; piper=local, system=pyttsx3, docker=HTTP in Docker)
                "speech_tts_enabled": False,           # Enable Text-to-Speech
                "speech_tts_engine": "docker",         # TTS engine: "docker" (default), "piper", or "system"
                "speech_tts_docker_url": "http://localhost:5002",  # Default/fallback TTS URL
                "speech_tts_docker_url_de": "http://localhost:5002",   # German voice (optional)
                "speech_tts_docker_url_en": "http://localhost:5004",   # English voice (optional)
                "speech_tts_docker_url_fr": "http://localhost:5006",   # French voice (optional)
                "speech_tts_chatterbox_url": "http://localhost:4123",  # When engine=chatterbox (HTTP TTS server)

                # Speaker identification (enroll-and-verify, sherpa-onnx local lane).
                # ON by default as a kill-switch only: the REAL opt-in is the explicit
                # enrollment - without a stored voice profile this flag is inert and
                # no model is ever loaded (fail-closed per profile, not per flag).
                "speaker_id_enabled": True,            # Master gate for voice-profile labeling
                "speaker_id_threshold": 0.60,          # Cosine score >= threshold -> the enrolled user
                "speaker_id_band": 0.05,               # Uncertainty band below threshold -> "unsure"
                "speaker_id_confirmation_enabled": True,  # Ask the owner on "unsure" (messenger/web card)

                # Voice reflex awareness (docs/agents/VOICE_REFLEX.md). User-writable
                # preferences (not admin/billing/security): they only scale a LOCAL
                # policy threshold and never redirect inference or spend API quota.
                "voice_awareness_activity": 0.5,       # ONE dial 0..1 (quiet..active): how readily the agent chimes in on interesting overheard talk. At 0 it only takes notes (never interrupts).
                "voice_awareness_topics": [],          # The owner's interest topics; a proactive chime-in must embedding-match one of these (empty = the agent never chimes in unprompted)
                "voice_semantic_endpoint_enabled": False,  # Server-side semantic turn-end (Smart Turn v3 ONNX, CPU): the browser streams mic PCM during a call and the server proposes the endpoint from prosody instead of a fixed silence timer. Off = today's browser timer only.

                # Cloud TTS provider lane (mirrors vision_provider). "" = local engine above.
                "speech_tts_provider": "",             # "" | "elevenlabs" | "openai"
                "speech_tts_api_model": "",            # "" = provider default (eleven_flash_v2_5 / gpt-4o-mini-tts)
                "speech_tts_api_voice": "",            # "" = provider default (ElevenLabs Rachel / OpenAI alloy)
                "tts_auto_speak": False,               # Auto-speak agent responses in browser
                
                # Librarian Agent settings
                "librarian_max_pdf_size_mb": 50,       # Max PDF size in MB (default: 50)
        "librarian_max_doc_size_mb": 20,       # Max Word/PowerPoint size in MB (default: 20)
        "librarian_max_excel_size_mb": 30,     # Max Excel size in MB (default: 30)
        "librarian_max_text_size_kb": 500,     # Max text file size in KB (default: 500)
        "document_conversion_docker_url": "http://localhost:5005",  # Gotenberg: DOCX/XLSX/PPTX → PDF (LibreOffice in Docker)
        "librarian_auto_chunk_large_files": True,  # Auto-chunk large files (default: True)
        "librarian_pdf_max_pages_preview": 50, # Max pages to show in preview (default: 50)
        "librarian_ocr_fallback_for_pdf": True,  # OCR scanned PDFs in the librarian read path (engine via ocr_engine: tesseract or the vision model)

        # System Settings
        "server_mode": False,                  # True = server installation (LAN always on, no desktop UI controls)
        "web_ui_enabled": True,                # Start Web UI automatically
        "server_persistence_enabled": False,   # Keep server running after exit
        "tray_autostart": False,               # Auto-start tray on OS login
        "debug_logs_enabled": True,            # Write domain logs, timeline and queue.log; ON by default (the log GC bounds disk use). No UI toggle — user opt-out via config.json.
        "parallel_main_workers": 1,            # Main headless workers (1=legacy serialized, 2=weighted-fair parallel)
        "queue_policy": "legacy",              # legacy | weighted_fair
        "queue_weight_interactive": 5,         # Used when queue_policy=weighted_fair
        "queue_weight_automation": 3,          # Used when queue_policy=weighted_fair
        "queue_weight_background": 1,          # Used when queue_policy=weighted_fair
        # Per-provider hard cap on effective concurrent workers (clamps parallel_main_workers by provider).
        "max_parallel_api_workers": 5,         # API providers: up to N users' turns run at once
        "max_parallel_local_workers": 2,       # local llama.cpp: keep <= server --parallel slots (VRAM safety)
        "server_idle_timeout": 15,             # Unload local model after idle seconds (Web UI / CLI)
        "telegram_idle_timeout": 120,          # Keep model loaded this long after last Telegram prompt when no Web connections (seconds)
        "telegram_debounce_seconds": 5,        # Wait this long for follow-up messages; combine into one prompt per chat

        # Thinking mode: background reflection when user idle
        "thinking_enabled": True,                              # Enable thinking mode when idle
        "thinking_idle_minutes": 10,                           # Start after this many minutes without activity
        "thinking_max_idle_age_hours": 168,                    # Upper bound: skip scope IDs silent longer than this (default 7 days). Filters stale/orphan web-session UUIDs that would otherwise run forever. 0 disables the cap.
        "thinking_check_interval_seconds": 60,                 # How often to check for idle users
        "thinking_automation_buffer_minutes": 10,              # Do not start if automation runs within this many minutes
        "thinking_max_duration_minutes": 30,                  # Max duration per thinking run (then release lock)
        "thinking_wait_nudge_minutes": 3,                     # If user does not reply to a question: send nudge after this many minutes
        "thinking_followup_max": 3,                            # Re-ask an unanswered proactive question up to N times (pointed follow-up), then let the topic rest
        "default_language": "",                                # Fallback language for backend canned phrases (vocab book) when the user has no preferred_language; empty -> 'en'
        "thinking_wait_skip_minutes": 10,                     # If still no reply after this many minutes total: skip the question and do other things
        "thinking_reply_wait_ttl_hours": 12,                  # Safety net: a waiting-for-reply latch older than this is expired at read time (the 10-min skip only runs when a thinking run fires); 0 disables
        "workflow_agent_step_timeout_seconds": 1800,          # Worst-case hard cap for a heavy agent step (coder/research/document) INSIDE a workflow; dead children are caught much earlier by heartbeat liveness
        "workflow_identity_injection": "declared",           # Who a workflow's tools think is calling. 'declared' (default) = they pass the real one, distributed by each tool's identity_kwargs; 'legacy' = the four consumers that never passed an identity keep passing none, i.e. a saved workflow runs as the machine owner whoever started it. Only 'legacy' and 'off' count as a rollback - any other value means 'declared', because 'as before' is the leaky state. NOT a boolean: the three lanes that always passed an identity are unaffected either way
        "thinking_nudge_activity_minutes": 5,                # Do not nudge if user was active on any channel in the last N minutes
        "thinking_provider": "inherit",                      # AI provider for thinking mode ('inherit' or e.g. 'openai', 'local')
        "thinking_model": None,                              # Specific model for thinking mode (None = use provider default)
        "thinking_cooldown_minutes": 110,                    # After a thinking run completes: wait this many minutes before starting another
        "thinking_gc_hours": 12,                              # GC deletes thinking-mode sessions older than this many hours
        "thinking_quiet_hours_enabled": False,               # Do not run thinking mode during quiet hours (local time)
        "thinking_quiet_hours_start": "23:00",                # Quiet period start (HH:MM, 24h); e.g. 23:00 = 11 PM
        "thinking_quiet_hours_end": "07:00",                 # Quiet period end (HH:MM, 24h); e.g. 07:00 = 7 AM (overnight span supported)
        "thinking_gate_enabled": True,                       # Completion gate: nudge once if a captured note/todo is still unhandled before thinking_done
        "thinking_read_cap_enabled": True,                   # Block excessive read/gather tool calls in a thinking run (memory_search/web_search spin etc.)
        "thinking_read_cap_per_tool": 3,                     # Nth call of a read tool (memory_search/web_search/list_*) within one step is blocked
        "thinking_no_progress_turns": 5,                     # After this many turns with no decisive (act/ask/clear) tool, force a single-tool decision
        "model_unload_idle_minutes": 30,                     # Desktop only: unload the local model after the user is really away (no message) this long, once thinking is idle. Server/headless never unloads.
        "thinking_proactive_enabled": True,                  # When the floor (notes/todos) is clear, run a proactive memory-mined suggestion scan (Stufe 2)
        "thinking_proactive_evidence_min_chars": 24,         # Evidence-gate (LOCAL/weak model): a proactive suggestion's message/details must quote >= this many chars verbatim from real retrieved memory/history
        "thinking_proactive_evidence_min_chars_api": 12,     # Evidence-gate when the thinking run uses a HOSTED/strong model (fabricates rarely -> lenient bar); selected automatically by provider
        "thinking_proactive_min_runs": 6,                    # DEPRECATED: rate-limiting no longer silences runs (silence is never the goal); repeats are prevented by the recent/declined dedup prompts. Unused.
        "thinking_proactive_memory_k": 4,                    # Per-query top-K when the proactive step pre-fetches real memories to hand the model (it may also memory_search once itself)
        # Semantic de-duplication of proactive questions: text-based "don't repeat" only blocks the same wording,
        # so the model kept re-asking the SAME topic reworded (always "work/VAF"). This embeds the candidate
        # question and rejects it when it is too similar to a recently asked/declined one, forcing the model to
        # pick a genuinely different area. Reuses the SAME embedding singleton the run already uses every run
        # (no new vector lane); fail-open; the last get-to-know attempt bypasses the gate so a run never ends silent.
        "thinking_question_dedup_enabled": True,             # Master kill-switch for the semantic question-dedup (also requires memory_enabled)
        "thinking_question_similarity_threshold": 0.80,      # Cosine >= this vs a recent question -> reject as too similar (MiniLM runs ~0.78-0.85; tune per deployment)
        "thinking_question_similarity_runs": 12,             # Compare against questions asked within this many recent runs
        "thinking_question_similarity_max_compare": 12,      # Hard cap on how many recent questions are embedded/compared per turn (leak/cost bound)
        "thinking_getto_max_attempts": 3,                    # Get-to-know retries that enforce dedup before the gate is bypassed; the bypass also fires on the loop's last turn, so a low turn budget can never cause silence

        # Garbage Collector Settings
        "gc_enabled": True,                    # Enable automatic temp file / log cleanup
        "gc_interval_hours": 12,               # Run GC every N hours
        "gc_max_age_hours": 48,                # Delete files older than N hours
        # Security logs are an AUDIT TRAIL, so they outlive ordinary logs by a wide
        # margin. They share the app log directory and the dated-name convention, so
        # without their own retention the 48-hour rule swept them up - measured: not a
        # single security_events_*.jsonl survived on this machine. A trail that erases
        # itself after two days cannot answer "what happened last week".
        "security_log_retention_days": 14,     # Security logs are kept this long (audit trail)

        # Cloud Storage Sync Settings
        "cloud_sync_enabled": False,                               # Enable cloud storage sync feature
        "cloud_sync_interval_minutes": 15,                         # Background sync interval
        "cloud_sync_max_file_size_mb": 100,                        # Max file size to sync (MB)
        "cloud_sync_conflict_resolution": "last_write_wins",       # "last_write_wins" or "keep_both"
        "cloud_oauth_google_client_id": "827949283932-0l83lmf1ip671vqta9d6m9k2fa4gii42.apps.googleusercontent.com",  # Built-in client ID for developers; UI shows empty so users aren't confused
        "cloud_oauth_google_client_secret": "",                    # Optional — Desktop apps don't require a secret
        "cloud_oauth_microsoft_client_id": "",                     # OneDrive OAuth client ID
        "cloud_oauth_microsoft_client_secret": "",                 # OneDrive OAuth client secret
        "cloud_oauth_dropbox_client_id": "",                       # Dropbox OAuth app key
        "cloud_oauth_dropbox_client_secret": "",                   # Dropbox OAuth app secret
        "cloud_credentials_key": "",                               # AES-256 key for cloud credential fallback (auto-generated)
        "cloud_oauth_callback_base_url": "",                       # Override redirect_uri base (e.g. for proxy)
        "cloud_config": None,                                      # Cloud account list (local admin)
        "cloud_config_by_user": {},                                # Per-user cloud account lists

        # Memory System Settings (RAG + Vector Search)
        "memory_enabled": True,                                    # Enable memory system
        "memory_rag_refine_query": True,                           # Refine vague queries (e.g. "who am I") for better RAG hits
        "memory_rag_k": 5,                                        # Max RAG snippets per query (1-20); applies to chat, gateway, automation
        "memory_rag_threshold": 0.3,                               # Min relevance score (0.0-1.0); only snippets >= this % are in RAG results. 0.3 = 30%
        # At-rest encryption of the file stores (chats, context archives, handoff
        # bundles, sub-agent queue, working memory). Reading always tolerates BOTH
        # forms, so older plaintext chats keep opening and turning this off does not
        # strand the files already encrypted. Embedders set it per deployment.
        "cli_password_gate": True,                                 # Interactive terminal (vaf run / TUI) asks for the admin password. Scripts, -p, tray and automations never do
        "secure_store_kek_backend": "auto",                        # Where the master key lives: "auto" (keyring on Windows where chmod protects nothing, file elsewhere), "file" (0600), or "keyring" (OS keyring)
        "allow_plaintext_at_rest": True,                           # Accept files WITHOUT the encryption header on read. Needed while migrating; the sweep turns it off after a clean pass
        "file_encryption_enabled": True,
        "prompt_log_full_enabled": False,                          # Log the ENTIRE assembled system prompt (profile, retrieved memories, contacts) to prompt_*.log. Debug only
        "context_archive_max_age_days": 14,                        # Age sweep for pre-compression conversation snapshots (0 = keep forever)
        "cross_chat_hint_enabled": True,                           # Cross Chat Hint: pointers from this user's OTHER chats, below the RAG snippets
        "cross_chat_hint_k": 2,                                    # Max cross-chat hints per turn (0 disables the lane entirely)
        "cross_chat_hint_min_terms": 2,                            # Distinct query terms a chat must match; a single rare term also qualifies
        "cross_chat_hint_min_score": 0.45,                         # Min share of the question's informative terms a chat must cover (0.0-1.0)
        "cross_chat_hint_max_age_days": 30,                        # Chats older than this are not scanned
        "memory_hybrid_enabled": True,                             # Long-term RAG: enable vector+lexical hybrid fusion (RRF)
        "memory_hybrid_rrf_k": 60,                                 # RRF denominator constant (typical default: 60)
        "memory_hybrid_lexical_k": 20,                             # Max lexical candidates retained before fusion
        "memory_hybrid_lexical_scan_limit": 400,                   # Max lexical rows scanned for hybrid retrieval
        "memory_hybrid_lexical_min_score": 0.05,                   # Min lexical score (0.0-1.0) before fusion; 0.05 filters zero-overlap noise conservatively
        "memory_auto_capture": False,                               # DISABLED: Auto-capture causes memory spikes (investigating)
        "memory_compaction_enabled": True,                          # Session compaction: prompt to store durable memories every N turns
        "memory_compaction_interval": 15,                           # Run compaction every N user/assistant turns
        "memory_compaction_max_tokens": 4000,                       # Max tokens for compaction LLM reply (more MEMORY: lines; API/local/server)
        "resume_compaction_enabled": True,                          # Append deterministic resume block after context compression/checkpoint
        "memory_db_url": "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory",  # App DATA connection (per-user). At the RLS cutover this becomes the non-superuser role.
        "memory_db_owner_url": "",                                  # Owner/superuser DSN for DDL/migrations/global stats. Empty -> falls back to memory_db_url (correct before cutover); at cutover set this to the OWNER dsn while memory_db_url switches to the non-super role.
        "memory_encryption_key": "",                               # AES-256 key (Base64). Minted once when a CLEANLY-PARSED config genuinely lacks it; an unreadable config refuses to mint (vaf/memory/crypto.py)
        "memory_embedding_model": "all-MiniLM-L6-v2",             # Sentence-transformers model
        "memory_auto_connect_threshold": 0.7,                      # Cosine similarity threshold for auto-connections
        "memory_chunk_size": 512,                                  # Chunk size in tokens
        "memory_chunk_overlap": 50,                                # Chunk overlap in tokens
        "memory_db_echo": False,                                   # Enable SQL query logging (debug)
        # Attachment RAG lane (session-scoped, ephemeral, isolated from long-term memory lane)
        "attachment_rag_enabled": True,                             # Attachment lane enabled by default after staged stability validation
        "attachment_rag_k": 4,                                      # Top-k attachment snippets per query (1-12)
        "attachment_rag_threshold": 0.28,                           # Min similarity for attachment snippet retrieval
        "attachment_rag_lexical_min_score": 0.05,                   # Min lexical score for attachment lexical retrieval (safe mode + hybrid lexical candidates)
        "attachment_rag_ttl_hours": 24,                             # TTL for ephemeral attachment index
        "attachment_rag_max_chars_per_doc": 24000,                 # Max chars per attached doc indexed into ephemeral lane
        "attachment_rag_snippet_chars": 900,                        # Max chars per retrieved attachment snippet inserted into prompt
        "attachment_rag_max_rss_gb": 4.0,                           # Hard guard: kill attachment lane when process RSS exceeds this limit
        # Hierarchical document indexing (two-tier: section summaries → chunks)
        "attachment_rag_hierarchical_enabled": True,                # On by default: build section index for large structured docs (vector mode only)
        "attachment_rag_hierarchical_min_chars": 4000,              # Min doc length to activate hierarchical indexing (chars)
        "attachment_rag_hierarchical_max_sections": 15,             # Max sections to index per document (2-50)
        "attachment_rag_hierarchical_coarse_k": 3,                  # Top-k sections selected in Tier 1 retrieval (1-10)

        # Document learning (learn_document + attachment transfer). Deliberate:
        # the DEFAULT is to learn EVERYTHING - a cap is opt-in (0 = no cap), and
        # when a cap bites, the tool's reply names the key and reports "X of Y"
        # instead of a bare success count. These keys existed only as inline
        # fallbacks before (silent 200 pages / 40 sections = 3.9% of a 1000-page
        # book learned and reported as success).
        "learn_document_max_pages": 0,                              # PDF pages extracted for learning; 0 = all pages
        "learn_max_sections": 0,                                    # Sections stored per document; 0 = all sections
        "learn_batch_pages": 10,                                    # Pages per learn batch (clamped 2-100): one progress tick + one DB commit
        "memory_document_extraction_max_tokens": 1200,              # Max tokens per per-section extraction LLM call (clamped 400-4000 in agent.py)

        # OCR for scanned PDFs (resolver: pdf_extract.resolve_ocr_engine).
        "ocr_engine": "auto",                                       # "auto" (tesseract if present, else vision model), "tesseract", or "vision"
        "ocr_vision_max_pages_per_call": 10,                        # Vision OCR = one model call PER PAGE; per-call cost guard, named in the output when it cuts

        # Redis Cache Settings
        "redis_url": "redis://localhost:6379/0",                   # Redis connection URL
        "redis_enabled": True,                                     # Enable Redis caching
        
        # Local Admin Settings (for localhost without login)
        # user_identity.json and RAG/memory scope use these when no auth (local only)
        "local_admin_scope_id": LEGACY_LOCAL_ADMIN_SCOPE_ID,  # Set to admin UUID by bootstrap; fallback for fresh installs
        "local_admin_username": "admin",  # Username for ~/.vaf/users/<this>/user_identity.json when local (WebSocket + HTTP API)

        # Local Network Settings
        "local_network_enabled": False,                            # Enable local network access (LAN only)
        "local_network_force_enabled": False,                      # If True, always keep local_network_enabled=True (cannot be turned off by UI/API)
        "local_network_port": 8001,                                # Backend port for local network
        "local_network_port_frontend": 3000,                       # Frontend port for local network
        "local_network_firewall_enabled": True,                    # Enable OS firewall rules
        "local_network_require_2fa": True,                         # Require 2FA for network users
        "local_network_jwt_secret": "",                            # JWT secret (auto-generated if empty)
        "local_network_jwt_expiry_hours": 24,                      # JWT token expiry in hours
        "local_network_rate_limit_attempts": 5,                    # Max failed login attempts
        "local_network_rate_limit_window_minutes": 15,             # Rate limit window in minutes
        "local_network_tls_enabled": False,                       # Serve backend over HTTPS/WSS (need cert + key)
        "local_network_ssl_cert": "",                             # Path to PEM certificate file (e.g. cert.pem)
        "local_network_ssl_key": "",                              # Path to PEM private key file (e.g. key.pem)
        "local_network_https_port": 443,                           # Port for integrated HTTPS proxy (no Nginx); 8443 if 443 needs admin
        
        # Docker Settings (Desktop Mode only)
        # Note: CLI mode (vaf run) always runs natively with full host access
        # Docker mode is only for Desktop/Tray mode for isolation
        "use_docker": True,                                        # Desktop: Run backend/frontend in Docker

        # Connections: Telegram (bot token, whitelist per user_scope_id)
        "telegram_config": None,                                   # { bot_token, enabled, verified?, whitelist: [...] }
        # Connections: WhatsApp (Baileys via Node, per-user auth, whitelist with phone_number)
        "whatsapp_config": None,                                   # { enabled, whitelist: [{ phone_number, user_scope_id, vaf_username }] }
        # Per-user connection toggles (sliders). Only non-admins use this; admin uses global telegram/whatsapp/discord_config.enabled.
        "connection_enabled_by_scope": None,                       # { "<user_scope_id>": { "telegram": bool, "whatsapp": bool, "discord": bool } }
        # Channel ingress policy (default-deny / explicit pairing).
        # mode:
        #   - "paired_only": allow only explicitly paired senders (whitelist/verified admin)
        #   - "permissive": allow explicit pairs and contact fallback
        # Per-channel mode can be "inherit", "paired_only", or "permissive".
        "channel_ingress_policy": {
            "mode": "paired_only",
            "throttle_seconds": 60,
            "telegram": {"mode": "inherit", "allow_contact_fallback": False},
            "whatsapp": {"mode": "inherit", "allow_contact_fallback": False},
            "discord": {"mode": "inherit", "allow_contact_fallback": False},
        },

        # Messaging-channel tool access. By default, channel sessions (Telegram/WhatsApp/Discord)
        # cannot use channel-restricted tools (browser_agent, python_exec, …) and have no
        # interactive confirmation path. When True, channel sessions get the SAME tools as the
        # main agent — channel restrictions and per-call confirmations are lifted — gated only by
        # the channel whitelist and the per-user admin check (admin_only tools still need an admin
        # session). Admin-only setting; default ON - channel sessions get the same tools as the main
        # agent, gated by the channel whitelist (paired_only) + the per-user admin check.
        "channel_tools_unrestricted": True,
        "tool_confirmation_bypass_admins": False,
        "spend_budget_usd_per_day": 0,                      # 0 = no cap (default). Per USER per day, estimated from token usage; the estimate is always recorded so you can measure before capping
           # hands-off for admins: skip the confirmation dialog (never the authorization stages); every bypass emits a gate_bypassed event


        # Periodic skill re-scan (post-install tamper detection): every N hours the security
        # scanner re-checks ALL installed skills on disk and updates their manifest scan blocks;
        # a worsened level raises a security event on the Overview dashboard. 0 disables.
        "skills_rescan_interval_hours": 5,

        # Front Office: when True, replies to contacts (from_contact) require explicit approval in Web UI before sending.
        # Default False: contacts you added with "Can reach your assistant" get replies directly; set True to review each reply first.
        "front_office_contact_reply_require_approval": False,

        # Email connections: accounts only (no passwords/tokens in config).
        # Credentials stored in OS keyring or encrypted file (see vaf.core.credential_store).
        "email_config": None,  # { "accounts": [ { "account_id", "provider", "email", "enabled", "imap_host?", "imap_port?", "smtp_host?", "smtp_port?" } ] }
        "email_config_by_scope": None,  # { "<user_scope_id_uuid>": { "accounts": [...] } } — UUID-based per-user config (preferred)
        "email_config_by_user": None,  # { "<username>": { "accounts": [...] } } — legacy per-username config
        "email_credentials_key": "",  # AES key (Base64) for fallback encrypted file; auto-generated if empty
        "secure_store_kek": "",  # Key-encryption-key (Base64) for credential fallback when no master passphrase is set; auto-generated. See vaf.core.secure_store
        # SSRF guard for IMAP/SMTP: when False (default), refuse mail hosts resolving to
        # loopback/RFC-1918/link-local addresses. Admin-only (GLOBAL_CONFIG_KEYS).
        "email_allow_private_hosts": False,
        # Agent-facing phishing filter (prompt-injection defense): suspicious mail is hidden
        # from mail tools while the Web UI still shows it with a warning. Instance-global,
        # admin-only (email_agent_ prefix in GLOBAL_CONFIG_KEY_PREFIXES). See vaf/tools/mail_utils.py.
        "email_agent_phishing_filter_enabled": True,
        "email_agent_phishing_score_threshold": 3,  # 1-10; messages at/above are hidden from the agent
        "email_agent_trusted_sender_domains": None,  # list[str] of From-domains that bypass the filter
        # Mail engine (vaf/mail/, docs/integrations/EMAIL_CLIENT.md). The write
        # flag and retention are instance-wide resource policy: admin-only.
        "mail_engine_write_enabled": False,  # allow server-side writes (flags/move/append) - separate switch by design
        "mail_body_retention_days": 365,  # cached-body retention (headers are kept forever)
        "mail_store_encryption_key": "",  # AES key (Base64) for mail.db body blobs; auto-generated (PROTECTED)
        # Mail Composer (vaf/mail/composer.py): drafts and rewrites text in the mail
        # window's compose box. Draft only - it never sends. The budget keys bound how
        # much attacker-controlled thread text reaches a prompt, so they are policy,
        # not preference: admin-only via GLOBAL_CONFIG_KEYS.
        "mail_composer_enabled": True,
        "mail_composer_max_context_chars": 12000,  # total thread budget (clamped 2000-40000)
        "mail_composer_max_message_chars": 4000,   # per-message cap inside that budget
        "mail_composer_max_messages": 8,           # hard message-count cap
        "mail_composer_max_output_tokens": 800,    # output cap for the single call
        # Let the Composer consult the user's own long-term memory. Retrieval is keyed
        # on the USER's instruction only (never on mail text), so a mail cannot steer
        # what is pulled; the residual risk is disclosure INTO a draft the user then
        # sends, hence a switch. See _composer_knowledge in mail_routes.py.
        "mail_composer_memory_enabled": True,
        # Let the Composer quote older mail from OTHER threads, found by keyword over
        # the local FTS index. Default OFF: it widens what untrusted correspondence can
        # reach a prompt from "the thread you have open" to "anything matching a word
        # you typed", so it stays a deliberate choice. See _composer_related.
        "mail_composer_mailbox_search_enabled": False,
        # OAuth2: callback base URL must point to this backend (default http://127.0.0.1:8001). Set if behind proxy or different port.
        "email_oauth_callback_base_url": "",
        # OAuth2 client IDs (register app in Google Cloud Console / Azure; redirect_uri = {email_oauth_callback_base_url or http://127.0.0.1:PORT}/api/email/oauth/callback)
        "email_oauth_google_client_id": "",
        "email_oauth_google_client_secret": "",
        "email_oauth_microsoft_client_id": "",
        "email_oauth_microsoft_client_secret": "",
    }

    # Per-provider model metadata (single source — see module-level PROVIDER_MODELS).
    PROVIDER_MODELS = PROVIDER_MODELS

    @classmethod
    def get_default_model(cls, provider: str) -> str:
        """Default model id for an API provider (empty for local / unknown)."""
        return cls.PROVIDER_MODELS.get(provider, {}).get("default", "")

    @classmethod
    def get_fallback_models(cls, provider: str) -> list:
        """Static fallback model list for an API provider (used when no live fetch)."""
        return list(cls.PROVIDER_MODELS.get(provider, {}).get("fallback", []))

    # ── Config-file write safety ────────────────────────────────────────────
    # config.json is read and written by several processes at once (server,
    # tray, subagent children). A plain open("w") leaves a window in which a
    # concurrent reader sees a truncated file; load() then degrades to
    # DEFAULTS, and a caller like the memory-crypto key loader sees "no key"
    # on a LIVE installation (live incident: the memory encryption key was
    # silently re-minted and every already-encrypted row orphaned).
    # tmp+fsync+os.replace makes every read see either the old or the new
    # complete file; the file lock closes the read-modify-write lost-update
    # race between processes (precedent: vaf/core/secure_store.py).

    _filelock = None  # lazy singleton; filelock is reentrant per (instance, thread)

    @classmethod
    @contextmanager
    def _locked(cls):
        """Cross-process lock for config read-modify-write spans. set() ->
        save() nests cleanly (reentrant). Degrades to unlocked on timeout or
        when filelock is unavailable - a late write beats no write."""
        lk = None
        try:
            if cls._filelock is None:
                from filelock import FileLock
                cls.APP_DIR.mkdir(parents=True, exist_ok=True)
                cls._filelock = FileLock(str(cls.CONFIG_FILE) + ".lock")
            lk = cls._filelock
            lk.acquire(timeout=10)
        except Exception:
            lk = None
        try:
            yield
        finally:
            if lk is not None:
                try:
                    lk.release()
                except Exception:
                    pass

    @classmethod
    def _write_config_file(cls, data: dict) -> None:
        """Atomic write: mkstemp in the SAME directory as the target (0600
        from birth, and os.replace stays on one filesystem - atomic on POSIX
        and Windows). Readers never see a partial file; secrets never sit
        world-readable before hardening."""
        target_dir = Path(cls.CONFIG_FILE).parent
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=4).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=".config-", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cls.CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls) -> dict:
        if not cls.CONFIG_FILE.exists():
            return cls.DEFAULTS.copy()
        try:
            with open(cls.CONFIG_FILE, "r") as f:
                data = json.load(f)
            result = {**cls.DEFAULTS, **data}
            # Ordered, additive config migrations (vaf/core/migrations.py). The
            # stored version is read from the RAW file (missing -> 1) so an old
            # config is not mistaken for current via the DEFAULTS merge. No-op
            # until a migration is registered.
            try:
                from vaf.core import migrations as _mig
                _stored_ver = int(data.get("config_format_version", 1) or 1)
                if _stored_ver < _mig.CONFIG_FORMAT_VERSION:
                    result, _applied = _mig.run_config_migrations(result, _stored_ver)
                    result["config_format_version"] = _mig.CONFIG_FORMAT_VERSION
                    if _applied:
                        # Persist against the sparse raw file (don't write all defaults).
                        try:
                            _raw, _ = _mig.run_config_migrations(dict(data), _stored_ver)
                            _raw["config_format_version"] = _mig.CONFIG_FORMAT_VERSION
                            cls._write_config_file(_raw)
                        except Exception:
                            pass
            except Exception:
                pass
            # Apply defaults when saved value is missing or empty (so UI/API always get valid URLs)
            for key in ("speech_tts_docker_url", "speech_tts_docker_url_de", "speech_tts_docker_url_en", "speech_tts_docker_url_fr", "speech_stt_docker_url"):
                if key in cls.DEFAULTS and not (result.get(key) or "").strip():
                    result[key] = cls.DEFAULTS[key]
            # Migrate deprecated DeepSeek model names to current equivalents.
            # Old names (deepseek-chat, deepseek-coder, deepseek-reasoner) were valid
            # before 2025 but are now replaced by deepseek-v4-flash / deepseek-v4-pro.
            # deepseek-reasoner also causes 400 errors because it doesn't support tool_choice.
            _DS_MIGRATIONS = {
                "deepseek-chat":     "deepseek-v4-flash",
                "deepseek-coder":    "deepseek-v4-flash",
                "deepseek-reasoner": "deepseek-v4-flash",
                "deepseek-r1":       "deepseek-v4-flash",
            }
            _ds_saved = result.get("api_model_deepseek", "")
            if _ds_saved in _DS_MIGRATIONS:
                result["api_model_deepseek"] = _DS_MIGRATIONS[_ds_saved]
                # Persist the fix so the stale value never comes back
                try:
                    import json as _json
                    with open(cls.CONFIG_FILE, "r") as _f:
                        _raw = _json.load(_f)
                    if _raw.get("api_model_deepseek") == _ds_saved:
                        _raw["api_model_deepseek"] = _DS_MIGRATIONS[_ds_saved]
                        cls._write_config_file(_raw)
                except Exception:
                    pass
            # Hard lock for hosting mode (server appliance deployments):
            # when enabled, Local Network Hosting cannot be disabled via UI/API saves.
            if bool(result.get("local_network_force_enabled", False)):
                result["local_network_enabled"] = True
            # Security invariant: local network hosting must always run with TLS enabled.
            if bool(result.get("local_network_enabled", False)):
                result["local_network_tls_enabled"] = True
            # Enforce the minimum context window. VAF needs >= 32768 (system prompt ~5.5k +
            # tool schemas ~6k + conversation headroom); sub-32k values are raised here so every
            # reader sees one consistent, supported floor instead of an ad-hoc per-call clamp.
            try:
                result["n_ctx"] = max(int(result.get("n_ctx") or 32768), 32768)
            except (TypeError, ValueError):
                result["n_ctx"] = 32768
            return result
        except Exception:
            return cls.DEFAULTS.copy()

    # Keys that should never be overwritten when saving from frontend
    # These are auto-generated secrets that would break auth if lost
    PROTECTED_KEYS = [
        "local_network_jwt_secret",
        "email_credentials_key",
        "cloud_credentials_key",
        "secure_store_kek",
        "mail_store_encryption_key",
        # Losing this one orphans every encrypted memory row (live incident:
        # a save without it plus the silent first-run mint rotated the key).
        "memory_encryption_key",
    ]

    # Keys (and prefixes) that only admins can change. Non-admins can change user-scoped
    # settings (e.g. language, interface) but not backend/network/API config.
    GLOBAL_CONFIG_KEY_PREFIXES = (
        "local_network_",
        "api_key_",
        "api_model_",
        "email_oauth_",
        # Phishing-filter policy is instance-global prompt-injection defense: a
        # non-admin LAN user must not be able to disable it or trust a domain
        # for everyone's agent.
        "email_agent_",
        "cloud_oauth_",
        "github_oauth_",
        "speech_stt_",
        "speech_tts_",
        "speaker_id_",
        # Voice-agent LLM lane: redirecting the call's inference (or burning the
        # admin's API quota) must never be possible for a non-admin LAN user.
        "voice_agent_",
        "subagent_",
        "thinking_",
        "librarian_",
        "document_conversion_",
        "failover_",
    )
    GLOBAL_CONFIG_KEYS = frozenset([
        "provider", "model", "n_ctx", "gpu_layers", "n_parallel", "llama_cache_ram",
        "auto_start_local_server", "tray_autostart", "web_ui_enabled", "server_persistence_enabled",
        "debug_logs_enabled", "server_idle_timeout", "telegram_idle_timeout", "telegram_debounce_seconds",
        "redis_url", "redis_enabled", "use_docker",
        "local_admin_scope_id", "local_admin_username",
        "channel_ingress_policy", "channel_tools_unrestricted",
        # Skipping the confirmation dialog is an instance-wide safety decision:
        # a non-admin LAN user must never be able to turn it on for themselves.
        "tool_confirmation_bypass_admins",
        # A spend cap is instance money: a LAN user must not be able to raise
        # their own (precedent: the learn_ spend keys below).
        "spend_budget_usd_per_day",
        # Concurrency + rate-limit resilience: system-wide, admin-only (a LAN user must not change them).
        "parallel_main_workers", "queue_policy", "max_parallel_api_workers", "max_parallel_local_workers",
        "api_retry_attempts", "api_retry_after_max",
        # Local vision projector: a llama-server LAUNCH argument (code execution
        # surface) - never user-writable.
        "vision_local_mmproj",
        # Legacy STT toggle: enables the shared backend STT service for everyone
        # (soon a metered cloud lane), so it must not be user-writable. The
        # canonical speech_stt_enabled is already covered by the prefix list.
        "stt_enabled",
        # SSRF guard for mail hosts: flipping it opens IMAP/SMTP connects to
        # loopback/RFC-1918/metadata addresses for the whole instance - a LAN
        # user must never be able to do that.
        "email_allow_private_hosts",
        # Mail engine write flag + retention: instance-wide policy.
        "mail_engine_write_enabled",
        "mail_body_retention_days",
        # Mail Composer: the budget keys decide how much untrusted mail text reaches
        # a model prompt, and the enable flag decides whether that happens at all -
        # a per-user write would let a LAN user raise both for the instance.
        "mail_composer_enabled",
        "mail_composer_max_context_chars",
        "mail_composer_max_message_chars",
        "mail_composer_max_messages",
        "mail_composer_max_output_tokens",
        "mail_composer_memory_enabled",
        "mail_composer_mailbox_search_enabled",
        # Document-learning spend keys: pages/sections/batch size decide how many
        # LLM calls one learn_document run makes, the token key how large each
        # one is - a per-user write would let a LAN user multiply instance spend
        # (precedent: the mail_composer_max_* budget keys above). learn_ is NOT
        # a global prefix, so these need explicit entries.
        "learn_document_max_pages",
        "learn_max_sections",
        "learn_batch_pages",
        "memory_document_extraction_max_tokens",
        # OCR spend keys: the vision engine is one model call PER PAGE, so the
        # engine choice and its per-call budget are instance spend.
        "ocr_engine",
        "ocr_vision_max_pages_per_call",
        # At-rest protection and the doors in front of it. Every one of these is
        # instance-wide security policy, and a per-user write is a way to switch
        # the protection off from the LAN: `file_encryption_enabled` decides
        # whether new chats are ciphertext at all, `allow_plaintext_at_rest`
        # whether a swapped-in plaintext record is still accepted on read,
        # `cli_password_gate` whether the terminal asks for the password,
        # `prompt_log_full_enabled` whether the entire assembled prompt (with
        # decrypted memories) is written to disk, and `secure_store_kek_backend`
        # where the next master key is placed. `context_archive_max_age_days` is
        # the retention of conversation snapshots - raising it keeps everyone's
        # history on disk longer. None of them is a personal preference.
        "file_encryption_enabled",
        "allow_plaintext_at_rest",
        "cli_password_gate",
        "prompt_log_full_enabled",
        "secure_store_kek_backend",
        "context_archive_max_age_days",
    ])

    @classmethod
    def is_global_config_key(cls, key: str) -> bool:
        """True if this config key may only be written by an admin (backend/network/API)."""
        if key in cls.GLOBAL_CONFIG_KEYS:
            return True
        return any(key.startswith(prefix) for prefix in cls.GLOBAL_CONFIG_KEY_PREFIXES)

    # Secret config values that must NEVER be returned to a non-admin client.
    # This is a READ-redaction list and is intentionally NARROWER than the
    # global write-denylist above: keys like api_model_* or non-secret
    # local_network_* are admin-only to *write* but safe for any user to *read*
    # (the UI needs them), whereas the entries below are credentials/keys
    # (API keys, OAuth client secrets, the JWT secret, encryption keys, DB URLs
    # that may embed passwords).
    SECRET_CONFIG_KEY_SUFFIXES = (
        "_client_secret",
        "_secret",
        "_credentials_key",
        "_encryption_key",
        "_kek",
        "_password",
        "_passwd",
    )
    SECRET_CONFIG_KEY_PREFIXES = ("api_key_",)
    SECRET_CONFIG_KEYS = frozenset({
        "secure_store_kek",
        "memory_db_url",
        "redis_url",
    })

    @classmethod
    def is_secret_config_key(cls, key: str) -> bool:
        """True if this config value is a credential/secret that must never be sent
        to a non-admin client. Narrower than is_global_config_key (which also covers
        non-secret admin-only settings the UI legitimately reads)."""
        if key in cls.SECRET_CONFIG_KEYS:
            return True
        if any(key.startswith(p) for p in cls.SECRET_CONFIG_KEY_PREFIXES):
            return True
        return any(key.endswith(s) for s in cls.SECRET_CONFIG_KEY_SUFFIXES)

    # Connection config keys that only admin may write (enabled/whitelist etc.). Non-admins write to connection_enabled_by_scope instead.
    CONNECTION_CONFIG_KEYS = frozenset({"telegram_config", "whatsapp_config", "discord_config"})

    # Secrets a person MANAGES through Settings - the OAuth client secrets - as opposed to
    # infrastructure secrets (the KEK, the JWT signing secret, DB URLs) that no UI has any
    # business displaying, hinting at, or deleting. This is the allowlist behind the
    # stored-state listing and the explicit-delete endpoint: `is_secret_config_key` decides
    # what never TRAVELS, this decides the far smaller set a UI may ask ABOUT. A hint of the
    # KEK would be a leak with no user need behind it; deleting the JWT secret from a
    # settings page would be an outage button. CI-guarded: every entry here must also be
    # classified secret, so the two sets cannot drift into contradiction.
    UI_MANAGED_SECRET_KEYS = frozenset({
        "email_oauth_google_client_secret",
        "cloud_oauth_google_client_secret",
        "email_oauth_microsoft_client_secret",
        "cloud_oauth_microsoft_client_secret",
        "cloud_oauth_dropbox_client_secret",
        "github_oauth_client_secret",
    })

    @classmethod
    def filter_for_non_admin(cls, config: dict) -> dict:
        """Return a copy of config with only keys non-admins are allowed to write (user-scoped settings)."""
        return {k: v for k, v in config.items() if not cls.is_global_config_key(k)}

    @classmethod
    def extract_connection_toggles_for_scope(
        cls, body: dict, user_scope_id: Optional[str]
    ) -> tuple[dict, dict]:
        """
        For non-admin save: extract telegram/whatsapp/discord enabled from body into connection_enabled_by_scope entry,
        and return (body_without_connection_configs, { scope_id: { telegram, whatsapp, discord } }).
        Caller merges the returned dict into connection_enabled_by_scope and merges body_filtered into config (so global connection configs are not overwritten).
        """
        if not user_scope_id:
            return body, {}
        scope_str = str(user_scope_id).strip()
        toggles = {}
        body_filtered = dict(body)
        for key in cls.CONNECTION_CONFIG_KEYS:
            if key not in body_filtered:
                continue
            val = body_filtered[key]
            if isinstance(val, dict) and "enabled" in val:
                if key == "telegram_config":
                    toggles["telegram"] = bool(val["enabled"])
                elif key == "whatsapp_config":
                    toggles["whatsapp"] = bool(val["enabled"])
                elif key == "discord_config":
                    toggles["discord"] = bool(val["enabled"])
            body_filtered.pop(key, None)
        if not toggles:
            return body, {}
        return body_filtered, {scope_str: toggles}

    @classmethod
    def config_for_user(cls, config: dict, user_scope_id: Optional[str], role: str) -> dict:
        """
        Return a copy of config safe to send to a given user. Admins get the full config.
        Non-admins get connection data scoped to their user_scope_id only (no other users' mail, telegram, whatsapp, etc.).
        """
        if (role or "").lower() == "admin":
            # Full config MINUS every secret VALUE - for admins too. Secrets are WRITE-ONLY
            # through the config API: state travels as booleans and lossy hints via
            # `GET /api/config/api-keys`, never as values. This started as an `api_key_`
            # carve-out and was widened to the `is_secret_config_key` classifier the same
            # day, because the narrow cut was an enumeration bought one incident at a time:
            # the api_key echo had already poisoned a stored key (the browser echoed the
            # estate value into a save, and it was stored AS the key), and the OAuth client
            # secrets were still travelling by the identical mechanism, one save away from
            # the identical damage. One classifier, one rule, instead of a prefix list that
            # grows a member per incident. Measured before widening: no web surface reads
            # any secret value - the client-id fields are not secrets, and the secret
            # fields are exactly the ones being converted to stored-state display. Blanked
            # rather than popped, so client code reading `config.<key>` keeps getting a
            # defined empty string.
            out = dict(config)
            for k in [k for k in out if cls.is_secret_config_key(k)]:
                out[k] = ""
            return out
        out = dict(config)
        scope_str = str(user_scope_id).strip() if user_scope_id else None

        # Strip credentials/secrets: non-admins must never receive API keys, OAuth
        # client secrets, the JWT secret, encryption keys or DB URLs. (The wizard's
        # OAuth-config read is admin-only; non-admins read connection *state* from
        # dedicated status endpoints, not raw secrets.)
        for k in [k for k in out if cls.is_secret_config_key(k)]:
            out.pop(k, None)

        # Email: only this user's accounts (email_config_by_scope[user_scope_id])
        by_scope = config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict) and scope_str:
            out["email_config_by_scope"] = {scope_str: by_scope.get(scope_str, {"accounts": []})}
        else:
            out["email_config_by_scope"] = {}

        # Legacy email_config / email_config_by_user: non-admin should not see other users; expose only empty or own
        out["email_config"] = None
        out["email_config_by_user"] = {}

        # Per-user connection toggles (new users = all off)
        by_scope = config.get("connection_enabled_by_scope") or {}
        if not isinstance(by_scope, dict):
            by_scope = {}
        user_toggles = by_scope.get(scope_str or "", {}) if scope_str else {}
        if not isinstance(user_toggles, dict):
            user_toggles = {}

        # Telegram: do not expose full whitelist to non-admin; enabled = per-user toggle (default False for new user)
        tc = config.get("telegram_config") or {}
        if isinstance(tc, dict):
            out["telegram_config"] = {
                "enabled": user_toggles.get("telegram", False),
                "verified": tc.get("verified", False),
                "bot_username": tc.get("bot_username"),
                "whitelist": [],
            }
        else:
            out["telegram_config"] = None

        # WhatsApp: only whitelist entries for this user; enabled = per-user toggle (default False for new user)
        wc = config.get("whatsapp_config") or {}
        if isinstance(wc, dict):
            whitelist = wc.get("whitelist") or []
            if scope_str:
                my_entries = [e for e in whitelist if isinstance(e, dict) and str(e.get("user_scope_id")) == scope_str]
            else:
                my_entries = []
            out["whatsapp_config"] = {**wc, "whitelist": my_entries, "enabled": user_toggles.get("whatsapp", False)}
        else:
            out["whatsapp_config"] = None

        # Discord: single-tenant; enabled = per-user toggle (default False for new user)
        dc = config.get("discord_config") or {}
        if isinstance(dc, dict):
            out["discord_config"] = {
                "enabled": user_toggles.get("discord", False),
                "verified": dc.get("verified", False),
                "configured": bool(dc.get("verified") and dc.get("admin_user_id")),
                "chat_activity": [],
            }
        else:
            out["discord_config"] = None

        return out

    @classmethod
    def merge_preserving_nonempty_sensitive(cls, existing: dict, incoming: dict) -> dict:
        """
        Merge config updates while preventing accidental destructive overwrites.

        Safety rules:
        - Keep existing API keys if incoming value is empty/blank.
        - Keep existing connection configs if incoming value is None.
        """
        merged = dict(existing or {})
        if not isinstance(incoming, dict):
            return merged

        for key, value in incoming.items():
            # EVERY secret, not only api_key_*: since the admin view blanks secret values,
            # every save echoes "" for every secret the form did not retype - and with the
            # old api_key_-only guard that echo would have WIPED the OAuth client secrets
            # from config.json on the first unrelated settings save. Blank means "not
            # re-sent" for all of them; removal is an explicit endpoint, never an empty
            # field. (api_key_ is a secret prefix, so this deletes the special case rather
            # than adding a second one beside it.)
            if cls.is_secret_config_key(key):
                if isinstance(value, str) and not value.strip():
                    if (existing or {}).get(key):
                        continue
                if value is None and (existing or {}).get(key):
                    continue

            if key in cls.CONNECTION_CONFIG_KEYS:
                if value is None and isinstance((existing or {}).get(key), dict):
                    continue

            merged[key] = value

        return merged

    @classmethod
    def apply_veyllo_stt_default(cls, existing: dict, merged: dict) -> dict:
        """Default `speech_stt_provider` to `veyllo` the FIRST time a Veyllo API key
        is added (absent -> present) while no STT provider was chosen yet. Owner
        product decision: when a Veyllo key exists and the user never picked a
        specific STT provider, hosted Veyllo STT is the default; the always-local
        fallback still covers offline / empty credits, and an explicit later choice
        (local, OpenAI, ElevenLabs) overwrites it. Covers the onboarding key entry,
        a key added later in Settings, AND the CLI provider menu, because `save()`
        calls this centrally (one place, every write path - so a new key-write path
        can never silently miss it or consume the transition without seeding).

        Fires only on the absent->present transition (never on key rotation or a
        re-sent key). "No STT provider chosen" means an empty `speech_stt_provider`
        AND a default/empty `speech_stt_engine`: an explicit local pick also stores
        an empty provider, but `local_whisper` sets `speech_stt_engine='local'`
        (non-default), which is an unambiguous explicit-local signal that MUST block
        the seed so a deliberate local opt-out is never flipped to the metered cloud.
        (`local_docker`/unset both leave the default engine `docker`, genuinely
        indistinguishable, so that pristine-default case is seeded.) `api_key_` is
        admin-only, so this only runs for admin saves - consistent with the
        admin-gated `speech_stt_` keys it writes. Best-effort: never blocks a save."""
        try:
            had_key = bool((existing or {}).get("api_key_veyllo"))
            has_key = bool((merged or {}).get("api_key_veyllo"))
            provider_chosen = str((merged or {}).get("speech_stt_provider") or "").strip()
            engine = str((merged or {}).get("speech_stt_engine") or "").strip().lower()
            explicit_local = engine not in ("", "docker")  # e.g. 'local' = local_whisper picked
            if has_key and not had_key and not provider_chosen and not explicit_local:
                merged["speech_stt_provider"] = "veyllo"
        except Exception:
            pass
        return merged

    @classmethod
    def get(cls, key: str, default=None):
        return cls.load().get(key, default if default is not None else cls.DEFAULTS.get(key))

    @classmethod
    def set(cls, key: str, value):
        # Locked read-modify-write: without it, two processes saving at once
        # lose one of the two updates (server + tray + subagent children all
        # write this file).
        with cls._locked():
            config = cls.load()
            config[key] = value
            cls.save(config)
    
    @classmethod
    def set_api_key(cls, provider: str, api_key: str):
        """Store an API key in the ENCRYPTED store. A thin adapter, kept logic-free.

        It used to base64-encode into `config.json` under a docstring claiming to "securely
        store" it - a label the mechanism did not carry, in a module that says "NOT
        encryption!" twenty lines higher. Both the encoding and the claim are gone; the one
        decision about where a key lives is in `vaf/core/api_keys.py`.
        """
        if not api_key:
            return
        from vaf.core.api_keys import store_api_key
        store_api_key(provider, api_key)

    @classmethod
    def get_api_key(cls, provider: str) -> str:
        """Retrieve an API key. A thin adapter over the shared resolver.

        IT MUST STAY LOGIC-FREE. Thirteen call sites reach a provider key through this name,
        and the reason they used to disagree with an embedder's key is that this method
        answered the question by itself, from the file, as a classmethod no caller config
        could influence. If a lane ever needs an answer the resolver cannot give, that shape
        belongs in the resolver - or the design behind it is wrong.

        Raises `ApiKeyUnavailable` when a key IS stored and cannot be read; "" still means
        "nothing configured". Those two used to be the same value, which is why a corrupt
        store looked exactly like an unconfigured one.
        """
        from vaf.core.api_keys import resolve_api_key
        return resolve_api_key(provider)
    
    @classmethod
    def mask_api_key(cls, api_key: str) -> str:
        """
        Best Practice: Mask API key for display (show first 8 chars + ...)
        
        Args:
            api_key: Full API key
            
        Returns:
            Masked key string
        """
        if not api_key:
            return "(not set)"
        
        if len(api_key) <= 8:
            return "***"
        
        return f"{api_key[:8]}...{api_key[-4:]}"    
    @classmethod
    def is_docker_mode(cls) -> bool:
        """
        Check if running inside Docker container.
        
        Returns:
            True if running in Docker, False otherwise
        """
        return os.environ.get("VAF_DOCKER_MODE", "").lower() == "true"
    
    @classmethod
    def get_llama_server_url(cls, endpoint: str = "") -> str:
        """
        Get the correct llama-server URL based on environment.
        
        In Docker mode, llama-server runs on the HOST (for GPU access),
        so we need to use 'host.docker.internal' to reach it from the container.
        
        In native mode, llama-server runs on localhost.
        
        Args:
            endpoint: Optional API endpoint (e.g., "/v1/chat/completions", "/health")
            
        Returns:
            Full URL to llama-server
        """
        # Check environment variables first (highest priority)
        llama_url = os.environ.get("LLAMA_SERVER_URL")
        if llama_url:
            return f"{llama_url.rstrip('/')}{endpoint}"
        
        # Build URL from host/port env vars
        host = os.environ.get("LLAMA_SERVER_HOST")
        port = os.environ.get("LLAMA_SERVER_PORT", "8080")
        
        if host:
            return f"http://{host}:{port}{endpoint}"
        
        # Fallback based on Docker mode
        if cls.is_docker_mode():
            # In Docker, use host.docker.internal to reach host machine
            return f"http://host.docker.internal:8080{endpoint}"
        else:
            # Native mode, llama-server runs locally
            return f"http://127.0.0.1:8080{endpoint}"
    
    @classmethod
    def get_llama_server_host(cls) -> str:
        """Get just the host portion of llama-server address."""
        if os.environ.get("LLAMA_SERVER_HOST"):
            return os.environ.get("LLAMA_SERVER_HOST")
        return "host.docker.internal" if cls.is_docker_mode() else "127.0.0.1"
    
    @classmethod
    def get_llama_server_port(cls) -> int:
        """Get the llama-server port."""
        return int(os.environ.get("LLAMA_SERVER_PORT", "8080"))

    # Observer Pattern Implementation
    _observers = []
    _observers_lock = threading.Lock()

    @classmethod
    def add_observer(cls, callback):
        """
        Add a callback function to be notified of configuration changes.
        Callback signature: callback(key: str, new_value: Any)
        """
        with cls._observers_lock:
            if callback not in cls._observers:
                cls._observers.append(callback)

    @classmethod
    def notify_observers(cls, key: str, value, old_value=None):
        """Notify all observers of a change. Optional old_value for provider etc."""
        if cls._observers_lock is None:
            # Should already be init by add_observer or safe execution
            return

        # Copy observers to avoid issues if callback modifies list
        with cls._observers_lock:
            observers_copy = list(cls._observers)
        
        for callback in observers_copy:
            try:
                callback(key, value, old_value)
            except Exception as e:
                print(f"[Config] Observer callback failed: {e}")

    @classmethod
    def save(cls, config: dict):
        if not cls.APP_DIR.exists():
            cls.APP_DIR.mkdir(parents=True, exist_ok=True)

        # Locked read-modify-write + atomic replace: see the block comment at
        # _locked/_write_config_file for the incident this prevents.
        with cls._locked():
            # Load existing to detect changes
            existing_config = cls.load()

            # Preserve protected keys from existing config
            for key in cls.PROTECTED_KEYS:
                if key in existing_config and key not in config:
                    config[key] = existing_config[key]

            # Central seed point for the Veyllo-key -> default-STT rule (see the method
            # docstring). Runs here so EVERY write path is covered - web REST/WS, the CLI
            # provider menu, and any future key-writer - and no path can consume the
            # absent->present transition without seeding.
            cls.apply_veyllo_stt_default(existing_config, config)

            # Hosting lock: keep Local Network Hosting enabled if lock is active.
            force_network = bool(
                config.get("local_network_force_enabled", existing_config.get("local_network_force_enabled", False))
            )
            if force_network:
                config["local_network_enabled"] = True
            # Security invariant: it must not be possible to persist network mode with TLS disabled.
            if bool(config.get("local_network_enabled", False)):
                config["local_network_tls_enabled"] = True

            cls._write_config_file(config)

        # config.json holds secrets (KEK, JWT secret, base64 API keys): owner-only.
        # Lazy import avoids a circular dependency (secure_store imports Config).
        try:
            from vaf.core.secure_store import harden_dir, harden_path
            harden_dir(cls.APP_DIR)
            harden_path(cls.CONFIG_FILE)
        except Exception:
            pass

        # Detect and notify changes for critical keys
        # local_network_* for server restart; provider for tray VRAM load/unload; model for llama-server reload
        critical_keys = [
            "local_network_enabled",
            "local_network_tls_enabled",
            "local_network_https_port",
            "local_network_port",
            "local_network_port_frontend",
            "provider",
            "n_ctx",
            "gpu_layers",
            "model",
            # API keys: a key change (e.g. onboarding) must also re-apply the live provider.
            "api_key_veyllo",
            "api_key_openai",
            "api_key_anthropic",
            "api_key_deepseek",
            "api_key_google",
            "api_key_openrouter",
        ]
        
        for key in critical_keys:
            old_val = existing_config.get(key)
            new_val = config.get(key)
            if old_val != new_val:
                cls.notify_observers(key, new_val, old_val)


def get_local_admin_scope_id() -> str:
    """Return the local admin user_scope_id (UUID). Use this instead of Config.get('local_admin_scope_id', ...)."""
    return str(Config.get("local_admin_scope_id", LEGACY_LOCAL_ADMIN_SCOPE_ID) or LEGACY_LOCAL_ADMIN_SCOPE_ID).strip()


def is_admin_identity(role: Optional[str], user_scope_id: Optional[str]) -> bool:
    """Return True if this identity has admin rights.

    Two halves, and neither is redundant:
    - ``role == "admin"`` covers every admin account. VAF supports more than one admin
      (user management refuses to delete the LAST one), and a second admin carries their
      OWN scope UUID - a scope-only check would treat them as an ordinary user.
    - the local-admin scope covers the machine owner when there is no DB role at all:
      the tokenless desktop, the CLI and automations resolve to ``local_admin_scope_id``
      with no role claim, so a role-only check would lock the owner out.

    The role is only ever read from a signature-verified JWT claim (issued from
    ``LocalUser.role`` at login), never from client- or model-supplied input. Callers that
    receive tool arguments MUST overwrite the role from their trusted context rather than
    honoring whatever a model passed in.
    """
    if str(role or "").strip().lower() == "admin":
        return True
    if user_scope_id is None:
        return False
    return str(user_scope_id).strip() == str(get_local_admin_scope_id())


def get_local_admin_username() -> str:
    """Return the local admin username. Use for display and paths when no JWT."""
    return (Config.get("local_admin_username") or "admin").strip()


def resolve_caller_username(
    username: Optional[str],
    user_scope_id: Optional[str] = None,
    *,
    allow_lookup: bool = False,
) -> str:
    """Who is calling, expressed as a NAME, for the stores that key on one.

    A NAMELESS CALLER IS NOT AUTOMATICALLY THE OWNER, and that is the whole reason this exists.
    Seven owner-branches across the stores read `if not username or username == local_admin` and
    answer with the machine owner's data, so a missing name and the owner's name are the same
    key - "no name" cannot be expressed by passing None. The question a caller with no name has
    to answer is therefore not "what is my name" but "am I the owner", and only the SCOPE can
    say. No scope, or the owner's scope, means single-user or the owner, and the owner's name is
    right. A DIFFERENT scope means a tenant whose name simply is not in the session metadata,
    and handing them the owner's name hands them the owner's data.

    Measured on a live installation before this was written: of 3238 stored sessions 24 carry a
    username, and of the rest 3208 carry a NON-OWNER scope while 0 carry the owner's. So the
    nameless case is overwhelmingly a tenant, not the owner - a number that was counted right
    and read wrong once already.

    Three stores make the ownership decision on the NAME ALONE, which is why the name matters
    this much: `github_tools._get_github_account_for_user` (which accepts `user_scope_id` and
    never references it), `cloud_routes._get_cloud_config` and
    `cloud_storage._get_cloud_accounts`. For those the owner's name IS the owner's data.

    The synthetic `scope_<hex>` for an unknown tenant is not invented here: `automation` and
    `thinking_mode` both resolved it that way, correctly, and wrote down why. What they could
    not do was make the DISPATCHER agree, because it had its own naive answer - which is how a
    rule that existed twice still failed at the one place every tool passes through.

    `allow_lookup` costs a database round trip (and sometimes a thread) with no cache, so it is
    off by default and must stay off on the per-dispatch path. Without it a tenant addresses a
    stable, isolated bucket of their own rather than their real account name - safe, and still
    better than the shared literal, which pooled every nameless tenant into ONE bucket.
    """
    given = str(username or "").strip()
    if given:
        return given
    try:
        scope = str(user_scope_id or "").strip()
        if not scope or scope == str(get_local_admin_scope_id() or "").strip():
            return get_local_admin_username()
        if allow_lookup:
            try:
                from vaf.core.thinking_mode import _resolve_username_for_scope
                resolved = _resolve_username_for_scope(scope)
                if resolved and str(resolved).strip():
                    return str(resolved).strip()
            except Exception:
                pass
        return "scope_" + scope.replace("-", "")[:8]
    except Exception:
        # Cannot tell whose scope this is -> must not answer with the owner's name.
        return get_local_admin_username() if not user_scope_id else "scope_unknown"


def subagent_provider_override() -> Optional[str]:
    """The provider a sub-agent must run on, or None when it inherits the main one.

    TWO KEYS, ONE DECISION, and that is the whole reason this exists.
    `subagent_provider` names the choice, `subagent_use_separate_provider` gates
    it, and the gate defaults to False - so a name written WITHOUT the gate is
    silently inert. Nothing raises, nothing warns, the sub-agent simply keeps
    the main provider.

    Measured before this was written: SIX places derived the pair by hand, in
    two byte-identical blocks. Four spawn sites - `tools/coder.py`,
    `tools/librarian.py`, `tools/research_agent.py`, `tools/document_agent.py` -
    turned it into a `VAF_PROVIDER` environment entry; two more,
    `core/headless_runner.py` and `core/platform.py`, resolved it against the
    main provider. A seventh caller, the terminal app's settings row, wrote only
    the NAME, reported success, and changed nothing for any of the six.

    Returns the override or None; never the string "inherit", which is a stored
    sentinel and not a provider anyone can run on.
    """
    try:
        if not Config.get("subagent_use_separate_provider", False):
            return None
        name = str(Config.get("subagent_provider", "inherit") or "inherit").strip()
        return name if name and name != "inherit" else None
    except Exception:
        # A sub-agent that cannot read the config inherits; it must not fail to spawn.
        return None


def set_subagent_provider(name: Optional[str]) -> None:
    """Record the sub-agent provider AND its gate, so the pair cannot drift.

    "inherit", an empty value or None all mean the same thing: clear the gate
    and let sub-agents follow the main agent. Written through `Config.set` so
    the change observers fire exactly as they did when callers set both keys by
    hand.
    """
    chosen = str(name or "inherit").strip() or "inherit"
    Config.set("subagent_provider", chosen)
    Config.set("subagent_use_separate_provider", chosen != "inherit")
