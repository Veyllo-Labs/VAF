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

def main():
    # Hidden/coder-only policy and the bundle order come from the shared
    # catalog. This file used to keep byte-identical copies of both, which is
    # the drift CLAUDE Rule 2 forbids - and the copies were already stale.
    from vaf.cli.tool_catalog import describe_tools, group_tools
    from vaf.core.agent import Agent
    from vaf.core.tool_contract import category_label

    agent = Agent(verbose=False)
    for category, rows in group_tools(describe_tools(agent)):
        print(f"\n=== {category_label(category)} ({len(rows)}) ===\n")
        for row in rows:
            print(f"  {row.name:<28} | {row.description:<58} | {row.audience}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
