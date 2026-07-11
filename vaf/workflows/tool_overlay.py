# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared tool set for workflow runners (single source, Rule 2).

Three places used to hand-maintain their own copy of "the tools a workflow may
call": the in-chat executor (vaf/tools/workflow_executor.py), the @workflow CLI
subprocess (vaf/cli/cmd/workflow.py) and the run_temp overlay in agent.py. The
copies drifted: the CLI subprocess lacked python_sandbox, so the youtube_summary
template failed its first step with "Tool not found" when invoked via
@youtube_summary while working fine through execute_workflow (live incident,
session yellow305153).

workflow_primitives() is the ONE list now. It must cover every tool any
BUILT-IN template step names (tests/test_workflow_tool_overlay.py enforces
exactly that), because the CLI subprocess has no live agent registry to fall
back on. Runners with an agent overlay the live registry ON TOP of this.
"""
from __future__ import annotations

import importlib
from typing import Dict

# (module, class, tool name) - constructible without an agent or session.
_PRIMITIVES = [
    ("vaf.tools.search",          "WebSearchTool",      "web_search"),
    ("vaf.tools.webfetch",        "WebFetchTool",       "webfetch"),
    ("vaf.tools.filesystem",      "WriteFileTool",      "write_file"),
    ("vaf.tools.filesystem",      "ReadFileTool",       "read_file"),
    ("vaf.tools.filesystem",      "ListFilesTool",      "list_files"),
    ("vaf.tools.filesystem",      "MoveFileTool",       "move_file"),
    ("vaf.tools.bash",            "BashTool",           "bash"),
    ("vaf.tools.python_sandbox",  "PythonSandboxTool",  "python_sandbox"),
    ("vaf.tools.coder",           "CodingAgentTool",    "coding_agent"),
    ("vaf.tools.librarian",       "LibrarianTool",      "librarian_agent"),
    ("vaf.tools.research_agent",  "ResearchAgentTool",  "research_agent"),
    ("vaf.tools.document_agent",  "DocumentAgentTool",  "document_agent"),
    ("vaf.tools.document_writer", "DocumentWriterTool", "document_writer"),
    ("vaf.tools.report_filename", "ReportFilenameTool", "report_filename"),
    ("vaf.tools.repair_report",   "RepairReportTool",   "repair_report"),
    ("vaf.tools.automation",      "AutomationTool",     "create_automation"),
    ("vaf.tools.list_tools",      "ListToolsTool",      "list_tools"),
]


def workflow_primitives() -> Dict[str, object]:
    """Instantiate the workflow primitive tools. Import failures are skipped
    (optional integrations may be absent); never raises."""
    tools: Dict[str, object] = {}
    for module_path, class_name, tool_name in _PRIMITIVES:
        try:
            mod = importlib.import_module(module_path)
            tools[tool_name] = getattr(mod, class_name)()
        except Exception:
            pass
    return tools
