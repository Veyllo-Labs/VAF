import os
import json
from pathlib import Path
from typing import Optional
import base64

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
        # For production, consider using system keyring
        "api_key_openai": "",
        "api_key_anthropic": "",
        "api_key_deepseek": "",
        "api_key_google": "",
        "api_key_openrouter": "",
        
        # API Model Selection per Provider
        "api_model_openai": "gpt-4o",
        "api_model_anthropic": "claude-3-5-sonnet-20241022",
        "api_model_deepseek": "deepseek-chat",
        "api_model_google": "gemini-1.5-flash",  # Free tier, fast & capable
        "api_model_openrouter": "anthropic/claude-3.5-sonnet",
        
        # Sub-Agent Provider Configuration
        "subagent_provider": "inherit",  # Options: "inherit", or any provider name
        "subagent_use_separate_provider": False,
        
        # Auto-start local llama-server (disable if only using APIs)
        "auto_start_local_server": True,

        # UX toggles (opt-in)
        # Auto open web search source links in the user's default browser (tabs)
        "ux_auto_open_links": True,
        # Auto open created output folders/files (file explorer / browser for html)
        "ux_auto_open_outputs": True,
        # Safety cap for tabs opened automatically
        "ux_auto_open_max_tabs": 8,
        # Run each sub-agent in its own terminal window
        "sub_agents_in_separate_terminals": True,
                # Sub-Agent timeout settings
                "subagent_timeout_enabled": True,      # Enable/disable timeout for sub-agents
                "subagent_timeout_minutes": 120,       # Timeout in minutes (default: 2 hours)
                
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
        "librarian_auto_chunk_large_files": True,  # Auto-chunk large files (default: True)
        "librarian_pdf_max_pages_preview": 50, # Max pages to show in preview (default: 50)
        
        # System Settings
        "web_ui_enabled": True,                # Start Web UI automatically
        "server_persistence_enabled": False,   # Keep server running after exit
        "tray_autostart": False,               # Auto-start tray on OS login
        "debug_logs_enabled": False,           # Write domain logs and queue.log when enabled; off by default to reduce I/O
        "server_idle_timeout": 15,             # Unload local model after idle seconds
        
        # Memory System Settings (RAG + Vector Search)
        "memory_enabled": True,                                    # Enable memory system
        "memory_rag_refine_query": True,                           # Refine vague queries (e.g. "who am I") for better RAG hits
        "memory_auto_capture": True,                                # Auto-store high-value snippets after agent response
        "memory_compaction_enabled": True,                          # Session compaction: prompt to store durable memories every N turns
        "memory_compaction_interval": 15,                           # Run compaction every N user/assistant turns
        "memory_db_url": "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory",  # PostgreSQL connection URL
        "memory_encryption_key": "",                               # AES-256 key (Base64, auto-generated if empty)
        "memory_embedding_model": "all-MiniLM-L6-v2",             # Sentence-transformers model
        "memory_auto_connect_threshold": 0.7,                      # Cosine similarity threshold for auto-connections
        "memory_chunk_size": 512,                                  # Chunk size in tokens
        "memory_chunk_overlap": 50,                                # Chunk overlap in tokens
        "memory_db_echo": False,                                   # Enable SQL query logging (debug)
        
        # Redis Cache Settings
        "redis_url": "redis://localhost:6379/0",                   # Redis connection URL
        "redis_enabled": True,                                     # Enable Redis caching
        
        # Local Admin Settings (for localhost without login)
        # user_identity.json and RAG/memory scope use these when no auth (local only)
        "local_admin_scope_id": "00000000-0000-0000-0000-000000000001",  # Fixed UUID for Local Admin user scope (DB/RAG)
        "local_admin_username": "admin",  # Username for ~/.vaf/users/<this>/user_identity.json when local (WebSocket + HTTP API)

        # Local Network Settings
        "local_network_enabled": False,                            # Enable local network access (LAN only)
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
        
        # Docker Settings (Desktop Mode only)
        # Note: CLI mode (vaf run) always runs natively with full host access
        # Docker mode is only for Desktop/Tray mode for isolation
        "use_docker": True,                                        # Desktop: Run backend/frontend in Docker
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
            return result
        except Exception:
            return cls.DEFAULTS.copy()

    # Keys that should never be overwritten when saving from frontend
    # These are auto-generated secrets that would break auth if lost
    PROTECTED_KEYS = [
        "local_network_jwt_secret",
    ]

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
            provider: Provider name (openai, anthropic, deepseek, google, openrouter)
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
    _observers_lock = threading.Lock() if 'threading' in globals() else None

    @classmethod
    def add_observer(cls, callback):
        """
        Add a callback function to be notified of configuration changes.
        Callback signature: callback(key: str, new_value: Any)
        """
        # Lazy import threading if needed (though it's usually standard)
        if cls._observers_lock is None:
            import threading
            cls._observers_lock = threading.Lock()
            
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

        with open(cls.CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
            
        # Detect and notify changes for critical keys
        # local_network_* for server restart; provider for tray VRAM load/unload
        critical_keys = ["local_network_enabled", "local_network_port", "local_network_port_frontend", "provider"]
        
        for key in critical_keys:
            old_val = existing_config.get(key)
            new_val = config.get(key)
            if old_val != new_val:
                cls.notify_observers(key, new_val, old_val)

