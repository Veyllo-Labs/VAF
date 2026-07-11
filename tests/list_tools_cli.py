#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Print the tool list as the CLI "All Available Tools" menu would show it.
Run from VAF root: python tests/list_tools_cli.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOLS_HIDDEN_FROM_CLI = frozenset({"update_intent"})
CODER_SUBAGENT_TOOLS = [
    ("read_file", "Read a file's contents", "Coder Sub-Agent"),
    ("list_files", "List files in directory", "Coder Sub-Agent"),
    ("bash", "Execute shell commands (build, test, git)", "Coder Sub-Agent"),
    ("codesearch", "Search for code patterns/symbols", "Coder Sub-Agent"),
    ("batch", "Execute multiple tools in parallel", "Coder Sub-Agent"),
]


def main():
    from vaf.core.agent import Agent
    agent = Agent(verbose=False)
    print("=== Main Agent tools (update_intent excluded) ===\n")
    for name, tool in sorted(agent.tools.items()):
        if name in TOOLS_HIDDEN_FROM_CLI:
            continue
        t_type = "Main Agent"
        tstr = str(type(tool))
        if "CodingAgent" in tstr or "Librarian" in tstr:
            t_type = "Sub-Agent Delegator"
        if "WebSearch" in tstr:
            t_type = "Main Agent (Research)"
        if "WebFetch" in tstr:
            t_type = "Main Agent (Research)"
        desc = (tool.description[:55] + "...") if len(tool.description) > 55 else tool.description
        print(f"  {name:<28} | {desc:<56} | {t_type}")
    print("\n=== Coder Sub-Agent only (not given to Main Agent) ===\n")
    for name, desc, available_to in CODER_SUBAGENT_TOOLS:
        print(f"  {name:<28} | {desc:<56} | {available_to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
