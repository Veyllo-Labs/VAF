# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Create File Workflow

Generate and create a new file.
"""

WORKFLOW = {
    "name": "Create File",
    "description": "Generate and create a new file",
    "triggers": [
        "erstelle datei",
        "create file",
        "schreibe eine datei",
        "write a file",
        "neue datei",
        "new file",
    ],
    "trigger_patterns": [
        r"erstell.*datei",
        r"create.*file",
        r"schreib.*datei",
        r"neue.*datei",
    ],
    "variables": {
        "description": "What the file should contain",
        "filename": "Name of the file to create",
    },
    "steps": [
        {
            "tool": "coding_agent",
            "input": (
                "CONTENT_ONLY: Generate ONLY the complete file content.\n"
                "Return ONLY the file content (no Markdown fences, no explanations, no project structure).\n\n"
                "Filename: {filename}\n"
                "Requirements: {description}\n"
            ),
            "output": "content",
            "description": "Generate file content",
        },
        {
            "tool": "write_file",
            "args": {
                "path": "{filename}",
                "content": "{content}",
            },
            "input": "{filename}",
            "output": "saved",
            "description": "Save the file",
        },
        # NOTE: no librarian "completion message" step - see research_and_code:
        # the filesystem agent misreads that prompt as a file search and its
        # garbage becomes the workflow's final output. The save result is the
        # honest completion message.
    ],
}

