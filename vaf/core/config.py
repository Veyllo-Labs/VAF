import os
import json
import threading
from pathlib import Path
from typing import Optional
import base64

# Single source of truth for legacy local-admin scope (before bootstrap sets real admin UUID)
LEGACY_LOCAL_ADMIN_SCOPE_ID = "00000000-0000-0000-0000-000000000001"


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
        "model": "Veyllo/VQ-1_Instruct-q4_k_m",
        "provider": "local",
        "gpu_layers": -1,
        "n_ctx": 8192,
        "n_parallel": 0, # 0 = Auto-detect based on VRAM (1 or 2); Set to 1 to force sequential if crashing
        "llama_cache_ram": 4096,  # Prompt cache size in MB. 0 = disabled. -1 = auto (40% free RAM, cap 8192).
        "temperature": 0.7,

        # AI Provider Settings
        # Options: "local", "openai", "anthropic", "deepseek", "google", "openrouter"
        "provider": "local",
        
        # API Keys (Base64 encoded for basic obfuscation - NOT encryption!)
        # For production, consider using system keyring for API keys and tokens.
        "api_key_veyllo": "",  # Veyllo API server coming later
        "api_key_openai": "",
        "api_key_anthropic": "",
        "api_key_deepseek": "",
        "api_key_google": "",
        "api_key_openrouter": "",
        # Web Search API Keys (optional; when set, used before scrape/DDG)
        "api_key_brave_search": "",
        "api_key_google_search": "",
        "google_search_engine_id": "",
        
        # API Model Selection per Provider
        "api_model_openai": "gpt-4o",
        "api_model_anthropic": "claude-3-5-sonnet-20241022",
        "api_model_deepseek": "deepseek-v4-flash",  # deepseek-chat deprecated 2026-07-24
        "api_model_google": "gemini-1.5-flash",  # Free tier, fast & capable
        "api_model_openrouter": "anthropic/claude-3.5-sonnet",
        
        # Vision Model Fallback — used when the primary provider does not support image input.
        # Example: primary = deepseek (no vision) → vision_provider = google / openai / anthropic.
        # Leave empty to keep current behavior (strip images + show error to user).
        "vision_provider": "",   # e.g. "google", "openai", "anthropic", "openrouter"
        "vision_model": "",      # e.g. "gemini-2.0-flash", "gpt-4o" — leave empty for provider default

        # Sub-Agent Provider Configuration
        "subagent_provider": "inherit",  # Options: "inherit", or any provider name
        "subagent_use_separate_provider": False,
        "subagent_model": "",  # Hybrid mode: model for tools/workflows (empty = same as main chat)
        
        # Auto-start local llama-server (disable if only using APIs)
        "auto_start_local_server": True,

        # Tool Router cap — max number of tools passed to the agent per turn.
        # list_tools and search_tools are always included on top of this limit.
        # Lower = faster LLM inference + less context pollution. Range: 1–100.
        "router_max_tools": 12,

        # UX toggles (opt-in; off by default – user must enable)
        # Auto open web search source links in the user's default browser (tabs)
        "ux_auto_open_links": False,
        # Auto open created output folders/files (file explorer / browser for html)
        "ux_auto_open_outputs": True,
        # Safety cap for tabs opened automatically
        "ux_auto_open_max_tabs": 8,
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

                # Voice / STT Settings
                "stt_enabled": False,                  # Enable Speech-to-Text
                "speech_stt_engine": "docker",         # STT engine: "docker" (default) or "local" (faster-whisper)
                "speech_stt_docker_url": "http://localhost:5003",  # When engine=docker; STT container port 5003 (maps to 9000)

                # STT (Whisper) - only when engine=local; keep "base" to avoid 20GB+ spikes
                "speech_stt_whisper_model": "base",    # faster-whisper: tiny, base, small, medium, large-v3

                # TTS Settings (Web UI uses Docker TTS by default; piper=local, system=pyttsx3, docker=HTTP in Docker)
                "speech_tts_enabled": False,           # Enable Text-to-Speech
                "speech_tts_engine": "docker",         # TTS engine: "docker" (default), "piper", or "system"
                "speech_tts_docker_url": "http://localhost:5002",  # Default/fallback TTS URL
                "speech_tts_docker_url_de": "http://localhost:5002",   # German voice (optional)
                "speech_tts_docker_url_en": "http://localhost:5004",   # English voice (optional)
                "speech_tts_docker_url_fr": "http://localhost:5006",   # French voice (optional)
                "tts_auto_speak": False,               # Auto-speak agent responses in browser
                
                # Librarian Agent settings
                "librarian_max_pdf_size_mb": 50,       # Max PDF size in MB (default: 50)
        "librarian_max_doc_size_mb": 20,       # Max Word/PowerPoint size in MB (default: 20)
        "librarian_max_excel_size_mb": 30,     # Max Excel size in MB (default: 30)
        "librarian_max_text_size_kb": 500,     # Max text file size in KB (default: 500)
        "document_conversion_docker_url": "http://localhost:5005",  # Gotenberg: DOCX/XLSX/PPTX → PDF (LibreOffice in Docker)
        "librarian_auto_chunk_large_files": True,  # Auto-chunk large files (default: True)
        "librarian_pdf_max_pages_preview": 50, # Max pages to show in preview (default: 50)
        
        # System Settings
        "server_mode": False,                  # True = server installation (LAN always on, no desktop UI controls)
        "web_ui_enabled": True,                # Start Web UI automatically
        "server_persistence_enabled": False,   # Keep server running after exit
        "tray_autostart": False,               # Auto-start tray on OS login
        "debug_logs_enabled": False,           # Write domain logs and queue.log when enabled; off by default to reduce I/O
        "parallel_main_workers": 1,            # Main headless workers (1=legacy serialized, 2=weighted-fair parallel)
        "queue_policy": "legacy",              # legacy | weighted_fair
        "queue_weight_interactive": 5,         # Used when queue_policy=weighted_fair
        "queue_weight_automation": 3,          # Used when queue_policy=weighted_fair
        "queue_weight_background": 1,          # Used when queue_policy=weighted_fair
        "server_idle_timeout": 15,             # Unload local model after idle seconds (Web UI / CLI)
        "telegram_idle_timeout": 120,          # Keep model loaded this long after last Telegram prompt when no Web connections (seconds)
        "telegram_debounce_seconds": 5,        # Wait this long for follow-up messages; combine into one prompt per chat

        # Thinking mode: background reflection when user idle
        "thinking_enabled": True,                              # Enable thinking mode when idle
        "thinking_idle_minutes": 10,                           # Start after this many minutes without activity
        "thinking_check_interval_seconds": 60,                 # How often to check for idle users
        "thinking_automation_buffer_minutes": 10,              # Do not start if automation runs within this many minutes
        "thinking_max_duration_minutes": 30,                  # Max duration per thinking run (then release lock)
        "thinking_wait_nudge_minutes": 3,                     # If user does not reply to a question: send nudge after this many minutes
        "thinking_wait_skip_minutes": 10,                     # If still no reply after this many minutes total: skip the question and do other things
        "thinking_nudge_activity_minutes": 5,                # Do not nudge if user was active on any channel in the last N minutes
        "thinking_provider": "inherit",                      # AI provider for thinking mode ('inherit' or e.g. 'openai', 'local')
        "thinking_model": None,                              # Specific model for thinking mode (None = use provider default)
        "thinking_cooldown_minutes": 60,                     # After a thinking run completes: wait this many minutes before starting another
        "thinking_gc_hours": 12,                              # GC deletes thinking-mode sessions older than this many hours
        "thinking_quiet_hours_enabled": False,               # Do not run thinking mode during quiet hours (local time)
        "thinking_quiet_hours_start": "23:00",                # Quiet period start (HH:MM, 24h); e.g. 23:00 = 11 PM
        "thinking_quiet_hours_end": "07:00",                 # Quiet period end (HH:MM, 24h); e.g. 07:00 = 7 AM (overnight span supported)

        # Garbage Collector Settings
        "gc_enabled": True,                    # Enable automatic temp file / log cleanup
        "gc_interval_hours": 12,               # Run GC every N hours
        "gc_max_age_hours": 48,                # Delete files older than N hours

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
        "memory_db_url": "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory",  # PostgreSQL connection URL
        "memory_encryption_key": "",                               # AES-256 key (Base64, auto-generated if empty)
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
        "attachment_rag_hierarchical_enabled": False,               # Opt-in: build section index for large structured docs (vector mode only)
        "attachment_rag_hierarchical_min_chars": 4000,              # Min doc length to activate hierarchical indexing (chars)
        "attachment_rag_hierarchical_max_sections": 15,             # Max sections to index per document (2-50)
        "attachment_rag_hierarchical_coarse_k": 3,                  # Top-k sections selected in Tier 1 retrieval (1-10)

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

        # Front Office: when True, replies to contacts (from_contact) require explicit approval in Web UI before sending.
        # Default False: contacts you added with "Can reach your assistant" get replies directly; set True to review each reply first.
        "front_office_contact_reply_require_approval": False,

        # Email connections: accounts only (no passwords/tokens in config).
        # Credentials stored in OS keyring or encrypted file (see vaf.core.credential_store).
        "email_config": None,  # { "accounts": [ { "account_id", "provider", "email", "enabled", "imap_host?", "imap_port?", "smtp_host?", "smtp_port?" } ] }
        "email_config_by_scope": None,  # { "<user_scope_id_uuid>": { "accounts": [...] } } — UUID-based per-user config (preferred)
        "email_config_by_user": None,  # { "<username>": { "accounts": [...] } } — legacy per-username config
        "email_credentials_key": "",  # AES key (Base64) for fallback encrypted file; auto-generated if empty
        # OAuth2: callback base URL must point to this backend (default http://127.0.0.1:8001). Set if behind proxy or different port.
        "email_oauth_callback_base_url": "",
        # OAuth2 client IDs (register app in Google Cloud Console / Azure / Apple; redirect_uri = {email_oauth_callback_base_url or http://127.0.0.1:PORT}/api/email/oauth/callback)
        "email_oauth_google_client_id": "",
        "email_oauth_google_client_secret": "",
        "email_oauth_microsoft_client_id": "",
        "email_oauth_microsoft_client_secret": "",
        "email_oauth_apple_client_id": "",
        "email_oauth_apple_client_secret": "",
    }

    @classmethod
    def load(cls) -> dict:
        if not cls.CONFIG_FILE.exists():
            return cls.DEFAULTS.copy()
        try:
            with open(cls.CONFIG_FILE, "r") as f:
                data = json.load(f)
            result = {**cls.DEFAULTS, **data}
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
                        with open(cls.CONFIG_FILE, "w") as _f:
                            _json.dump(_raw, _f, indent=4)
                except Exception:
                    pass
            # Hard lock for hosting mode (server appliance deployments):
            # when enabled, Local Network Hosting cannot be disabled via UI/API saves.
            if bool(result.get("local_network_force_enabled", False)):
                result["local_network_enabled"] = True
            # Security invariant: local network hosting must always run with TLS enabled.
            if bool(result.get("local_network_enabled", False)):
                result["local_network_tls_enabled"] = True
            return result
        except Exception:
            return cls.DEFAULTS.copy()

    # Keys that should never be overwritten when saving from frontend
    # These are auto-generated secrets that would break auth if lost
    PROTECTED_KEYS = [
        "local_network_jwt_secret",
        "email_credentials_key",
        "cloud_credentials_key",
    ]

    # Keys (and prefixes) that only admins can change. Non-admins can change user-scoped
    # settings (e.g. language, interface) but not backend/network/API config.
    GLOBAL_CONFIG_KEY_PREFIXES = (
        "local_network_",
        "api_key_",
        "api_model_",
        "email_oauth_",
        "cloud_oauth_",
        "github_oauth_",
        "speech_stt_",
        "speech_tts_",
        "subagent_",
        "thinking_",
        "librarian_",
        "document_conversion_",
    )
    GLOBAL_CONFIG_KEYS = frozenset([
        "provider", "model", "n_ctx", "gpu_layers", "n_parallel", "llama_cache_ram",
        "auto_start_local_server", "tray_autostart", "web_ui_enabled", "server_persistence_enabled",
        "debug_logs_enabled", "server_idle_timeout", "telegram_idle_timeout", "telegram_debounce_seconds",
        "redis_url", "redis_enabled", "use_docker",
        "local_admin_scope_id", "local_admin_username",
        "channel_ingress_policy",
    ])

    @classmethod
    def is_global_config_key(cls, key: str) -> bool:
        """True if this config key may only be written by an admin (backend/network/API)."""
        if key in cls.GLOBAL_CONFIG_KEYS:
            return True
        return any(key.startswith(prefix) for prefix in cls.GLOBAL_CONFIG_KEY_PREFIXES)

    # Connection config keys that only admin may write (enabled/whitelist etc.). Non-admins write to connection_enabled_by_scope instead.
    CONNECTION_CONFIG_KEYS = frozenset({"telegram_config", "whatsapp_config", "discord_config"})

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
            return dict(config)
        out = dict(config)
        scope_str = str(user_scope_id).strip() if user_scope_id else None

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
            if key.startswith("api_key_"):
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
    def save(cls, config: dict):
        if not cls.APP_DIR.exists():
            cls.APP_DIR.mkdir(parents=True, exist_ok=True)

        # Preserve protected keys from existing config
        existing_config = cls.load()
        for key in cls.PROTECTED_KEYS:
            if key in existing_config and key not in config:
                config[key] = existing_config[key]

        with open(cls.CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)

    @classmethod
    def get(cls, key: str, default=None):
        return cls.load().get(key, default if default is not None else cls.DEFAULTS.get(key))

    @classmethod
    def set(cls, key: str, value):
        config = cls.load()
        config[key] = value
        cls.save(config)
    
    @classmethod
    def set_api_key(cls, provider: str, api_key: str):
        """
        Securely store API key with basic obfuscation.
        Best Practice: Base64 encoding for storage (not encryption, but prevents casual viewing)
        
        Args:
            provider: Provider name (veyllo, openai, anthropic, deepseek, google, openrouter)
            api_key: Raw API key string
        """
        if not api_key:
            return

        # Basic obfuscation using base64
        encoded = base64.b64encode(api_key.encode()).decode()
        cls.set(f"api_key_{provider}", encoded)
    
    @classmethod
    def get_api_key(cls, provider: str) -> str:
        """
        Retrieve and decode API key.
        
        Args:
            provider: Provider name
            
        Returns:
            Decoded API key string
        """
        encoded = cls.get(f"api_key_{provider}", "")

        if not encoded:
            return ""
        
        try:
            # Decode from base64
            return base64.b64decode(encoded.encode()).decode()
        except Exception:
            # If decoding fails, assume it's plain text (backward compatibility)
            return encoded
    
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

        # Load existing to detect changes
        existing_config = cls.load()
        
        # Preserve protected keys from existing config
        for key in cls.PROTECTED_KEYS:
            if key in existing_config and key not in config:
                config[key] = existing_config[key]

        # Hosting lock: keep Local Network Hosting enabled if lock is active.
        force_network = bool(
            config.get("local_network_force_enabled", existing_config.get("local_network_force_enabled", False))
        )
        if force_network:
            config["local_network_enabled"] = True
        # Security invariant: it must not be possible to persist network mode with TLS disabled.
        if bool(config.get("local_network_enabled", False)):
            config["local_network_tls_enabled"] = True

        with open(cls.CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
            
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
        ]
        
        for key in critical_keys:
            old_val = existing_config.get(key)
            new_val = config.get(key)
            if old_val != new_val:
                cls.notify_observers(key, new_val, old_val)


def get_local_admin_scope_id() -> str:
    """Return the local admin user_scope_id (UUID). Use this instead of Config.get('local_admin_scope_id', ...)."""
    return str(Config.get("local_admin_scope_id", LEGACY_LOCAL_ADMIN_SCOPE_ID) or LEGACY_LOCAL_ADMIN_SCOPE_ID).strip()


def get_local_admin_username() -> str:
    """Return the local admin username. Use for display and paths when no JWT."""
    return (Config.get("local_admin_username") or "admin").strip()
