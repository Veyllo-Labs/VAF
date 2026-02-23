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
    Includes fallback for local admin when primary username has no auth.

    Args:
        username: VAF username (e.g. from session or local_admin_username)

    Returns:
        Path to user's WhatsApp auth directory
    """
    primary = Config.APP_DIR / "users" / username / "whatsapp"
    if (primary / "creds.json").exists():
        return primary
    
    # Fallback for local admin (single-user setups where name might vary)
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if username.lower() != local_admin:
        fallback = Config.APP_DIR / "users" / local_admin / "whatsapp"
        if (fallback / "creds.json").exists():
            return fallback
            
    return primary


def whatsapp_auth_exists(username: str) -> bool:
    """Check if this user has linked WhatsApp (creds.json present)."""
    auth_dir = get_whatsapp_auth_dir(username)
    creds_path = auth_dir / "creds.json"
    return creds_path.exists() and creds_path.is_file()
