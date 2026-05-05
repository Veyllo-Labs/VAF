"""
Code Review Workflow

Read a file, review it, and save improvements.
"""

WORKFLOW = {
    "name": "Code Review",
    "description": "Read a file, review it, and save improvements",
    "triggers": [
        "review diesen code",
        "review this code",
        "verbessere diese datei",
        "improve this file",
        "optimiere den code",
        "optimize the code",
        "prüfe und verbessere",
    ],
    "trigger_patterns": [
        r"review.*code",
        r"review.*datei",
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

