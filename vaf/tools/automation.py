# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Automation Tool - Allow the model to create scheduled tasks
"""
import json
import re
from typing import List, Dict, Any, Optional, Tuple
from vaf.tools.base import BaseTool
from vaf.core.automation import (
    AutomationClarifier,
    AutomationManager,
    AutomationTask,
    format_daily_calendar_status,
)


def _extract_first_json_array(text: str) -> Optional[List[Any]]:
    """Extract the first VALID top-level JSON array from a model response.

    The model often wraps the workflow array in prose or markdown, or emits trailing text/a second array.
    A greedy ``re.search(r'\\[.*\\]')`` then grabs from the first ``[`` to the LAST ``]`` and json.loads
    fails with 'Extra data ...' — which used to route the run into the (previously unbounded) prompt-based
    fallback. This scans bracket-balanced candidates and returns the first one that parses to a list, or None.
    """
    if not text:
        return None
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start:i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, list):
                            return parsed
                    except json.JSONDecodeError:
                        pass
                    start = -1
    return None


def _manager_for_scope(user_scope_id: Optional[str], user_role: Optional[str] = None) -> Tuple[AutomationManager, Optional[str]]:
    """Return (manager, effective_scope) for user-scoped automation access.

    Local admin scope → global AutomationManager (no user_scope_id).
    Other scopes → user-scoped manager.
    """
    from vaf.core.config import get_local_admin_scope_id
    local = get_local_admin_scope_id()

    # If no scope provided or it's the primary local admin, use the global root manager
    if not user_scope_id or str(user_scope_id).strip() == str(local).strip():
        return AutomationManager(), None

    # Role-based admin fallback: allow aggregated admin access even if scope differs.
    if (user_role or "").strip().lower() == "admin":
        return AutomationManager(), user_scope_id

    # Regular users always use their own isolated scope
    return AutomationManager(user_scope_id=user_scope_id), user_scope_id


class AutomationTool(BaseTool):
    """Tool for creating and managing automated tasks."""
    
    name = "create_automation"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Create a scheduled automation task that runs a prompt at a specific clock time. Use frequency='once' for a one-time task or reminder at a given clock time (runs exactly once, then self-deletes). Use frequency='daily'/'weekly'/'monthly'/'hourly' only for recurring schedules. For a SHORT relative delay that should fire proactively in the live chat (e.g. 'in 1 minute', 'in 90 seconds'), use the set_timer tool instead — not this one.
Use this when user wants to schedule recurring tasks or a one-time task at a clock time:
- Once (einmalig): single run at a clock time, automatically deleted after execution. Use for a one-time task or reminder scheduled to a specific time (e.g. 'Remind me tomorrow at 10:00', 'Run this script tonight at 23:00'). For 'in N seconds/minutes' use set_timer instead.
- Daily (täglich), weekly (wöchentlich, use weekday e.g. monday), monthly (monatlich, use day 1-31), hourly (stündlich)
- Daily news/weather reports, periodic backups, scheduled reminders, regular document generation

**CRITICAL RULES - READ CAREFULLY:**

1. **MANDATORY: Check for existing automations FIRST**
   - ALWAYS call `list_automations` FIRST before creating
   - If similar automation exists, use `read_automation` + `update_automation` instead
   - Automation files are stored in: ~/.vaf/automations/*.json (or Platform.vaf_dir() / "automations")

2. **MANDATORY: Ask for missing information BEFORE calling this tool**
   - This is the ONLY time you can get user input - once created, automation runs unattended!
   - If user says "weather report" → YOU MUST ASK (in user's language):
     * "Which city?" (DE: "Für welche Stadt?")
     * "What time?" (DE: "Zu welcher Uhrzeit?")
   - If user says "Schedule task" → ASK (in user's language): "What should the task do? When should it run?"
   - DO NOT call this tool with missing critical information (city, time, task description, etc.)
   - DO NOT assume defaults for city, time, or other task-specific parameters
   - ONLY use defaults for output_path (use "Documents" if not specified)
   - **NEVER ask "Where should output be saved?" - just use "Documents" automatically!**

3. **ONLY call this tool when:**
   - You have ALL required information (city, time, task description, etc.)
   - OR you've already asked the user and they provided it
   - OR the user explicitly provided all information in their request

**If information is missing → ASK FIRST in user's language, then call the tool!**

**IF USER GIVES FEEDBACK ABOUT AN EXISTING AUTOMATION:**
- Use `list_automations` to find the automation ID
- Use `read_automation` to see the full details
- UPDATE the existing automation instead of creating a new one
- Inform user: "I'll update the existing automation [name] instead of creating a new one"

3b. **FREQUENCY — CRITICAL: NEVER assume a recurring schedule!**
   - Set `frequency='once'` for a one-time task scheduled to a clock time (reminders, "do this tomorrow", "run this at 10:00", etc.). For a short relative delay that fires in the current chat ("in N seconds/minutes"), use set_timer instead.
   - `frequency='once'` means: run **exactly once** and then the automation is automatically deleted. **No repeat.**
   - Only set `frequency` to `daily` / `weekly` / `monthly` / `hourly` when the user has **EXPLICITLY** asked for a recurring schedule (e.g. "every day", "every Monday", "täglich", "wöchentlich").
   - If the user says "remind me tomorrow at 10" → `frequency='once'`, `time='10:00'`. **No delay, no repeat.**
   - **NEVER add a delay** unless the user explicitly asks to run something in the future. If `time` is in the future today, the system schedules it automatically.
   - Summary: `once` = one run, no repeat | `daily`/`weekly`/etc. = only when user EXPLICITLY requested recurrence.

4. **PROMPT CONTENT (when the automation sends something to the user):**
   - Never hardcode a messenger (e.g. send_telegram). The user may use WhatsApp, Discord, email, etc.
   - In the prompt, instruct: "Send the result/summary to the user with **send_to_user** (it resolves their main_messenger at run time and falls back to the Web UI). Use a platform tool (send_telegram, send_whatsapp, send_discord, send_mail, ...) ONLY if the user explicitly named that platform."
   - For "weekly review", "wöchentlicher Report", or similar: use frequency **weekly** and set **weekday** (e.g. friday). Do not use daily.
   - Do not assume git or version control. If the prompt mentions "recent changes" or "commits", phrase it as: "If the project uses version control (e.g. git), check recent commits with git_log; otherwise skip or use file/context."

5. **TIME SLOTS (HH:MM, 10-minute rule):**
   - Always use **HH:MM** for the time parameter (e.g. 06:00, 06:15, 18:30).
   - No two automations may be scheduled within 10 minutes of each other. The system will return an error if the chosen time is too close to an existing automation; then choose a different time (e.g. 06:10 if 06:00 is taken).

6. **Same-day status (`list_automations` / `read_automation`):**
   - Each automation shows **Today (local)**: Done (today), Scheduled (later today), Due (not yet run today), or In progress.
   - If you create a daily automation whose clock time already passed today, the system normally runs it once immediately—but **not** if another automation in the same family (same name, or both look like "briefing"/"Morgenbriefing") **already ran today**; then catch-up is skipped and the tool message explains why. """
    
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name for the automation (e.g., 'daily_news', 'weather_report')"
            },
            "prompt": {
                "type": "string",
                "description": "The full prompt/task to execute (e.g., 'Create a summary of today\\'s tech news')"
            },
            "frequency": {
                "type": "string",
                "enum": ["once", "hourly", "daily", "weekly", "monthly"],
                "description": (
                    "How often to run. REQUIRED — you MUST ask the user explicitly if not stated.\n"
                    "- 'once': ONE-TIME scheduled task — runs exactly once at the given clock time, then automatically deleted. "
                    "Use for a single reminder or task scheduled to a specific time ('remind me tomorrow at 10', "
                    "'run this at 22:00 tonight', 'send report next Friday at 09:00 once'). For a short relative delay that fires in the live chat ('in N seconds/minutes'), use set_timer instead. DEFAULT — use unless user explicitly wants repetition.\n"
                    "- 'daily': every day at the given time\n"
                    "- 'weekly': every week on weekday (requires weekday param)\n"
                    "- 'monthly': every month on day (requires day param)\n"
                    "- 'hourly': every hour at :MM\n"
                    "NEVER default to 'daily'. If unclear, ask the user."
                )
            },
            "time": {
                "type": "string",
                "description": (
                    "Time to run in HH:MM format (e.g., '07:00', '18:30'). "
                    "REQUIRED — ask the user what time they want if not stated. "
                    "Must be at least 10 minutes apart from existing automations."
                )
            },
            "weekday": {
                "type": "string",
                "description": "For frequency 'weekly': which weekday (e.g. 'monday', 'tuesday'). Use English lowercase."
            },
            "day": {
                "type": "integer",
                "description": "For frequency 'monthly': day of month (1-31)"
            },
            "output_path": {
                "type": "string",
                "description": "Where to save results (e.g., 'Documents', 'Desktop', 'Downloads'). Default: 'Documents' if not specified."
            },
            "parameters": {
                "type": "object",
                "description": "Additional parameters like city for weather, category for news"
            },
            "max_retries": {
                "type": "integer",
                "description": "How many times to retry on failure (0 = no retry). Only set if user explicitly requests retry behavior.",
                "minimum": 0,
                "maximum": 5
            },
            "retry_delay_minutes": {
                "type": "integer",
                "description": "Minutes to wait between retries. Only relevant if max_retries > 0. Only set if user explicitly requests it.",
                "minimum": 1
            },
            "confirm_duplicate": {
                "type": "boolean",
                "description": "Set true ONLY after the user explicitly confirmed they want a SECOND, near-identical automation at a different time than one that already exists. Leave false/unset normally — if a similar automation exists, the tool will tell you to ask the user first instead of creating a duplicate."
            }
        },
        "required": ["name", "prompt", "frequency", "time"]
    }
    
    def run(self, **kwargs) -> str:
        name = kwargs.get("name", "untitled")
        prompt = kwargs.get("prompt", "")
        frequency = (kwargs.get("frequency") or "").lower().strip()
        schedule_time = kwargs.get("time", "06:00")
        # Explicit user confirmation to create a SECOND near-identical automation at a different time.
        # When False (default), a near-duplicate at a different time is NOT created — the tool returns a
        # truthful "nothing created, ask the user" prompt instead of silently creating (and lying about it).
        confirm_duplicate = bool(kwargs.get("confirm_duplicate", False))
        weekday_arg = kwargs.get("weekday")
        day_arg = kwargs.get("day")
        # Normalize weekday to English lowercase (e.g. Montag -> monday)
        weekday_map = {
            "montag": "monday", "dienstag": "tuesday", "mittwoch": "wednesday",
            "donnerstag": "thursday", "freitag": "friday", "samstag": "saturday", "sonntag": "sunday",
            "monday": "monday", "tuesday": "tuesday", "wednesday": "wednesday",
            "thursday": "thursday", "friday": "friday", "saturday": "saturday", "sunday": "sunday",
        }
        weekday = None
        if weekday_arg and isinstance(weekday_arg, str):
            weekday = weekday_map.get(weekday_arg.strip().lower(), weekday_arg.strip().lower())
        day = None
        if day_arg is not None:
            try:
                day = max(1, min(31, int(day_arg)))
            except (TypeError, ValueError):
                pass
        # IMPORTANT: Distinguish between "not provided" (None) and "default value"
        # This allows us to detect if user explicitly passed a path or not
        output_path_arg = kwargs.get("output_path")
        # Default to CHAT-ONLY: a file is written only when the user actually wants one. The path is
        # finalised after parameter extraction below (see wants_file). output_path=None -> run_task
        # skips the file-write block and the result is delivered to WebUI/messenger as a message.
        output_path = output_path_arg  # may be None; finalised after extraction
        params = kwargs.get("parameters", {})
        max_retries = kwargs.get("max_retries")
        retry_delay_minutes = kwargs.get("retry_delay_minutes")
        if max_retries is not None:
            try:
                params["max_retries"] = max(0, min(5, int(max_retries)))
            except (TypeError, ValueError):
                pass
        if retry_delay_minutes is not None:
            try:
                params["retry_delay_minutes"] = max(1, int(retry_delay_minutes))
            except (TypeError, ValueError):
                pass
        
        if not prompt:
            return "Error: No prompt provided for automation."
        
        # IMPORTANT: Automatically extract parameters from prompt if not provided
        # This ensures city, output_path, etc. are extracted even if agent doesn't pass them
        clarifier = AutomationClarifier()
        extracted_params = clarifier.extract_params(prompt)
        
        # Merge extracted params with any explicitly provided params (explicit takes precedence)
        if not params or len(params) == 0:
            params = extracted_params.copy()
        else:
            # Merge: extracted params as base, explicit params override
            params = {**extracted_params, **params}
        
        # Decide FILE vs CHAT-ONLY and finalise the output path. A file is produced only when the
        # user wants one: an explicit output_path arg, a file/save phrase in the prompt, or a path
        # the clarifier extracted from the prompt. Otherwise output_path stays None and run_task
        # delivers the result as a chat/WebUI/messenger message without writing anything.
        wants_file = (
            bool(output_path_arg)
            or self._prompt_wants_file(prompt)
            or bool(extracted_params.get("output_path"))
        )
        if output_path:
            # Explicit path given — keep it unless it looks suspicious (time format, digits, too
            # short), in which case prefer an extracted path or fall back to Documents. Works
            # regardless of system language (Documents vs Dokumente).
            is_suspicious_path = (
                (":" in output_path and len(output_path) <= 6)
                or output_path.isdigit()
                or len(output_path) <= 3
            )
            if is_suspicious_path:
                output_path = extracted_params.get("output_path") or "Documents"
        elif wants_file:
            # File wanted but no explicit path — use the extracted path, else default to Documents.
            output_path = extracted_params.get("output_path") or "Documents"
        else:
            output_path = None  # chat-only
        
        # Generate a better name from prompt if name is generic (language-independent)
        # Check if name is generic: untitled, just digits, time format (HH:MM), or very short.
        # ALSO treat a long/whole-prompt name as generic: the create_scheduled_task workflow passes
        # name="{task_description}" (the entire request), which otherwise gets stored verbatim — an
        # unwieldy title in lists and the root of the "[Errno 36] File name too long" filename. A
        # name == prompt or > 40 chars triggers the shortener below to derive a concise title.
        is_generic_name = (
            name == "untitled" or
            name.isdigit() or
            (":" in name and len(name) <= 6) or
            len(name) <= 3 or
            name.strip() == (prompt or "").strip() or
            len(name) > 40
        )
        
        if is_generic_name:
            # Language-independent name generation based on task_type and parameters
            task_type = clarifier.detect_task_type(prompt)
            
            # Helper function: Safe name sanitization (Unicode-safe, removes invalid filename chars)
            # Works with Chinese, Japanese, Cyrillic, etc. - \w matches Unicode word characters
            def safe_name(text):
                """Sanitize text for use in filenames/automation names. Unicode-safe."""
                if not text:
                    return ""
                # Replace anything that's NOT a word character (letter, digit, underscore) with underscore
                # \w in Python 3 matches Unicode word characters (Chinese, Japanese, etc.)
                sanitized = re.sub(r'[^\w]', '_', str(text))
                # Remove leading/trailing underscores and limit length
                return sanitized.strip('_')[:30]
            
            # Build name parts based on task type (language-independent identifiers)
            name_parts = []
            
            if task_type == "weather":
                name_parts.append("weather")
                if "city" in extracted_params:
                    # Works for "New York", "München", "北京" (Beijing)
                    city_name = safe_name(extracted_params["city"]).lower()
                    if city_name:
                        name_parts.append(city_name)
            elif task_type == "news":
                name_parts.append("news")
                if "category" in extracted_params:
                    category_name = safe_name(extracted_params["category"]).lower()
                    if category_name:
                        name_parts.append(category_name)
            elif task_type == "stock":
                name_parts.append("stock")
                if "symbol" in extracted_params:
                    # Stock symbols are usually uppercase ASCII
                    symbol = safe_name(extracted_params["symbol"]).upper()
                    if symbol:
                        name_parts.append(symbol)
            elif task_type == "reminder":
                name_parts.append("reminder")
            elif task_type == "email_summary":
                name_parts.append("email")
                if "email_account" in extracted_params:
                    account_name = safe_name(extracted_params["email_account"]).lower()
                    if account_name:
                        name_parts.append(account_name)
            elif task_type == "backup":
                name_parts.append("backup")
            
            # If we have name parts, join them with underscore (language-independent)
            if name_parts:
                name = "_".join(name_parts)
            else:
                # Fallback: Use LLM to generate a descriptive name from prompt (language-independent)
                # Similar to workflow matches - ask LLM to understand the intent
                name = self._generate_name_with_llm(prompt, extracted_params)
        
        # Validate frequency - must be one of the valid values
        # IMPORTANT: "once" is a valid frequency for one-time tasks — do NOT override it!
        valid_frequencies = ["once", "hourly", "daily", "weekly", "monthly"]
        if frequency not in valid_frequencies:
            # Try to infer from common patterns
            frequency_lower = str(frequency).lower()
            if "once" in frequency_lower or "einmalig" in frequency_lower or "one-time" in frequency_lower or "one time" in frequency_lower:
                frequency = "once"
            elif "täglich" in frequency_lower or "daily" in frequency_lower or "jeden tag" in frequency_lower or "every day" in frequency_lower:
                frequency = "daily"
            elif "wöchentlich" in frequency_lower or "weekly" in frequency_lower:
                frequency = "weekly"
            elif "stündlich" in frequency_lower or "hourly" in frequency_lower:
                frequency = "hourly"
            elif "monatlich" in frequency_lower or "monthly" in frequency_lower:
                frequency = "monthly"
            else:
                return (
                    f"Error: frequency '{frequency}' is not valid. "
                    "Must be one of: once, hourly, daily, weekly, monthly. "
                    "Ask the user which frequency they want — NEVER assume daily."
                )
        
        # Validate time format (HH:MM)
        if not isinstance(schedule_time, str) or ":" not in schedule_time:
            return f"Error: Invalid time format '{schedule_time}'. Expected HH:MM format (e.g., '22:46')."
        
        try:
            from datetime import datetime
            datetime.strptime(schedule_time, "%H:%M")
        except ValueError:
            return f"Error: Invalid time format '{schedule_time}'. Expected HH:MM format (e.g., '22:46')."
        
        try:
            manager, use_scope = _manager_for_scope(kwargs.get("user_scope_id"))
            
            # Check for existing automations with same name or similar prompt
            existing_tasks = manager.list()
            existing_task = None
            
            # Check for same name (case-insensitive)
            for task in existing_tasks:
                if task.name.lower() == name.lower():
                    existing_task = task
                    break
            
            # If not found by name, check for similar prompt
            if not existing_task and prompt:
                for task in existing_tasks:
                    if task.prompt:
                        # Simple similarity check: if prompts share significant words
                        prompt_words = set(prompt.lower().split())
                        task_words = set(task.prompt.lower().split())
                        if len(prompt_words) > 0 and len(task_words) > 0:
                            similarity = len(prompt_words & task_words) / max(len(prompt_words), len(task_words))
                            if similarity > 0.6:  # 60% word overlap
                                existing_task = task
                                break
            
            # Check for automations at the same time
            conflicting_task = None
            for task in existing_tasks:
                if task.time == schedule_time and task.frequency == frequency and task.enabled:
                    conflicting_task = task
                    break
            
            if conflicting_task and conflicting_task.id != (existing_task.id if existing_task else None):
                return (
                    f"ERROR: Automation already exists at the same time!\n\n"
                    f"❌ **DO NOT RETRY create_automation** - An active automation '{conflicting_task.name}' ({conflicting_task.id}) "
                    f"already runs at the same time ({schedule_time}) with the same frequency ({frequency}).\n\n"
                    f"**Solution:**\n"
                    f"- Use `read_automation(task_id='{conflicting_task.id}')` to see details, then `update_automation(task_id='{conflicting_task.id}', ...)` to update it\n"
                    f"- Or choose a different time for the new automation\n"
                    f"- Or disable the existing automation first\n\n"
                    f"**Note:** Only one automation can run at the same time."
                )
            
            # If existing task found, check if it's at the same time or different time
            if existing_task:
                same_time = existing_task.time == schedule_time and existing_task.frequency == frequency
                
                if same_time:
                    # Same time - suggest updating
                    return (
                        f"ERROR: Automation already exists with same name and time!\n\n"
                        f"❌ **DO NOT RETRY create_automation** - An automation '{existing_task.name}' ({existing_task.id}) already exists "
                        f"with the same time ({schedule_time}) and frequency ({frequency}).\n\n"
                        f"**Current settings:**\n"
                        f"- Schedule: {existing_task.frequency} at {existing_task.time}\n"
                        f"- Prompt: {existing_task.prompt[:100]}{'...' if len(existing_task.prompt) > 100 else ''}\n"
                        f"- Status: {'✅ Active' if existing_task.enabled else '⏸️ Disabled'}\n\n"
                        f"**To update the existing automation:**\n"
                        f"1. Use `read_automation(task_id='{existing_task.id}')` to see full details\n"
                        f"2. Then use `update_automation(task_id='{existing_task.id}', ...)` with new values\n\n"
                        f"**Or:** If you really want to create a new automation, choose a different name or time."
                    )
                elif not confirm_duplicate:
                    # Different time + NOT explicitly confirmed: do NOT silently create a near-duplicate.
                    # The old code returned a "wird erstellt / both run in parallel" message but RETURNED
                    # without creating anything — a lie the agent then relayed to the user ("I created a
                    # second one"). Return a TRUTHFUL decision prompt; nothing is created here. The agent
                    # asks the user, then either updates the existing one or re-calls with confirm_duplicate=True.
                    similar_tasks = [t for t in existing_tasks if t.id != existing_task.id and
                                   ((t.name.lower() == name.lower()) or
                                    (t.prompt and prompt and len(set(t.prompt.lower().split()) & set(prompt.lower().split())) / max(len(t.prompt.lower().split()), len(prompt.lower().split())) > 0.6))]
                    _more = ("\n" + "\n".join(f"- '{t.name}' ({t.id}) — {t.frequency} um {t.time}" for t in similar_tasks[:3])) if similar_tasks else ""
                    return (
                        f"⚠️ **Noch NICHTS erstellt.** Es existiert bereits eine ähnliche Automatisierung zu einer anderen Zeit:\n"
                        f"- '{existing_task.name}' ({existing_task.id}) — {existing_task.frequency} um {existing_task.time}{_more}\n\n"
                        f"Gewünscht: '{name}' um {schedule_time} ({frequency}).\n\n"
                        f"**Frag den Nutzer, was er möchte, und handle DANN entsprechend:**\n"
                        f"1. Bestehende auf die neue Zeit ändern → `update_automation(task_id='{existing_task.id}', time='{schedule_time}')`\n"
                        f"2. Wirklich eine ZWEITE parallel anlegen → erneut `create_automation(...)` mit `confirm_duplicate=true`\n\n"
                        f"WICHTIG: Behaupte NICHT, die Automatisierung sei angelegt — es wurde nichts erstellt, bis der Nutzer entscheidet."
                    )
                # else: same-time is handled above; a different time WITH confirm_duplicate falls through and
                # is actually created (the success result below is then truthful).
            
            # Check cooldown BEFORE creating automation (so agent can inform user)
            # Only apply cooldown if there's a time conflict with existing automations
            can_create, error_msg = manager.check_can_create_automation(new_time=schedule_time, new_frequency=frequency)
            if not can_create:
                return error_msg
            
            # Extract format from prompt or use default
            # Check if format is mentioned in the prompt or use default from workflow
            format_type = kwargs.get("format", "html")  # Get from workflow variables if available
            if not format_type or format_type not in ["html", "markdown", "txt"]:
                # Try to infer from prompt
                prompt_lower = prompt.lower()
                if "html" in prompt_lower:
                    format_type = "html"
                elif "markdown" in prompt_lower or "md" in prompt_lower:
                    format_type = "markdown"
                elif "txt" in prompt_lower or "text" in prompt_lower:
                    format_type = "txt"
                else:
                    format_type = "html"  # Default
            
            # Generate the (LLM-produced, slow, fragile) workflow step-array ONLY when the prompt clearly
            # needs deterministic multi-tool orchestration. A simple "tell me X / remind me daily" runs fine
            # prompt-based at trigger time, so pre-generating a JSON step-array just adds latency + a failure
            # path that — on a slow reasoning provider — fed the create_automation runaway. Either way it is
            # time-bounded (workflow_generation_timeout_seconds) and the create returns promptly.
            if self._prompt_needs_workflow_gen(prompt):
                workflow_steps = self._generate_workflow_with_llm(
                    task_description=prompt,
                    format=format_type,
                    output_path=output_path
                )
            else:
                workflow_steps = []

            # If workflow generation was skipped or failed, the task simply runs prompt-based.
            if not workflow_steps or len(workflow_steps) == 0:
                from vaf.cli.ui import UI
                UI.event("Debug", "Automation will use prompt-based execution", style="dim")
            
            # Chat-only run (no file wanted): clear the format so the stored task reflects "no file"
            # (run_task gates the file write on `if task.output_path:`, which is None here anyway).
            if not output_path:
                format_type = ""

            # Create the task (user_scope_id so run_task uses this user's tools/memory)
            task = AutomationTask(
                name=name,
                prompt=prompt,  # Keep for backwards compatibility
                workflow_steps=workflow_steps if workflow_steps else [],  # NEW: Structured workflow
                frequency=frequency,
                time=schedule_time,
                weekday=weekday if frequency == "weekly" else None,
                day=day if frequency == "monthly" else None,
                output_path=output_path,
                output_format=format_type,  # IMPORTANT: Set the output format!
                parameters=params,
                user_scope_id=use_scope,
            )
            
            task = manager.create(task)
            
            # Check if the scheduled time has already passed today
            from datetime import datetime
            now = datetime.now()
            try:
                task_time = datetime.strptime(task.time, "%H:%M").time()
            except ValueError:
                # Fallback if time format is different
                task_time = None
            
            current_time = now.time()
            
            # If time has passed and it's a daily/hourly task, run it immediately
            should_run_now = False
            if task_time:
                if task.frequency == "daily":
                    # For daily tasks: if current time >= scheduled time, run immediately
                    # Compare hours and minutes
                    if (current_time.hour > task_time.hour) or \
                       (current_time.hour == task_time.hour and current_time.minute >= task_time.minute):
                        should_run_now = True
                elif task.frequency == "hourly":
                    # For hourly tasks, check if the minute has passed
                    task_minute = int(task.time.split(":")[1])
                    current_minute = now.minute
                    if current_minute >= task_minute:
                        should_run_now = True

            catchup_skipped_note = ""
            if should_run_now:
                skip_dup, skip_msg = manager.should_skip_daily_catch_up_run(task)
                if skip_dup:
                    should_run_now = False
                    catchup_skipped_note = skip_msg or ""
            
            # Try to auto-start the scheduler if not already running. MUST go through
            # the process-wide singleton (ensure_scheduler_running): this tool's own
            # manager instance has _running=False even while the real scheduler runs,
            # and starting on it re-registered every task into the module-global
            # `schedule` registry a second time (live 2026-07-13: double TRIGGER on
            # every automation; only the run lock prevented double execution).
            scheduler_started = False
            try:
                from vaf.core.automation import ensure_scheduler_started
                _, scheduler_started = ensure_scheduler_started(origin="create_automation_tool")
            except Exception:
                # Scheduler might not be available or already running
                pass
            
            result = f"""✅ **Automation Created Successfully!**

**Name:** {task.name}
**ID:** {task.id}
**Schedule:** {task.frequency} at {task.time}
**Next Run:** {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}
**Output:** {task.output_path}"""
            
            if catchup_skipped_note:
                result += f"\n\n**Same-day catch-up skipped:** {catchup_skipped_note}"
            
            if should_run_now:
                # Run the task immediately in a new terminal (never block current terminal!)
                # Silent execution - no cooldown check, no notifications in main terminal
                import threading
                import time
                def run_immediately():
                    # Small delay to ensure the response is sent first
                    time.sleep(0.5)
                    try:
                        from vaf.cli.ui import UI
                        # Silent execution - don't show notifications in main terminal
                        # Always use new terminal - never block the current one!
                        result_msg = manager.run_task(task, new_terminal=True)
                        # Only log to debug, don't show in main terminal
                        # (Silent execution - no debug output needed)
                        pass
                    except Exception as e2:
                        # Log error but don't fail the creation
                        from vaf.cli.ui import UI
                        import traceback
                        # Only log to debug, don't show in main terminal
                        # (Silent execution - errors are silently ignored)
                        pass
                
                # Run in background thread to not block the response
                thread = threading.Thread(target=run_immediately, daemon=True)
                thread.start()
                # Give thread a moment to start
                time.sleep(0.1)
            
            if scheduler_started:
                result += f"""

✅ **Scheduler started automatically** (running in background)"""
            else:
                result += f"""

**To start the scheduler manually:**
```bash
vaf automation start
```"""
            
            result += f"""

**Other commands:**
- `vaf automation list` - View all automations
- `vaf automation run {task.id}` - Run manually
- `vaf automation delete {task.id}` - Remove"""

            # Terminal signal: the automation is created and scheduled — there is NOTHING left to do.
            # Without this an agent (esp. a slow reasoning model) kept calling list_automations/
            # read_automation/update_automation to "verify", grinding the turn loop. Tell it to stop.
            result += (
                "\n\n**Done — the automation is created and scheduled. No further action needed: "
                "do NOT call list_automations, read_automation or update_automation to verify. "
                "Just confirm to the user in one short sentence.**"
            )

            return result
            
        except Exception as e:
            return f"Error creating automation: {e}"
    
    @staticmethod
    def _prompt_needs_workflow_gen(prompt: str) -> bool:
        """True only when the automation prompt clearly needs deterministic MULTI-TOOL orchestration
        (web search + content generation + file write/open, or explicit numbered steps). A simple
        "tell me X / remind me daily" returns False → the create skips the slow, fragile LLM workflow
        pre-generation and runs prompt-based at trigger time. Removes the per-create latency/failure path
        that fed the create_automation runaway on slow reasoning providers."""
        p = (prompt or "").lower()
        if any(s in p for s in (
            "web_search", "coding_agent", "write_file", "document_viewer", "html",
            "speichere", "save the", "save it", "öffne", "open the", "open it", "schritt", "step",
        )):
            return True
        return bool(re.search(r"\b[1-9]\)", prompt or ""))

    @staticmethod
    def _prompt_wants_file(prompt: str) -> bool:
        """True when the user actually wants a FILE produced (and saved) — e.g. "als HTML/PDF",
        "speichere/save", "erstelle eine Datei/create a file", "exportiere/export". When False, the
        automation delivers its result as a chat/WebUI/messenger MESSAGE only and no file is written
        (output_path stays None -> run_task skips the file-write block). Multilingual (DE+EN) to match
        the rest of the codebase (datei/file, speichere/save, öffne/open)."""
        p = (prompt or "").lower()
        # Unambiguous substrings: file extensions + explicit tool/phrase markers.
        if any(s in p for s in (
            ".html", ".pdf", ".md", ".txt", ".csv", ".json", ".xlsx", ".docx",
            "write_file", "document_viewer", "save the", "save it", "save a",
            "open the", "open it",
        )):
            return True
        # Word-boundary tokens so 'file' does not match 'profile', etc. (DE + EN).
        return bool(re.search(
            r"\b(datei\w*|file|files|speicher\w*|abspeicher\w*|save|saved|export\w*|"
            r"herunterladen|download\w*|pdf|csv|html|markdown|öffne\w*)\b",
            p,
        ))

    def _generate_workflow_with_llm(self, task_description: str, format: str, output_path: str) -> List[Dict[str, Any]]:
        """
        Use LLM to generate a structured workflow for the automation (n8n-like).
        Returns a list of workflow steps that will be executed sequentially.
        """
        try:
            import requests
            import re
            
            prompt = f"""Generate a structured workflow for this automation task.

Task: {task_description}
Output Format: {format}
Output Path: {output_path}

Available Tools:
- web_search(query): Search the web for information (weather, news, quotes, data, etc.)
- coding_agent(task): Generate code/content (HTML, Markdown, text, etc.)
- write_file(path, content): Write content to file
- librarian_agent(task): File/info retrieval and analysis
- python_sandbox(code): Execute Python code safely for calculations
- send_to_user(message, file_path): Deliver a message to the user on their main messenger
  (channel-agnostic: the platform is resolved at RUN time from the user's main_messenger,
  with a Web UI fallback); file_path optionally attaches a produced file (HTML report, PDF, ...)

Return ONLY a JSON array of workflow steps. Each step should have:
- "tool": tool name (e.g., "web_search", "coding_agent", "write_file")
- "args": dictionary with arguments for the tool
- "description": what this step does
- "output": optional output name for next steps to reference

Rules:
1. Break down the task into logical steps (like n8n nodes)
2. First step should gather data (web_search if needed)
3. Middle steps should process/generate content (coding_agent)
4. Then save the result (write_file); if the task asks to notify/message the user,
   the LAST step is send_to_user - with file_path set to the saved file when one was
   produced. NEVER pick a platform tool (send_telegram, send_discord, ...) here: the
   user's platform is their configuration, not your decision, and it can change later.
5. Use {{previous_step.output}} to reference previous step outputs
6. For write_file, use a descriptive filename. The ONLY placeholders you may use are the
   declared step outputs above PLUS these built-in variables (do NOT invent others):
   {{date}} {{time}} {{datetime}} {{timestamp}} {{today}} {{now}} {{year}} {{month}} {{day}}
7. CRITICAL - send_to_user and write_file are DETERMINISTIC: they send/write their args
   VERBATIM. Never put a raw step output like {{weather_data}} or an instruction
   ("summarize this...") into a message or file content - no LLM processes it there.
   To send a readable message, FIRST add a coding_agent step that produces the final
   short text (CONTENT_ONLY), then send THAT output. (A live automation sent the user
   a raw search-result dump with a dangling "summarize" instruction because of this.)

Example for weather report with messenger delivery:
[
  {{"tool": "web_search", "args": {{"query": "weather Berlin tomorrow"}}, "description": "Get weather data", "output": "weather_data"}},
  {{"tool": "coding_agent", "args": {{"task": "CONTENT_ONLY: Generate ONLY the complete HTML document content (with <!DOCTYPE html>, <head>, <body>, embedded CSS styling) for a weather report.\\n\\nHere is the weather data from the previous step:\\n{{weather_data}}\\n\\nReturn ONLY the HTML code as a single string, no explanations, no project structure, no file paths, just the complete HTML document content."}}, "description": "Generate HTML content", "output": "html_content"}},
  {{"tool": "write_file", "args": {{"path": "{output_path}/weather_berlin_{{date}}.html", "content": "{{html_content}}"}}, "description": "Save HTML file"}},
  {{"tool": "coding_agent", "args": {{"task": "CONTENT_ONLY: Write ONLY the final short messenger message (3-4 lines, German, no smalltalk): current temperature, max temperature, rain probability, wind, notable warnings. Base it STRICTLY on this data:\\n{{weather_data}}"}}, "description": "Write the short message text", "output": "message_text"}},
  {{"tool": "send_to_user", "args": {{"message": "{{message_text}}", "file_path": "{output_path}/weather_berlin_{{date}}.html"}}, "description": "Deliver summary + report file on the user's main messenger"}}
]

Return ONLY valid JSON array, no explanations, no markdown code blocks."""
            
            # Call LLM to generate workflow
            messages = [{"role": "user", "content": prompt}]
            
            # Provider-aware: query_llm reaches the configured cloud provider (api_model_{provider})
            # OR the local server, instead of hardcoding 127.0.0.1:8080 (which only exists in local
            # mode). Replaces the old health-check + local-Agent()-spinup + :8080 POST.
            from vaf.core.config import Config as _CfgWf
            _wf_to = int(_CfgWf.get("workflow_generation_timeout_seconds", 90) or 90)
            content = self.query_llm(messages, max_tokens=1024, temperature=0.2, timeout=_wf_to)
            if not content:
                from vaf.cli.ui import UI
                UI.event("Debug", "Workflow generation: empty LLM response, using prompt-based execution", style="dim")
                return []

            # Robustly extract the first VALID JSON array (tolerates markdown / prose / trailing text)
            workflow_steps = _extract_first_json_array(content.strip())
            if not workflow_steps:
                from vaf.cli.ui import UI
                UI.event("Debug", "Workflow generation: no valid JSON array in response, using prompt-based execution", style="dim")
                return []

            # Validate workflow steps
            if isinstance(workflow_steps, list) and len(workflow_steps) > 0:
                for step in workflow_steps:
                    if "tool" not in step:
                        step["tool"] = "coding_agent"
                    if "args" not in step:
                        step["args"] = {}
                    if "description" not in step:
                        step["description"] = f"Execute {step['tool']}"
                from vaf.cli.ui import UI
                UI.event("Debug", f"Generated {len(workflow_steps)} workflow steps", style="dim")
                return workflow_steps
            from vaf.cli.ui import UI
            UI.event("Debug", "Workflow generation: Empty or invalid workflow steps", style="dim")
            return []
            
            return []
            
        except Exception as e:
            # Fallback: return empty list, will use prompt-based execution
            return []
    
    def _generate_name_with_llm(self, prompt: str, extracted_params: Dict[str, Any]) -> str:
        """
        Generate automation name using LLM (language-independent, like workflow matches).
        
        Instead of hardcoded stop-words, we ask the LLM to understand the prompt
        and generate a short, descriptive name. Uses the same approach as workflow matches.
        """
        try:
            import requests
            from vaf.cli.ui import UI
            
            # Use same animation style as workflow matches
            with UI.console.status("[bold cyan](O_O)  Analyzing automation name...[/bold cyan]", spinner="dots"):
                # Build context about extracted parameters
                params_context = ""
                if extracted_params:
                    params_list = []
                    if "city" in extracted_params:
                        params_list.append(f"city: {extracted_params['city']}")
                    if "category" in extracted_params:
                        params_list.append(f"category: {extracted_params['category']}")
                    if "symbol" in extracted_params:
                        params_list.append(f"symbol: {extracted_params['symbol']}")
                    if params_list:
                        params_context = f"\n\nExtracted parameters: {', '.join(params_list)}"
                
                # Simple prompt to LLM: "Generate a short name for this automation"
                llm_prompt = f"""Generate a short, descriptive name for this automation task.

Task description: {prompt}{params_context}

Requirements:
- Maximum 30 characters
- Use underscores instead of spaces (e.g., "weather_berlin" not "weather berlin")
- Language-independent (use English identifiers like "weather", "news", "stock")
- Include key parameters if available (city, category, etc.)
- Remove common words like "create", "make", "generate", "daily", "schedule", "automation"
- Make it descriptive but concise

Examples:
- "Weather report for Berlin" → "weather_berlin"
- "Daily news summary" → "news_daily"
- "Stock price for AAPL" → "stock_aapl"
- "Create backup of documents" → "backup_documents"

Return ONLY the name, no explanations, no quotes, no markdown, just the name:"""

                messages = [{"role": "user", "content": llm_prompt}]
                
                # Provider-aware: query_llm reaches the cloud provider OR the local server,
                # instead of the hardcoded 127.0.0.1:8080 + local-Agent()-spinup fallback.
                import re
                name = self.query_llm(messages, max_tokens=50, temperature=0.2, timeout=10)
                if name:
                    name = name.strip('"\'` \n\t')
                    if name.startswith('```'):
                        _nlines = name.split('\n')
                        name = '\n'.join(_nlines[1:-1]) if len(_nlines) > 2 else name
                        name = name.strip()
                    if name and len(name) <= 50:
                        def safe_name(text):
                            if not text:
                                return ""
                            sanitized = re.sub(r'[^\w]', '_', str(text))
                            return sanitized.strip('_')[:30]
                        return safe_name(name)
                
        except Exception as e:
            # Silent fallback - don't show error to user
            pass
        
        # Fallback: Simple extraction if LLM unavailable
        import re
        words = re.findall(r'\b\w{3,}\b', prompt.lower())
        # Just take first 2-3 meaningful words (no stop-words needed)
        if words:
            return "_".join(words[:3])[:30]
        else:
            # Last resort
            return "automation"


class ListAutomationsTool(BaseTool):
    """Tool for listing automation tasks."""
    
    name = "list_automations"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List all scheduled automation tasks with prompts and parameters. "
        "Each entry includes a **today** status: Done (today), Scheduled (later today), "
        "Due (not yet run today), or In progress—so you know if the job already ran today."
    )
    
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        try:
            user_scope_id = kwargs.get("user_scope_id")
            user_role = kwargs.get("user_role")
            manager, _ = _manager_for_scope(user_scope_id, user_role=user_role)
            tasks = manager.list()

            # Admins get aggregated manager; restrict visible list to root + admin-visible scopes.
            if (user_role or "").strip().lower() == "admin" and user_scope_id:
                from vaf.core.config import get_local_admin_scope_id
                local_admin_scope = get_local_admin_scope_id()
                tasks = [
                    t for t in tasks
                    if (
                        t.user_scope_id is None
                        or str(t.user_scope_id) == str(user_scope_id)
                        or str(t.user_scope_id) == str(local_admin_scope)
                    )
                ]
            
            if not tasks:
                return "No automations configured yet. Use `create_automation` to create one."
            
            result = "**📋 Scheduled Automations:**\n\n"
            
            for task in tasks:
                status = "✅ Active" if task.enabled else "⏸️ Disabled"
                today_line = format_daily_calendar_status(task)
                result += f"• **{task.name}** ({task.id}) - {status}\n"
                result += f"  Schedule: {task.frequency} at {task.time}\n"
                result += f"  Next: {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                result += f"  Today (local): **{today_line}**\n"
                result += f"  Status: {status}\n"
                result += f"  **Prompt:** {task.prompt[:100]}{'...' if len(task.prompt) > 100 else ''}\n"
                if task.parameters:
                    result += f"  **Parameters:** {json.dumps(task.parameters, ensure_ascii=False)}\n"
                result += f"  Use `read_automation` with ID '{task.id}' to see full details\n\n"
            
            return result
            
        except Exception as e:
            return f"Error listing automations: {e}"


class ReadAutomationTool(BaseTool):
    """Tool for reading the full content of a specific automation task."""
    
    name = "read_automation"
    permission_level = "read"
    side_effect_class = "none"
    description = "Read the full content of a specific automation task including prompt, parameters, schedule, and all details. Use this when user asks about what an automation does or wants to see the full content of an automation."
    
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ID of the automation task to read (e.g., '904afb26')"
            }
        },
        "required": ["task_id"]
    }
    
    def run(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        
        if not task_id:
            return "Error: No task ID provided. Use `list_automations` to see available task IDs."
        
        try:
            manager, _ = _manager_for_scope(
                kwargs.get("user_scope_id"),
                user_role=kwargs.get("user_role"),
            )
            task = manager.get(task_id)
            
            if not task:
                return f"Error: Automation task '{task_id}' not found. Use `list_automations` to see available tasks."
            
            result = f"""**📄 Automation Details: {task.name}**

**ID:** {task.id}
**Status:** {'✅ Active' if task.enabled else '⏸️ Disabled'}
**Schedule:** {task.frequency} at {task.time}
**Next Run:** {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}
**Last Run:** {task.last_run or 'Never'}
**Last completed (local date):** {task.last_completed_local_date or '(legacy: infer from Last Run)'}
**Today (local calendar):** {format_daily_calendar_status(task)}
**Created:** {task.created_at}
**Output Path:** {task.output_path or 'Not specified'}
**Output Format:** {task.output_format}

**Full Prompt:**
```
{task.prompt}
```

"""
            
            if task.parameters:
                result += f"**Parameters:**\n```json\n{json.dumps(task.parameters, indent=2, ensure_ascii=False)}\n```\n\n"
            
            if task.description:
                result += f"**Description:** {task.description}\n\n"
            
            result += f"**Full JSON Content:**\n```json\n{json.dumps(task.to_dict(), indent=2, ensure_ascii=False)}\n```"
            
            return result
            
        except Exception as e:
            return f"Error reading automation: {e}"


class UpdateAutomationTool(BaseTool):
    """Tool for updating existing automation tasks."""
    
    name = "update_automation"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Update an existing automation task by ID. Use this when user wants to modify an existing automation (change time, frequency, prompt, etc.) instead of creating a new one.
    
**IMPORTANT:** Always check existing automations with `list_automations` first to find the correct ID, then use `read_automation` to see current values before updating.
When changing **time**, the new time must be at least 10 minutes apart from all other automations; the system will return an error otherwise."""
    
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the automation task to update (get from list_automations)"
            },
            "name": {
                "type": "string",
                "description": "New name for the automation (optional)"
            },
            "prompt": {
                "type": "string",
                "description": "New prompt/task description (optional)"
            },
            "frequency": {
                "type": "string",
                "enum": ["once", "hourly", "daily", "weekly", "monthly"],
                "description": "New frequency (optional): once, hourly, daily, weekly, monthly"
            },
            "time": {
                "type": "string",
                "description": "New time in HH:MM format (optional). Must be at least 10 minutes apart from other automations."
            },
            "weekday": {
                "type": "string",
                "description": "For frequency 'weekly': weekday in English lowercase (e.g. 'monday')"
            },
            "day": {
                "type": "integer",
                "description": "For frequency 'monthly': day of month (1-31)"
            },
            "output_path": {
                "type": "string",
                "description": "New output path (optional)"
            },
            "parameters": {
                "type": "object",
                "description": "New parameters (optional)"
            }
        },
        "required": ["task_id"]
    }
    
    def run(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        
        if not task_id:
            return "Error: No task ID provided. Use `list_automations` to see available task IDs."
        
        try:
            manager, _ = _manager_for_scope(
                kwargs.get("user_scope_id"),
                user_role=kwargs.get("user_role"),
            )
            task = manager.get(task_id)
            
            if not task:
                return f"Error: Automation with ID '{task_id}' not found. Use `list_automations` to see available automations."
            
            # Check for time interval (10-min rule) if time is being updated
            if "time" in kwargs and kwargs["time"]:
                new_time = kwargs["time"]
                new_frequency = kwargs.get("frequency", task.frequency)
                can_update, err_msg = manager.check_can_update_automation(
                    task_id=task_id, new_time=new_time, new_frequency=new_frequency
                )
                if not can_update and err_msg:
                    return err_msg
            
            # Prepare update parameters (only include non-None values)
            update_params = {}
            if "name" in kwargs and kwargs["name"]:
                update_params["name"] = kwargs["name"]
            if "prompt" in kwargs and kwargs["prompt"]:
                update_params["prompt"] = kwargs["prompt"]
            if "frequency" in kwargs and kwargs["frequency"]:
                update_params["frequency"] = kwargs["frequency"]
            if "time" in kwargs and kwargs["time"]:
                update_params["time"] = kwargs["time"]
            if "weekday" in kwargs:
                w = kwargs.get("weekday")
                update_params["weekday"] = w.strip().lower() if isinstance(w, str) and w.strip() else None
            if "day" in kwargs and kwargs["day"] is not None:
                try:
                    update_params["day"] = max(1, min(31, int(kwargs["day"])))
                except (TypeError, ValueError):
                    pass
            if "output_path" in kwargs and kwargs["output_path"]:
                update_params["output_path"] = kwargs["output_path"]
            if "parameters" in kwargs and kwargs["parameters"]:
                update_params["parameters"] = kwargs["parameters"]
            
            if not update_params:
                return f"Error: No update parameters provided. Automation '{task.name}' (ID: {task_id}) remains unchanged."
            
            # Regenerate workflow steps if prompt changed
            if "prompt" in update_params:
                format_type = "html"  # Default
                prompt_lower = update_params["prompt"].lower()
                if "html" in prompt_lower:
                    format_type = "html"
                elif "markdown" in prompt_lower or "md" in prompt_lower:
                    format_type = "markdown"
                elif "txt" in prompt_lower or "text" in prompt_lower:
                    format_type = "txt"
                
                workflow_steps = self._generate_workflow_with_llm(
                    task_description=update_params["prompt"],
                    format=format_type,
                    output_path=update_params.get("output_path", task.output_path or "Documents")
                )
                update_params["workflow_steps"] = workflow_steps
            
            updated_task = manager.update(task_id, **update_params)
            
            if not updated_task:
                return f"Error: Failed to update automation '{task_id}'."
            
            result = f"""✅ **Automation Updated Successfully!**

**Name:** {updated_task.name}
**ID:** {updated_task.id}
**Schedule:** {updated_task.frequency} at {updated_task.time}
**Next Run:** {updated_task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}
**Output:** {updated_task.output_path}

**Updated fields:** {', '.join(update_params.keys())}"""
            
            return result
            
        except Exception as e:
            return f"Error updating automation: {e}"
    
    def _generate_workflow_with_llm(self, task_description: str, format: str, output_path: str) -> List[Dict[str, Any]]:
        """Reuse workflow generation from AutomationTool."""
        # Import and use the same method from AutomationTool
        from vaf.tools.automation import AutomationTool
        tool = AutomationTool()
        return tool._generate_workflow_with_llm(task_description, format, output_path)


class DeleteAutomationTool(BaseTool):
    """Tool for deleting automation tasks (moves to trash for recovery)."""
    
    name = "delete_automation"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Delete an automation task by ID. The task will be moved to trash and can be restored later.
Use this when user wants to remove an automation but might want to restore it later."""
    
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ID of the automation task to delete"
            }
        },
        "required": ["task_id"]
    }
    
    def run(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        
        if not task_id:
            return "Error: No task ID provided."
        
        try:
            manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
            task = manager.get(task_id)
            
            if not task:
                # IDEMPOTENT: If automation doesn't exist, it's already deleted (success)
                return f"""✅ **Automation Already Deleted**

The automation with ID '{task_id}' does not exist (already deleted or never existed).

**Status:** Task successfully removed (idempotent operation)."""
            
            # Move to trash
            success = manager.move_to_trash(task_id)
            
            if success:
                return f"""🗑️ **Automation Deleted (Moved to Trash)**

**Name:** {task.name}
**ID:** {task_id}

The automation has been moved to trash and can be restored using `restore_automation` with the same ID.

**To permanently delete:** Use `empty_trash` command (CLI only)"""
            else:
                return f"Error: Failed to delete automation '{task_id}'."
                
        except Exception as e:
            return f"Error deleting automation: {e}"


class RestoreAutomationTool(BaseTool):
    """Tool for restoring automation tasks from trash."""
    
    name = "restore_automation"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Restore a deleted automation task from trash by ID.
Use this when user wants to recover a previously deleted automation."""
    
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ID of the automation task to restore from trash"
            }
        },
        "required": ["task_id"]
    }
    
    def run(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        
        if not task_id:
            return "Error: No task ID provided."
        
        try:
            manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
            
            # Check if task exists in trash
            trash_tasks = manager.list_trash()
            task_in_trash = any(t.id == task_id for t in trash_tasks)
            
            if not task_in_trash:
                return f"Error: Automation with ID '{task_id}' not found in trash."
            
            # Restore from trash
            success = manager.restore_from_trash(task_id)
            
            if success:
                task = manager.get(task_id)
                return f"""✅ **Automation Restored Successfully!**

**Name:** {task.name}
**ID:** {task_id}
**Schedule:** {task.frequency} at {task.time}
**Next Run:** {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}

The automation has been restored and is now active again."""
            else:
                return f"Error: Failed to restore automation '{task_id}'."
                
        except Exception as e:
            return f"Error restoring automation: {e}"


class ListTrashTool(BaseTool):
    """Tool for listing deleted automation tasks in trash."""
    
    name = "list_trash"
    permission_level = "read"
    side_effect_class = "none"
    description = "List all automation tasks in trash (deleted but recoverable)."
    
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        try:
            manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
            tasks = manager.list_trash()
            
            if not tasks:
                return "🗑️ Trash is empty. No deleted automations to restore."
            
            result = "**🗑️ Deleted Automations (in Trash):**\n\n"
            
            for task in tasks:
                result += f"• **{task.name}** ({task.id})\n"
                result += f"  Schedule: {task.frequency} at {task.time}\n"
                result += f"  Use `restore_automation` with ID '{task.id}' to restore\n\n"
            
            return result
            
        except Exception as e:
            return f"Error listing trash: {e}"
