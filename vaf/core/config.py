import os
import json
from pathlib import Path
from typing import Optional
import base64

class Config:
    APP_DIR = Path.home() / ".vaf"
    CONFIG_FILE = APP_DIR / "config.json"
    
    DEFAULTS = {
        "model": "Veyllo/VQ-1_Instruct-q4_k_m",
        "provider": "local",
        "gpu_layers": -1,
        "n_ctx": 8192,
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
                "stt_wake_word_enabled": False,        # Enable Wake Word detection (Auto Mode)
                "stt_wake_word": "hey_jarvis",         # Wake Word model name (openWakeWord)
                
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
        "server_idle_timeout": 15,             # Unload local model after idle seconds
    }

    @classmethod
    def load(cls) -> dict:
        if not cls.CONFIG_FILE.exists():
            return cls.DEFAULTS.copy()
        try:
            with open(cls.CONFIG_FILE, "r") as f:
                data = json.load(f)
                return {**cls.DEFAULTS, **data}
        except Exception:
            return cls.DEFAULTS.copy()

    @classmethod
    def save(cls, config: dict):
        if not cls.APP_DIR.exists():
            cls.APP_DIR.mkdir(parents=True, exist_ok=True)
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