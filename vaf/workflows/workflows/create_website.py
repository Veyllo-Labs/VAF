# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Create Website Workflow

Generate a complete, responsive website with HTML, CSS, and JavaScript.
"""

WORKFLOW = {
    "name": "Create Website",
    "description": "Generate a complete, responsive website with HTML, CSS, and JavaScript",
    "triggers": [
        # German - verschiedene Varianten
        "erstelle website", "erstelle webseite", 
        "erstelle eine website", "erstelle eine webseite",
        "website erstellen", "webseite erstellen", 
        "erstelle homepage", "homepage erstellen",
        "website für", "webseite für",
        "baue eine website", "baue website",
        "mache eine website", "mach mir eine website",
        "generiere website", "generiere webseite",
        # English
        "create website", "create a website", "build website",
        "make website", "make a website", "generate website",
        "website for", "build a website for",
        "create homepage", "build homepage",
        "create landing page", "landing page for",
        # Do NOT add single-word triggers like "website"/"webseite" – they match mentions (e.g. "ich habe eine Webseite auf wix") and wrongly start website creation. Only creation-intent phrases above.
    ],
    "trigger_patterns": [
        r"erstell.*websit",
        r"erstell.*webseite",
        r"erstell.*homepage",
        r"websit.*erstell",
        r"websit.*für",
        r"webseite.*für",
        r"homepage.*erstell",
        r"bau.*websit",
        r"mach.*websit",
        r"create.*websit",
        r"build.*websit",
        r"make.*websit",
        r"landing.*page",
        r"website.*handwerker",
        r"handwerker.*website",
    ],
    "variables": {
        "description": "Description of the website to create",
    },
    "steps": [
        {
            "tool": "coding_agent",
            "input": (
                "Create a complete, professional website for: {description}\n\n"
                "Requirements:\n"
                "- Modern, responsive design (works on mobile, tablet, desktop)\n"
                "- Real content (no lorem ipsum - use relevant placeholder text)\n"
                "- Professional styling with CSS\n"
                "- Basic interactivity with JavaScript if appropriate\n"
            ),
            "output": "result",
            "description": "Create complete website with HTML, CSS, JS",
        },
    ],
}

