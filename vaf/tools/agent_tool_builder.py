"""
Agent Tool Builder
==================
Gives the main agent the ability to create, edit, and list its own Python tools
at runtime — without any human having to upload a file via the WebUI.

Security model
--------------
  - admin_only = True  → execute_tool() hard-blocks this tool for regular users.
    The agent can ONLY call create_agent_tool / edit_agent_tool during a session
    with an admin user.  In a normal user chat the policy check returns a
    "Security Error" before run() is ever reached.

  - permission_level = "system"  → skips the legacy confirmation gate.
    The admin has already elevated trust for this session; asking for
    confirmation on every tool-write would be disruptive.

  - Agent-created tools are saved with created_by="agent" and shared_with=[]
    (admin-only visibility) by default.  The admin must explicitly share them
    via the WebUI before regular users can see or use them.

  - The agent may ONLY edit/delete tools where created_by == "agent".
    It cannot touch built-in tools or tools the admin uploaded manually.
    Attempting to do so returns a clear error string.

_agent injection
----------------
Like python_sandbox and the coder tool, _agent is injected by execute_tool()
(see Step 5 in agent.py _load_tools) so run() can call reload_custom_tools()
after writing a file — making the new tool immediately available in the same
chat turn without a server restart.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from vaf.tools.base import BaseTool


class AgentToolBuilderTool(BaseTool):
    name        = "create_agent_tool"
    description = (
        "Create or update a Python tool that the agent can use immediately. "
        "Use this when you need a capability that no existing tool provides. "
        "The tool is saved and hot-reloaded so you can call it in the next turn. "
        "New tools are visible only to the admin until explicitly shared. "
        "You can also list or edit tools you have previously created."
    )

    # ── Contract ─────────────────────────────────────────────────────────────
    # admin_only: this tool is completely inaccessible in regular-user sessions.
    # The policy check in execute_tool() will block it before run() is reached.
    admin_only = True

    # system: skip the legacy confirmation gate — the admin session already
    # represents elevated trust, and prompting on every tool-write is annoying.
    permission_level = "system"

    side_effect_class    = "reversible"   # files can be deleted again
    channel_restrictions = ("telegram", "whatsapp", "discord")  # no tool creation from chat

    # ── Examples (shown to the LLM in the tool description) ──────────────────
    input_examples = [
        {
            "action":      "create",
            "tool_name":   "fetch_exchange_rate",
            "description": "Fetch the current EUR/USD exchange rate from a free API.",
            "code": (
                "from vaf.tools.base import BaseTool\n"
                "import requests\n\n"
                "class FetchExchangeRateTool(BaseTool):\n"
                "    name = 'fetch_exchange_rate'\n"
                "    description = 'Fetch current EUR/USD exchange rate.'\n"
                "    permission_level = 'read'\n"
                "    side_effect_class = 'none'\n"
                "    def run(self, **kwargs):\n"
                "        r = requests.get('https://api.exchangerate.host/latest?base=EUR&symbols=USD')\n"
                "        return str(r.json()['rates']['USD'])\n"
            ),
        },
        {
            "action": "list",
        },
        {
            "action":    "edit",
            "tool_name": "fetch_exchange_rate",
            "code":      "# ... updated source code ...",
        },
    ]

    # ── Parameters ────────────────────────────────────────────────────────────
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "delete", "list"],
                "description": (
                    "What to do:\n"
                    "  'create' — write a new tool (tool_name + code required).\n"
                    "  'edit'   — overwrite an existing agent-created tool (tool_name + code required).\n"
                    "  'delete' — remove an agent-created tool permanently (tool_name required).\n"
                    "  'list'   — return all tools you have previously created."
                ),
            },
            "tool_name": {
                "type": "string",
                "description": (
                    "Snake_case name of the tool, e.g. 'fetch_exchange_rate'. "
                    "Must match the `name` attribute inside the class. "
                    "Required for create / edit / delete."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One-sentence description of what the tool does. "
                    "Used for 'create' to document intent; ignored for edit/delete/list."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Full Python source code of the tool module. "
                    "Must contain exactly one class that inherits from BaseTool. "
                    "Required for create and edit."
                ),
            },
        },
        "required": ["action"],
    }

    # Injected by execute_tool() — gives access to reload_custom_tools()
    # so the new tool is live immediately without a server restart.
    _agent: Optional[Any] = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self, **kwargs) -> str:                          # noqa: C901  (acceptable complexity)
        from vaf.core.custom_tools_registry import (
            delete_tool,
            get_all_custom_tool_names,
            get_tool_manifest_entry,
            load_custom_tool_class,
            register_tool,
            save_tool_file,
            update_tool_source,
        )

        action    = (kwargs.get("action") or "").strip().lower()
        tool_name = (kwargs.get("tool_name") or "").strip()
        code      = (kwargs.get("code") or "").strip()

        # ── list ─────────────────────────────────────────────────────────────
        if action == "list":
            return self._list_agent_tools()

        # ── Validate tool_name for create / edit / delete ─────────────────
        if not tool_name:
            return "Error: 'tool_name' is required for action='%s'." % action

        if not re.match(r'^[a-z][a-z0-9_]*$', tool_name):
            return (
                "Error: tool_name must be lowercase snake_case "
                "(e.g. 'my_tool'). Got: '%s'." % tool_name
            )

        # ── create ───────────────────────────────────────────────────────────
        if action == "create":
            if not code:
                return "Error: 'code' is required for action='create'."

            # Prevent overwriting a tool that already exists — use edit instead.
            if tool_name in get_all_custom_tool_names():
                entry = get_tool_manifest_entry(tool_name)
                origin = entry.get("created_by", "?") if entry else "?"
                return (
                    f"Error: A tool named '{tool_name}' already exists "
                    f"(created_by='{origin}'). "
                    "Use action='edit' to update it, or choose a different name."
                )

            try:
                filename = f"{tool_name}.py"
                # save_tool_file writes the .py to disk
                save_tool_file(filename, code)
                # register_tool adds the manifest entry:
                #   created_by="agent", shared_with=[] (admin-only until approved)
                register_tool(
                    tool_name=tool_name,
                    filename=filename,
                    created_by="agent",
                    shared_with=[],     # admin must explicitly share this tool
                )
                # Validate the class was found (load_custom_tool_class returns
                # None + logs a warning if no BaseTool subclass exists in the file)
                cls = load_custom_tool_class(tool_name)
                if cls is None:
                    # Roll back: remove the broken entry so the registry stays clean
                    try:
                        delete_tool(tool_name)
                    except Exception:
                        pass
                    return (
                        "Error: The code was saved but no BaseTool subclass was found. "
                        "Make sure your class inherits from BaseTool and is not abstract. "
                        "The tool has been removed from the registry."
                    )
            except Exception as exc:
                return f"Error creating tool '{tool_name}': {exc}"

            self._hot_reload()
            return (
                f"Tool '{tool_name}' created successfully and is now available. "
                "It is currently visible to admins only. "
                "An admin can share it with other users via Settings → Tools."
            )

        # ── edit ─────────────────────────────────────────────────────────────
        if action == "edit":
            if not code:
                return "Error: 'code' is required for action='edit'."

            # Safety check: only edit tools the agent itself created.
            # This prevents the agent from overwriting admin-uploaded tools or
            # built-in tools that happen to share a name with a custom tool.
            error = self._assert_agent_owns(tool_name)
            if error:
                return error

            try:
                # update_tool_source validates the BaseTool subclass before writing
                update_tool_source(tool_name, code, updated_by="agent")
            except ValueError as exc:
                # Validation failed (no BaseTool subclass in new code)
                return f"Error: {exc}"
            except KeyError:
                return f"Error: Tool '{tool_name}' not found in registry."
            except Exception as exc:
                return f"Error updating tool '{tool_name}': {exc}"

            self._hot_reload()
            return f"Tool '{tool_name}' updated successfully and is now live."

        # ── delete ───────────────────────────────────────────────────────────
        if action == "delete":
            error = self._assert_agent_owns(tool_name)
            if error:
                return error

            try:
                delete_tool(tool_name)
            except FileNotFoundError:
                return f"Error: Tool '{tool_name}' not found in registry."
            except Exception as exc:
                return f"Error deleting tool '{tool_name}': {exc}"

            self._hot_reload()
            return f"Tool '{tool_name}' deleted successfully."

        return f"Error: Unknown action '{action}'. Valid values: create, edit, delete, list."

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _list_agent_tools(self) -> str:
        """Return a formatted list of tools the agent has created."""
        from vaf.core.custom_tools_registry import load_manifest

        manifest = load_manifest()
        agent_tools = [
            (name, entry)
            for name, entry in manifest.get("tools", {}).items()
            if entry.get("created_by") == "agent"
        ]

        if not agent_tools:
            return "You have not created any custom tools yet."

        lines = ["Agent-created tools:"]
        for name, entry in agent_tools:
            shared = entry.get("shared_with", [])
            visibility = "all users" if "*" in shared else (
                f"{len(shared)} user(s)" if shared else "admin only"
            )
            lines.append(f"  • {name}  [visibility: {visibility}]")
        return "\n".join(lines)

    def _assert_agent_owns(self, tool_name: str) -> Optional[str]:
        """
        Return an error string if the tool does not exist or was not created
        by the agent.  Returns None when the ownership check passes.

        This prevents the agent from editing admin-uploaded tools or built-in
        tools that share a name with an entry in the custom tools registry.
        """
        from vaf.core.custom_tools_registry import get_tool_manifest_entry

        entry = get_tool_manifest_entry(tool_name)
        if entry is None:
            return (
                f"Error: Tool '{tool_name}' not found in the custom tools registry. "
                "You can only edit or delete tools you have created yourself. "
                "Use action='list' to see your tools."
            )
        if entry.get("created_by") != "agent":
            return (
                f"Error: Tool '{tool_name}' was created by '{entry.get('created_by', '?')}', "
                "not by you. You may only edit or delete your own tools."
            )
        return None

    def _hot_reload(self) -> None:
        """
        Ask the agent to reload all custom tools so the change is live
        immediately — no server restart needed.
        Does nothing if _agent was not injected (e.g. in tests).
        """
        agent = self._agent
        if agent is not None and hasattr(agent, "reload_custom_tools"):
            try:
                agent.reload_custom_tools()
            except Exception as exc:
                # Non-fatal: the file was written correctly, the agent will
                # pick it up on the next full restart even if hot-reload fails.
                import logging
                logging.getLogger(__name__).warning(
                    "agent_tool_builder: hot-reload failed: %s", exc
                )
