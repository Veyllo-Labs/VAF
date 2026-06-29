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
    },
    "defaults": {
        "frequency": "daily",
    },
    # NOTE: This prompt is deliberately file-NEUTRAL. It becomes create_automation's `prompt`, which
    # create_automation scans both for (a) whether to pre-generate a multi-tool workflow and (b)
    # whether the user wants a saved FILE vs a chat/messenger-only result. Naming tools/formats here
    # (web_search/html/save/datei/…) would force BOTH on every scheduled task, re-introducing the
    # slow workflow-gen for simple "tell me X daily" tasks AND always writing a file. Keeping it
    # neutral lets only the embedded {task_description} carry the user's real intent. output_path/
    # format are intentionally NOT passed: create_automation derives them from {task_description}
    # (no file is written unless the user actually asked for one).
    "steps": [
        {
            "tool": "create_automation",
            "args": {
                "name": "{task_description}",
                "prompt": (
                    "You are running as a scheduled automation. Task: {task_description}\n\n"
                    "Carry out the task now and return a clear, concise, well-structured result. "
                    "Gather any information you need first, and cite any sources you used. "
                    "Produce a document only if the task itself explicitly requests one; otherwise "
                    "return the result as text — it is delivered to the user automatically."
                ),
                "frequency": "{frequency}",
                "time": "{time}",
            },
            "input": "{task_description}",
            "output": "final",
            "description": "Create the scheduled automation task",
        },
    ],
}

