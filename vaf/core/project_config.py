"""
VAF Project Configuration
Lokale Projektkonfiguration über vaf.config.json
"""
import os
import json
from pathlib import Path
from typing import Optional, Dict, Any

class ProjectConfig:
    """
    Verwaltet projektspezifische Konfiguration.
    Sucht nach vaf.config.json im aktuellen oder übergeordneten Verzeichnis.
    """
    
    CONFIG_FILENAME = "vaf.config.json"
    
    DEFAULTS = {
        "language": "auto",
        "framework": None,
        "output_dir": ".",
        "test_command": None,
        "build_command": None,
        "start_command": None,
        "ignore_patterns": [
            "node_modules",
            "__pycache__",
            ".git",
            "venv",
            ".env",
            "dist",
            "build"
        ],
        "ai_settings": {
            "temperature": 0.7,
            "context_files": [],
            "exclude_patterns": []
        }
    }
    
    # Sprache erkennen basierend auf Dateien
    LANGUAGE_MARKERS = {
        "python": ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile"],
        "javascript": ["package.json"],
        "typescript": ["tsconfig.json"],
        "rust": ["Cargo.toml"],
        "go": ["go.mod"],
        "java": ["pom.xml", "build.gradle"],
        "csharp": ["*.csproj", "*.sln"],
        "php": ["composer.json"],
        "ruby": ["Gemfile"],
    }
    
    @classmethod
    def find_config_path(cls, start_path: str = ".") -> Optional[Path]:
        """
        Sucht nach vaf.config.json im aktuellen und übergeordneten Verzeichnissen.
        """
        current = Path(start_path).resolve()
        
        # Maximal 10 Ebenen nach oben
        for _ in range(10):
            config_file = current / cls.CONFIG_FILENAME
            if config_file.exists():
                return config_file
            
            parent = current.parent
            if parent == current:  # Root erreicht
                break
            current = parent
        
        return None
    
    @classmethod
    def load(cls, path: str = ".") -> Dict[str, Any]:
        """
        Lädt die Projektkonfiguration.
        Falls keine existiert, werden Defaults + Auto-Detection verwendet.
        """
        config_path = cls.find_config_path(path)
        
        if config_path:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                    # Merge mit Defaults
                    return {**cls.DEFAULTS, **user_config, "_config_path": str(config_path)}
            except Exception:
                pass
        
        # Keine Config gefunden - Auto-Detection
        config = cls.DEFAULTS.copy()
        config["language"] = cls.detect_language(path)
        config["_config_path"] = None
        return config
    
    @classmethod
    def save(cls, config: Dict[str, Any], path: str = "."):
        """
        Speichert die Konfiguration als vaf.config.json.
        """
        # Entferne interne Keys
        save_config = {k: v for k, v in config.items() if not k.startswith("_")}
        
        config_path = Path(path) / cls.CONFIG_FILENAME
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(save_config, f, indent=4, ensure_ascii=False)
        
        return config_path
    
    @classmethod
    def detect_language(cls, path: str = ".") -> str:
        """
        Erkennt automatisch die Projektsprache basierend auf Marker-Dateien.
        """
        path = Path(path).resolve()
        
        for language, markers in cls.LANGUAGE_MARKERS.items():
            for marker in markers:
                if "*" in marker:
                    # Glob-Pattern
                    if list(path.glob(marker)):
                        return language
                else:
                    if (path / marker).exists():
                        return language
        
        return "unknown"
    
    @classmethod
    def detect_test_command(cls, path: str = ".") -> Optional[str]:
        """
        Erkennt automatisch den Test-Befehl basierend auf der Projektstruktur.
        """
        path = Path(path).resolve()
        
        # Python
        if (path / "pytest.ini").exists() or (path / "tests").exists():
            return "pytest"
        if (path / "setup.py").exists():
            return "python -m pytest"
        
        # JavaScript/TypeScript
        if (path / "package.json").exists():
            try:
                with open(path / "package.json") as f:
                    pkg = json.load(f)
                    if "scripts" in pkg and "test" in pkg["scripts"]:
                        return "npm test"
            except:
                pass
        
        # Rust
        if (path / "Cargo.toml").exists():
            return "cargo test"
        
        # Go
        if (path / "go.mod").exists():
            return "go test ./..."
        
        return None
    
    @classmethod
    def detect_build_command(cls, path: str = ".") -> Optional[str]:
        """
        Erkennt automatisch den Build-Befehl.
        """
        path = Path(path).resolve()
        
        # TypeScript
        if (path / "tsconfig.json").exists():
            if (path / "package.json").exists():
                return "npm run build"
            return "tsc"
        
        # JavaScript mit Build
        if (path / "package.json").exists():
            try:
                with open(path / "package.json") as f:
                    pkg = json.load(f)
                    if "scripts" in pkg and "build" in pkg["scripts"]:
                        return "npm run build"
            except:
                pass
        
        # Rust
        if (path / "Cargo.toml").exists():
            return "cargo build"
        
        # Go
        if (path / "go.mod").exists():
            return "go build"
        
        # Python (setup.py)
        if (path / "setup.py").exists():
            return "python setup.py build"
        
        return None
    
    @classmethod
    def init(cls, path: str = ".", language: str = "auto") -> Path:
        """
        Initialisiert eine neue vaf.config.json im Projekt.
        """
        detected_lang = cls.detect_language(path) if language == "auto" else language
        
        config = {
            "language": detected_lang,
            "framework": None,
            "output_dir": ".",
            "test_command": cls.detect_test_command(path),
            "build_command": cls.detect_build_command(path),
            "ignore_patterns": cls.DEFAULTS["ignore_patterns"],
            "ai_settings": {
                "temperature": 0.7,
                "context_files": [],
                "exclude_patterns": []
            }
        }
        
        return cls.save(config, path)

