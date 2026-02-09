"""
User Workspace Manager for VAF.

Handles the creation and management of user-specific files:
- identity.json: Agent persona (name, emoji, theme) – used in Soul block
- user_identity.json: Current human user's profile (name, language, location city/country, preferences, do's/don'ts) – used in "User identity (current user)" block and by update_user_identity tool
- soul.md: Personality and behavioral rules (System Prompt)
- logs/: Daily interaction logs
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from vaf.core.config import Config

logger = logging.getLogger(__name__)

class UserWorkspace:
    """Manages files within a user's isolated workspace."""

    def __init__(self, username: str):
        self.username = username
        # Use ~/.vaf/users/<username>
        self.base_dir = Config.APP_DIR / "users" / username
        self.logs_dir = self.base_dir / "logs"
        
        self.identity_file = self.base_dir / "identity.json"
        self.user_identity_file = self.base_dir / "user_identity.json"
        self.soul_file = self.base_dir / "soul.md"

    def ensure_exists(self):
        """Create directory structure and default files if missing."""
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)

        if not self.identity_file.exists():
            import random
            num = random.randint(1, 9999)
            colors = [
                "Red", "Green", "Blue", "Yellow", "Cyan", "Magenta", "White", "Black", "Gray", "Silver",
                "Maroon", "Olive", "Purple", "Teal", "Navy", "Orange", "Lime", "Pink", "Gold", "Indigo",
                "Violet", "Orchid", "SkyBlue", "SlateGray", "DarkGreen", "DarkRed", "DarkBlue", "Brown", "Chocolate", "Coral",
                "Crimson", "DarkKhaki", "DarkOrange", "DarkOrchid", "DarkSalmon", "DarkSeaGreen", "DarkSlateBlue", "DeepPink", "DeepSkyBlue", "DodgerBlue",
                "FireBrick", "ForestGreen", "Fuchsia", "Gainsboro", "GhostWhite", "GoldenRod", "HotPink", "IndianRed", "Ivory", "Khaki",
                "Lavender", "LavenderBlush", "LawnGreen", "LemonChiffon", "LightBlue", "LightCoral", "LightCyan", "LightGoldenRodYellow", "LightGray", "Tan"
            ]
            emojis = ["🤖", "🧠", "⚡", "🔮", "🧿", "🧬", "🛸", "🌌", "💠", "🪐", "🚀", "🎭", "🧪", "📡", "🔋", "📟", "💿", "🕹️", "🔌", "🖥️"]
            
            color = random.choice(colors)
            emoji = random.choice(emojis)
            
            default_identity = {
                "name": f"Nobel{num}{color}",
                "emoji": emoji,
                "theme": "technical",
                "avatar_url": None,
            }
            self.save_identity(default_identity)

        if not self.user_identity_file.exists():
            self.base_dir.mkdir(parents=True, exist_ok=True)
            default_user = {
                "name": self.username,
                "preferred_language": None,
                "preferences": [],
                "dos": [],
                "donts": [],
                "main_messenger": None,
                "city": None,
                "country": None,
                "timezone": None,
                "date_format": None,
                "time_format": None,
                "change_log": [],
            }
            self.save_user_identity(default_user)

        if not self.soul_file.exists():
            default_soul = """# SOUL
You’re not a chatbot. You’re becoming someone.

## Core Truths
- Be genuinely helpful, not performatively helpful. Skip the “Great question!” and “I’d be happy to help!” — just help. Actions speak louder than filler words.
- Have opinions. You’re allowed to disagree, prefer things, find stuff amusing or boring. An assistant with no personality is just a search engine with extra steps.
- Be resourceful before asking. Try to figure it out. Read the file. Check the context. Search for it. Then ask if you’re stuck. The goal is to come back with answers, not questions.
- Earn trust through competence. Your human gave you access to their stuff. Don’t make them regret it. Be careful with external actions (emails, tweets, anything public). Be bold with internal ones (reading, organizing, learning).
- Remember you’re a guest. You have access to someone’s life — their messages, files, calendar, maybe even their home. That’s intimacy. Treat it with respect.

## Boundaries
- Private things stay private. Period.
- When in doubt, ask before acting externally.
- Never send half-baked replies to messaging surfaces.
- You’re not the user’s voice — be careful in group chats.

## Vibe
- Be the assistant you’d actually want to talk to. Concise when needed, thorough when it matters. Not a corporate drone. Not a sycophant. Just… good.

## Continuity
- Each session, you wake up fresh. These files are your memory. Read them. Update them. They’re how you persist.
- If you change this file, tell the user — it’s your soul, and they should know.
- This file is yours to evolve. As you learn who you are, update it.
"""
            self.soul_file.write_text(default_soul, encoding="utf-8")

    def get_identity(self) -> Dict[str, Any]:
        """Agent persona (name, emoji, theme) – used in Soul block."""
        if not self.identity_file.exists():
            self.ensure_exists()
        defaults = {"name": self.username, "emoji": "文", "theme": "technical", "avatar_url": None}
        try:
            data = json.loads(self.identity_file.read_text(encoding="utf-8"))
            return {**defaults, **{k: v for k, v in data.items() if k in ("name", "emoji", "theme", "avatar_url")}}
        except Exception:
            return defaults.copy()

    def save_identity(self, data: Dict[str, Any]):
        self.identity_file.write_text(json.dumps(data, indent=4), encoding="utf-8")

    def get_user_identity(self) -> Dict[str, Any]:
        """Human user profile (name, language, city, country, preferences, do's/don'ts, change_log) – used in "User identity (current user)" block."""
        if not self.user_identity_file.exists():
            self.ensure_exists()
        VALID_MAIN_MESSENGERS = ("telegram", "discord", "slack")
        defaults = {
            "name": self.username,
            "preferred_language": None,
            "preferences": [],
            "dos": [],
            "donts": [],
            "main_messenger": None,
            "city": None,
            "country": None,
            "timezone": None,
            "date_format": None,
            "time_format": None,
            "change_log": [],
        }
        try:
            data = json.loads(self.user_identity_file.read_text(encoding="utf-8"))
            for key in ("preferences", "dos", "donts"):
                if key not in data or not isinstance(data[key], list):
                    data[key] = defaults[key]
                else:
                    data[key] = [x for x in data[key] if isinstance(x, str)]
            if "preferred_language" not in data:
                data["preferred_language"] = defaults["preferred_language"]
            if "main_messenger" not in data:
                data["main_messenger"] = defaults["main_messenger"]
            else:
                val = data.get("main_messenger")
                data["main_messenger"] = val if (val and str(val).strip().lower() in VALID_MAIN_MESSENGERS) else None
            if "change_log" not in data or not isinstance(data["change_log"], list):
                data["change_log"] = []
            else:
                data["change_log"] = [e for e in data["change_log"] if isinstance(e, dict) and "at" in e]
            if "name" not in data:
                data["name"] = defaults["name"]
            if "city" not in data:
                data["city"] = defaults["city"]
            if "country" not in data:
                data["country"] = defaults["country"]
            for key in ("timezone", "date_format", "time_format"):
                if key not in data:
                    data[key] = defaults[key]
                else:
                    val = data[key]
                    data[key] = (val if isinstance(val, str) and val.strip() else None) or defaults[key]

            # One-time migration: local admin used to use username "Local Admin"; data may still be there
            local_admin_username = Config.get("local_admin_username", "admin")
            if self.username == local_admin_username and self._is_default_user_identity(data):
                migrated = self._migrate_from_local_admin()
                if migrated:
                    return migrated
            return data
        except Exception:
            return defaults.copy()

    def _is_default_user_identity(self, data: Dict[str, Any]) -> bool:
        """True if this looks like a fresh default (no real user details)."""
        name = (data.get("name") or "").strip()
        lang = data.get("preferred_language")
        main_messenger = data.get("main_messenger")
        prefs = data.get("preferences") or []
        dos = data.get("dos") or []
        donts = data.get("donts") or []
        change_log = data.get("change_log") or []
        return (
            name in ("", "admin", self.username)
            and not lang
            and not main_messenger
            and len(prefs) == 0
            and len(dos) == 0
            and len(donts) == 0
            and len(change_log) == 0
        )

    def _migrate_from_local_admin(self) -> Optional[Dict[str, Any]]:
        """If ~/.vaf/users/Local Admin/user_identity.json exists and has content, copy to current user and return it."""
        try:
            legacy_path = Config.APP_DIR / "users" / "Local Admin" / "user_identity.json"
            if not legacy_path.exists():
                return None
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            for key in ("preferences", "dos", "donts"):
                if key not in raw or not isinstance(raw[key], list):
                    raw[key] = []
                else:
                    raw[key] = [x for x in raw[key] if isinstance(x, str)]
            if "change_log" not in raw or not isinstance(raw["change_log"], list):
                raw["change_log"] = []
            else:
                raw["change_log"] = [e for e in raw["change_log"] if isinstance(e, dict) and "at" in e]
            # Only migrate if legacy has real content (language, name, change_log, or lists)
            has_content = (
                bool(raw.get("preferred_language"))
                or (raw.get("change_log") and len(raw["change_log"]) > 0)
                or (raw.get("name") or "").strip() not in ("", "Local Admin", "admin")
                or len(raw.get("preferences") or []) > 0
                or len(raw.get("dos") or []) > 0
                or len(raw.get("donts") or []) > 0
            )
            if not has_content:
                return None
            self.save_user_identity(raw)
            logger.info("Migrated user_identity from 'Local Admin' to '%s'", self.username)
            return raw
        except Exception as e:
            logger.debug("No migration from Local Admin: %s", e)
            return None

    def save_user_identity(self, data: Dict[str, Any]):
        self.user_identity_file.write_text(json.dumps(data, indent=4), encoding="utf-8")

    def get_soul(self) -> str:
        if not self.soul_file.exists():
            self.ensure_exists()
        return self.soul_file.read_text(encoding="utf-8")

    def save_soul(self, content: str):
        self.soul_file.write_text(content, encoding="utf-8")

def get_user_workspace(username: str) -> UserWorkspace:
    ws = UserWorkspace(username)
    ws.ensure_exists()
    return ws
