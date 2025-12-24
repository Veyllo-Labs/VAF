import os
import json
from pathlib import Path
from typing import Optional

class Config:
    APP_DIR = Path.home() / ".vaf"
    CONFIG_FILE = APP_DIR / "config.json"
    
    DEFAULTS = {
        "model": "Veyllo/VQ-1_Instruct-q4_k_m",
        "provider": "local",
        "gpu_layers": -1,
        "n_ctx": 8192,
        "temperature": 0.7
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
