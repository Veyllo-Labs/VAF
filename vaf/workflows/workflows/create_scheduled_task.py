# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
        "scheduled task",
        "zeitplan erstellen",
        "jeden tag um",
        "täglich generieren",
        "daily generate",
    ],
    "trigger_patterns": [
        r"erstell.*automatisier",
        r"create.*automation",
        r"schedule.*task",
        r"täglich.*um.*\d{1,2}:\d{2}",  # Requires time pattern
        r"daily.*at.*\d{1,2}:\d{2}",     # Requires time pattern
        r"immer.*um.*\d{1,2}:\d{2}",     # Requires time pattern
        r"every.*day.*at.*\d{1,2}",      # Requires time
        r"automatisch.*um.*\d{1,2}",     # Requires time
        r"jeden.*tag.*um.*\d{1,2}",      # Requires time
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
                    "You are running as an automation. Your task: {task_description}\n\n"
                    "EXECUTE THESE STEPS IN ORDER:\n"
                    "1) Use web_search to get any needed information (weather, quotes, news, data, etc.)\n"
                    "2) Generate the content as specified in the task description\n"
                    "3) Create a complete {format} file with the content:\n"
                    "   - HTML: Full HTML document with <!DOCTYPE html>, <head>, <body>, embedded CSS styling\n"
                    "   - Markdown: Well-structured .md with headers, sections, proper formatting\n"
                    "   - txt: Clean plain text with proper line breaks and formatting\n"
                    "4) Save the file to {output_path} with a descriptive filename\n\n"
                    "IMPORTANT REQUIREMENTS:\n"
                    "- Include timestamps in the content (when generated, when data is for)\n"
                    "- If weather: Include location, date, temperature, conditions, forecast\n"
                    "- If quotes: Include the quote text and source if available\n"
                    "- Make it visually appealing (especially for HTML - use CSS for styling)\n"
                    "- Save with a descriptive filename (include date if needed, e.g., weather_berlin_2025-12-25.html)\n"
                    "- Include sources/URLs at the bottom if you used web_search\n"
                    "- Keep content concise but complete\n"
                ),
                "frequency": "{frequency}",
                "time": "{time}",
                "output_path": "{output_path}",
            },
            "input": "{task_description}",
            "output": "final",
            "description": "Create the scheduled automation task with multi-tool prompt",
        },
    ],
}

