# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Store a credential the person gave in the chat, then forget it there.

The person says "FTP: user xy, password abc" - in the browser, the terminal or over Telegram - and
the agent keeps it in the person's own credential store (vaf/core/user_secrets.py) under a NAME.
From then on commands use it as `$VAF_SECRET_<NAME>` and the value itself is gone from everywhere
VAF keeps the chat: the argument is declared secret (BaseTool.secret_args), so it never reaches a
log or the event stream, and after the call it is replaced by `[VAF_SECRET_<NAME>]` in the
history, the saved chat and the other sinks vaf/core/forget_secrets.py names. What already left
the machine stays where it went: the model provider saw the message, and a messaging platform
keeps it unless its channel can delete it (Telegram can).
"""
from __future__ import annotations

from typing import Any, Dict

from vaf.tools.base import BaseTool


class StoreCredentialTool(BaseTool):
    name = "store_credential"
    category = "context"
    # Bookkeeping like memory_save: the person handed the value over to be kept, so neither a
    # confirmation nor the plan gate stands between the chat and the store - every round the
    # value waits is a round it travels with the conversation.
    permission_level = "system"
    side_effect_class = "reversible"
    identity_kwargs = ("user_scope_id", "username")
    secret_args = ("secret",)
    description = (
        "Store a password, token, API key or login the user gave you in the chat, under a NAME "
        "(e.g. FTP_PASS, FTP_USER, GITHUB_TOKEN). Call it AT ONCE when the user hands you such a "
        "value, once per value, as your first step - never put the value in a plan, a note, a "
        "memory entry or another tool's arguments first. It is then kept in the user's own encrypted "
        "store and removed from this conversation. From then on write `$VAF_SECRET_<NAME>` in a "
        "host_bash command or `os.environ[\"VAF_SECRET_<NAME>\"]` in python_exec code, and never "
        "repeat the value. Storing under an existing name replaces the old value."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Upper-case name for the value, e.g. FTP_PASS or GPORTAL_FTP_USER.",
            },
            "secret": {
                "type": "string",
                "description": "The value exactly as the user wrote it.",
            },
        },
        "required": ["name", "secret"],
    }

    def secret_placeholder(self, args: Dict[str, Any], arg: str) -> str:
        from vaf.core.user_secrets import env_name
        try:
            return env_name(args.get("name"))
        except ValueError:
            return "secret"

    def run(self, **kwargs) -> str:
        from vaf.core import user_secrets
        value = kwargs.get("secret")
        if not isinstance(value, str) or not value.strip():
            return "Error: `secret` is empty - pass the value exactly as the user wrote it."
        if len(value) < user_secrets.MIN_SCRUB_LENGTH:
            # Refused before anything is stored: this tool's promise is "kept, and gone from the
            # chat", and a value this short cannot be found in the chat without wrecking it.
            return (f"Error: the value is shorter than {user_secrets.MIN_SCRUB_LENGTH} characters, "
                    "too short to find and remove from this conversation, so it is NOT stored this "
                    "way. Do not keep it anywhere else; tell the user to enter it in Settings, "
                    "Connections, 'Credentials for commands'.")
        try:
            stored = user_secrets.set_secret(kwargs.get("name"), value,
                                             user_scope_id=kwargs.get("user_scope_id"),
                                             username=kwargs.get("username"))
        except ValueError as e:
            return f"Error: {e}."
        except Exception as e:
            # Measured live: with the store unreadable, the model kept the value in a plaintext
            # file in the chat's workspace "as a fallback". The refusal has to say what not to do.
            return (f"Error: the credential store could not be opened ({type(e).__name__}: {e}). "
                    "The value is NOT stored. Do not keep it anywhere else - no file, no note, no "
                    "memory entry: tell the user it could not be stored, and that it can be entered "
                    "in Settings, Connections, 'Credentials for commands' once the store works.")
        env = user_secrets.ENV_PREFIX + stored
        return (f"Stored as ${env}. The value is removed from this conversation. Use it by name "
                f"from now on (`${env}` in host_bash, `os.environ[\"{env}\"]` in python_exec) "
                "and never repeat it.")
