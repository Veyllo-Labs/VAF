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
        {
            "tool": "librarian_agent",
            "input": (
                "Create a short user-facing completion message.\n"
                "Include: what was created, where it was saved, and how to use it (if applicable).\n\n"
                "Filename: {filename}\n"
                "Write result: {saved}\n"
            ),
            "output": "final",
            "description": "Return a helpful completion message",
        },
    ],
}

