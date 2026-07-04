# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Chat toggle for the Co-authored-by trailer on VAF-authored commits.

VAF appends "Co-authored-by: VAF Agent <noreply@veyllo.app>" to commits whose
content it authored itself (project versioning commits, the coder's final
commit, GitHub file commits) — see apply_coauthor_trailer in project_git.py.
This tool lets the user turn that attribution on/off (or change the identity)
in plain chat, mirroring how coding assistants handle "stop adding yourself
as co-author". User-initiated commits (`vaf git commit`) never get the
trailer, so there is nothing to disable for them.
"""

import re

from vaf.tools.base import BaseTool


_IDENTITY_RE = re.compile(r"^[^<>]+ <[^<>@\s]+@[^<>@\s]+>$")


class SetGitCoauthorTool(BaseTool):
    """Turn VAF's commit co-author attribution on or off."""

    name = "set_git_coauthor"
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Enable or disable the 'Co-authored-by: VAF Agent' trailer that VAF appends to git commits it "
        "creates itself (project version commits, coder final commits, GitHub file commits). "
        "Use when the user says e.g. 'don't add yourself as co-author anymore', 'stop the co-authored-by "
        "line in commits', or 'name yourself as co-author again'. Optionally set a custom identity "
        "in 'Name <email>' form. The user's own commits are never touched by this trailer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "true = append the co-author trailer to VAF-authored commits, false = stop appending it.",
            },
            "identity": {
                "type": "string",
                "description": "Optional trailer identity in 'Name <email>' form (default: 'VAF Agent <noreply@veyllo.app>'). Omit to keep the current one.",
            },
        },
        "required": ["enabled"],
    }

    def run(self, **kwargs) -> str:
        enabled = kwargs.get("enabled")
        if isinstance(enabled, str):
            s = enabled.strip().lower()
            if s in ("true", "1", "yes", "on"):
                enabled = True
            elif s in ("false", "0", "no", "off"):
                enabled = False
            else:
                enabled = None
        if not isinstance(enabled, bool):
            return "Error: 'enabled' must be true or false."

        identity = (kwargs.get("identity") or "").strip()
        if identity and not _IDENTITY_RE.match(identity):
            return (
                f"Error: identity '{identity}' is not in 'Name <email>' form "
                "(e.g. 'VAF Agent <noreply@veyllo.app>'). Nothing was changed."
            )

        try:
            from vaf.core.config import Config
            Config.set("git_coauthor_enabled", enabled)
            if identity:
                Config.set("git_coauthor_identity", identity)
            effective = (Config.get("git_coauthor_identity") or "").strip()
        except Exception as e:
            return f"Error updating co-author setting: {e}"

        if enabled and effective:
            return (
                f"Co-author trailer ENABLED: commits VAF creates now end with "
                f"'Co-authored-by: {effective}'. The user's own commits are never touched."
            )
        if enabled and not effective:
            return (
                "Co-author trailer is enabled but the identity is empty, so no trailer will be "
                "appended. Provide an identity in 'Name <email>' form to activate it."
            )
        return "Co-author trailer DISABLED: VAF no longer names itself in commit messages."
