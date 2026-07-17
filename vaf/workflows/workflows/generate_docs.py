# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Generate Documentation Workflow

Analyze a codebase and generate documentation.
"""

WORKFLOW = {
    "name": "Generate Documentation",
    "description": "Analyze a codebase and generate documentation",
    "triggers": [
        "erstelle dokumentation",
        "generate documentation",
        "dokumentiere dieses projekt",
        "document this project",
        "erstelle readme",
        "create readme",
    ],
    "trigger_patterns": [
        r"erstell.*doku",
        r"generat.*doc",
        r"dokumentier.*projekt",
        r"create.*readme",
    ],
    "variables": {
        "path": "Path to the project/file to document",
    },
    "defaults": {
        "output": "DOCUMENTATION.md",
    },
    "steps": [
        {
            "tool": "list_files",
            "input": "{path}",
            "output": "file_list",
            "description": "List project files",
        },
        {
            "tool": "librarian_agent",
            "input": (
                "Create a comprehensive DOCUMENTATION.md for this project.\n"
                "Include: overview, key modules/files, how to run, common commands, and any important notes.\n"
                "Be concise but complete.\n\n"
                "Project file list:\n{file_list}\n"
            ),
            "output": "documentation",
            "description": "Generate documentation",
        },
        {
            "tool": "write_file",
            "args": {
                "path": "DOCUMENTATION.md",
                "content": "{documentation}",
            },
            "input": "DOCUMENTATION.md",
            "output": "saved",
            "description": "Save documentation",
        },
        # NOTE: no librarian "completion message" step - see research_and_code:
        # the filesystem agent misreads that prompt as a file search and its
        # garbage becomes the workflow's final output. The save result is the
        # honest completion message.
    ],
}

