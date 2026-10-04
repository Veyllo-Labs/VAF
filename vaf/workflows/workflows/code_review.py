# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Code Improvement Workflow (id `code_review`, kept so stored references still resolve)

Read a file, rewrite it improved and save it. It CHANGES the file, so it answers only to
"improve" and "optimize". A request to review or check code is a request for findings, and
goes to the code_audit tool, which reads, reports and asks before anything is changed.
"""

WORKFLOW = {
    "name": "Code Improvement",
    "description": "Rewrite a file improved and save it (to only review code, use code_audit)",
    "triggers": [
        "verbessere diese datei",
        "improve this file",
        "optimiere den code",
        "optimize the code",
        "prüfe und verbessere",
    ],
    "trigger_patterns": [
        r"verbess.*code",
        r"improv.*file",
        r"optimi.*code",
    ],
    "variables": {
        "path": "Path to the file to review",
    },
    "steps": [
        {
            "tool": "read_file",
            "input": "{path}",
            "output": "original_code",
            "description": "Read the original file",
        },
        {
            "tool": "coding_agent",
            "input": (
                "CONTENT_ONLY: Improve this code and return ONLY the improved file content.\n"
                "- Fix bugs\n"
                "- Improve readability\n"
                "- Add/adjust comments where helpful\n"
                "- Keep behavior unless a bug fix requires a change\n"
                "- Return ONLY the improved file content (no Markdown fences, no explanation, no project structure)\n\n"
                "{original_code}\n"
            ),
            "output": "improved_code",
            "description": "Review and improve the code",
        },
        {
            "tool": "write_file",
            "args": {
                "path": "{path}",
                "content": "{improved_code}",
            },
            "input": "{path}",
            "output": "saved",
            "description": "Save the improved code",
        },
        {
            "tool": "librarian_agent",
            "input": (
                "Write a short change summary for the user.\n"
                "Include: key improvements, any behavior changes, and where it was saved.\n\n"
                "File: {path}\n"
                "Save result: {saved}\n"
            ),
            "output": "final",
            "description": "Summarize changes",
        },
    ],
}

