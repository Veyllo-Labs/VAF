"""
Create Scheduled Task Workflow

Create a scheduled automation task that runs at specified times.
Handles multi-step tasks like weather reports, news summaries, etc.
Intelligently creates prompts that instruct the agent to use multiple tools (web_search, coding_agent, etc.)
"""

WORKFLOW = {
    "name": "Create Scheduled Task",
    "description": "Create a scheduled automation task that runs at specified times with multi-step content generation",
    "triggers": [
        "erstelle automatisierung",
        "create automation",
        "schedule task",
        "täglich um",
        "daily at",
        "immer um",
        "every day at",
        "automatisch um",
        "automatic at",
        "scheduled",
        "zeitplan",
    ],
    "trigger_patterns": [
        r"erstell.*automatisier",
        r"create.*automation",
        r"schedule.*task",
        r"täglich.*um",
        r"daily.*at",
        r"immer.*um",
        r"every.*day.*at",
        r"automatisch.*um",
    ],
    "variables": {
        "task_description": "What the automation should do (e.g., 'weather summary for Berlin tomorrow')",
        "time": "Time to run (HH:MM format, e.g., '21:07')",
        "frequency": "How often (daily, weekly, hourly, monthly)",
        "output_path": "Where to save results (e.g., 'Desktop', 'Documents')",
        "format": "Output format (html, markdown, txt)",
    },
    "defaults": {
        "frequency": "daily",
        "output_path": "Desktop",
        "format": "html",
    },
    "steps": [
        {
            "tool": "create_automation",
            "args": {
                "name": "{task_description}",
                "prompt": (
                    "You are running as an automation.\n"
                    "Goal: {task_description}\n\n"
                    "MANDATORY STEPS:\n"
                    "1) Use web_search to fetch up-to-date information needed for the goal.\n"
                    "2) Create a clean output file in {format} format.\n"
                    "   - If HTML: produce a complete HTML document with embedded CSS.\n"
                    "   - If Markdown: produce a well-structured .md report.\n"
                    "   - If txt: produce readable plain text.\n"
                    "3) Save the file to {output_path}.\n\n"
                    "OUTPUT REQUIREMENTS:\n"
                    "- Include sources (URLs) at the bottom.\n"
                    "- Use a timestamp in the content.\n"
                    "- Keep it concise and user-friendly.\n"
                ),
                "frequency": "{frequency}",
                "time": "{time}",
                "output_path": "{output_path}",
            },
            "input": "{task_description}",
            "output": "automation_result",
            "description": "Create the scheduled automation task with multi-tool prompt",
        },
        {
            "tool": "librarian_agent",
            "input": (
                "Create a short confirmation message for the user.\n"
                "Include schedule, output location, and how to list/run automations.\n\n"
                "{automation_result}\n"
            ),
            "output": "final",
            "description": "Return a helpful confirmation message",
        },
    ],
}

