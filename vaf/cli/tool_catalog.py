# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the tool list SAYS, separated from how a lane draws it.

The classic settings menu held two pieces of policy as function-local
constants: which tools are hidden from a human-facing list, and the reference
rows for tools only the coder sub-agent gets. A second lane wanting the same
list would have had to copy them - and a copied POLICY constant is the shape
CLAUDE.md's Rule 2 forbids outright, because the copies drift silently and the
drift is invisible in a diff.

So the data lives here and the renderers stay dumb: the classic menu prints a
Rich table, the terminal app fills an overlay, both from `describe_tools()`.

Import-light on purpose (no textual, no Rich, no agent import).
"""
from dataclasses import dataclass
from typing import List, Tuple

# Internal plumbing the model calls but a human list should not advertise.
TOOLS_HIDDEN_FROM_CLI = frozenset({"update_intent"})

# Registered to the coder sub-agent only, listed for reference so the catalog
# is not silently incomplete. Note read_file/list_files/write_file ARE on the
# main agent as well and are therefore not listed as coder-only here.
CODER_SUBAGENT_TOOLS = (
    ("read_file", "Read a file's contents", "files"),
    ("list_files", "List files in directory", "files"),
    ("bash", "Execute shell commands (build, test, git)", "code"),
    ("codesearch", "Search for code patterns/symbols", "code"),
)

_MAIN = "Main Agent"
_RESEARCH = "Main Agent (Research)"
_DELEGATOR = "Sub-Agent Delegator"
_CODER_ONLY = "Coder Sub-Agent"

DESCRIPTION_CHARS = 55


@dataclass(frozen=True)
class ToolRow:
    name: str
    description: str
    audience: str
    coder_only: bool = False
    category: str = "general"


def _audience(tool) -> str:
    kind = str(type(tool))
    if "CodingAgent" in kind or "Librarian" in kind:
        return _DELEGATOR
    if "WebSearch" in kind or "WebFetch" in kind:
        return _RESEARCH
    return _MAIN


def describe_tools(agent) -> List[ToolRow]:
    """Every tool a human should see, grouped by bundle, bundles in display
    order and tools alphabetical inside each.

    Plain alphabetical order was actively hostile at this size: over 117 names
    the five WhatsApp tools spanned 84 positions and the four Telegram tools
    70, in a 26-row overlay with no search box. Grouping is the whole point of
    the `category` declaration, and both renderers get it for free.

    Returns data, never prints - the classic menu's own version blocked on
    `console.input()` at the end, which no full-screen lane can call.
    """
    from vaf.core.tool_contract import TOOL_CATEGORIES, tool_category

    order = {key: index for index, key in enumerate(TOOL_CATEGORIES)}
    unknown_at = len(order)          # a bundle the framework does not know goes last

    rows: List[ToolRow] = []
    for name, tool in getattr(agent, "tools", {}).items():
        if name in TOOLS_HIDDEN_FROM_CLI:
            continue
        text = str(getattr(tool, "description", "") or "")
        if len(text) > DESCRIPTION_CHARS:
            text = text[:DESCRIPTION_CHARS] + "..."
        rows.append(ToolRow(name=name, description=text, audience=_audience(tool),
                            category=tool_category(name, tool)))

    listed = {r.name for r in rows}
    for name, text, category in CODER_SUBAGENT_TOOLS:
        if name in listed:
            continue
        rows.append(ToolRow(name=name, description=text, audience=_CODER_ONLY,
                            coder_only=True, category=category))

    rows.sort(key=lambda r: (order.get(r.category, unknown_at), r.category, r.name))
    return rows


def group_tools(rows: List[ToolRow]) -> List[Tuple[str, List[ToolRow]]]:
    """(bundle key, its rows) in the order describe_tools() already produced, so
    a renderer only has to draw a heading and never has to know the order."""
    groups: List[Tuple[str, List[ToolRow]]] = []
    for row in rows:
        if not groups or groups[-1][0] != row.category:
            groups.append((row.category, []))
        groups[-1][1].append(row)
    return groups
