# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
User-scoped configuration and file storage.

Per-user directories under Platform.data_dir()/users/{username}/.
"""

from pathlib import Path

from vaf.core.platform import Platform


class UserConfig:
    """The per-user folder under the data directory (the legacy per-name stores)."""

    @staticmethod
    def get_user_dir(username: str) -> Path:
        """Base directory for a user's data."""
        return Platform.data_dir() / "users" / username

    @staticmethod
    def ensure_user_dir(username: str) -> Path:
        """Create user directory if it does not exist. Returns the path."""
        path = UserConfig.get_user_dir(username)
        path.mkdir(parents=True, exist_ok=True)
        return path
