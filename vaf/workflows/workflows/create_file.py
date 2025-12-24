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
            "input": "Create: {description}\nFilename: {filename}",
            "output": "content",
            "description": "Generate file content",
        },
        {
            "tool": "write_file",
            "input": '{"path": "{filename}", "content": "{content}"}',
            "output": "saved",
            "description": "Save the file",
        },
    ],
}

