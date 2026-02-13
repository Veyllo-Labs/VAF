"""
WhatsApp auth directory resolution per VAF user.

Each user has isolated credentials at ~/.vaf/users/<username>/whatsapp/
for full user isolation (no shared WhatsApp sessions).
"""
from pathlib import Path

from vaf.core.config import Config


def get_whatsapp_auth_dir(username: str) -> Path:
    """
    Return the WhatsApp auth directory for the given VAF username.
    Credentials (creds.json, Baileys multi-file state) are stored here.

    Args:
        username: VAF username (e.g. from session or local_admin_username)

    Returns:
        Path to user's WhatsApp auth directory
    """
    return Config.APP_DIR / "users" / username / "whatsapp"


def whatsapp_auth_exists(username: str) -> bool:
    """Check if this user has linked WhatsApp (creds.json present)."""
    auth_dir = get_whatsapp_auth_dir(username)
    creds_path = auth_dir / "creds.json"
    return creds_path.exists() and creds_path.is_file()
