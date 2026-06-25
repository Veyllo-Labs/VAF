# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
GitHub Activity Logger

Persists recent agent actions related to GitHub for the dashboard.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

logger = logging.getLogger("vaf.github.activity")

_MAX_ACTIVITY_LOGS = 100


def _get_activity_file(username: str) -> Path:
    """Return the path to the activity log file for a specific user."""
    # Store in user-specific directory if possible, else data_dir
    safe_user = (username or "admin").strip() or "admin"
    base_dir = Platform.data_dir() / "users" / safe_user if safe_user != "admin" else Platform.data_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "github_activity.json"


def log_github_activity(
    username: str,
    action: str,
    details: str,
    account_id: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None
) -> None:
    """
    Log a GitHub action performed by the agent.
    
    Args:
        username: The VAF username.
        action: The name of the action (e.g., 'list_repos', 'get_file').
        details: A human-readable description of what was done.
        account_id: The GitHub login/account ID used.
        success: Whether the action was successful.
        error: Error message if the action failed.
    """
    try:
        path = _get_activity_file(username)
        logs = []
        if path.exists():
            try:
                logs = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logs = []
        
        entry = {
            "timestamp": time.time(),
            "action": action,
            "details": details,
            "account_id": account_id,
            "success": success,
            "error": error
        }
        
        logs.insert(0, entry)
        logs = logs[:_MAX_ACTIVITY_LOGS]
        
        path.write_text(json.dumps(logs, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to log GitHub activity: {e}")


def get_github_activity(username: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return the recent GitHub activity for a user."""
    try:
        path = _get_activity_file(username)
        if not path.exists():
            return []
        
        logs = json.loads(path.read_text(encoding="utf-8"))
        return logs[:limit]
    except Exception as e:
        logger.warning(f"Failed to read GitHub activity: {e}")
        return []
