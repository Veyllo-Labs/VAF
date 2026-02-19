"""
VAF Automation Tool - Allow the model to create scheduled tasks
"""
import json
import re
from typing import List, Dict, Any, Optional, Tuple
from vaf.tools.base import BaseTool
from vaf.core.automation import AutomationManager, AutomationTask, AutomationClarifier


def _manager_for_scope(user_scope_id: Optional[str]) -> Tuple[AutomationManager, Optional[str]]:
    """Return (manager, effective_scope) for user-scoped automation access. Local admin uses global scope."""
    from vaf.core.config import get_local_admin_scope_id
    local = get_local_admin_scope_id()
    if not user_scope_id or str(user_scope_id).strip() == str(local).strip():
        return AutomationManager(), None
    return AutomationManager(user_scope_id=user_scope_id), user_scope_id


class AutomationTool(BaseTool):
    """Tool for creating and managing automated tasks."""
    
    name = "create_automation"
    description = """Create a scheduled automation task that runs at specified times.
Use this when user wants to schedule recurring tasks or a one-time task:
- Once (einmalig): single run, scheduled for the next day
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

4. **PROMPT CONTENT (when the automation sends something to the user):**
   - Never hardcode a messenger (e.g. send_telegram). The user may use WhatsApp, Discord, email, etc.
   - In the prompt, instruct: "Send the result/summary to the user via their **main_messenger** (see User Identity): use the matching tool—send_telegram, send_whatsapp, send_discord, send_slack, or send_mail—depending on main_messenger. If main_messenger is not set, summarize in your reply."
   - For "weekly review", "wöchentlicher Report", or similar: use frequency **weekly** and set **weekday** (e.g. friday). Do not use daily.
   - Do not assume git or version control. If the prompt mentions "recent changes" or "commits", phrase it as: "If the project uses version control (e.g. git), check recent commits with git_log; otherwise skip or use file/context." """
    
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
                "description": "How often to run: once (single run), hourly, daily, weekly, monthly"
            },
            "time": {
                "type": "string",
                "description": "Time to run in HH:MM format (e.g., '06:00', '18:30')"
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
            }
        },
        "required": ["name", "prompt", "frequency", "time"]
    }
    
    def run(self, **kwargs) -> str:
        name = kwargs.get("name", "untitled")
        prompt = kwargs.get("prompt", "")
        frequency = (kwargs.get("frequency") or "daily").lower()
        schedule_time = kwargs.get("time", "06:00")
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
        output_path = output_path_arg or "Documents"  # Fallback only if not provided
        params = kwargs.get("parameters", {})
        
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
        
        # Also extract output_path from prompt if not explicitly provided or if it's invalid
        # Check if output_path looks suspicious (time format, digits, too short)
        is_suspicious_path = output_path and (
            (":" in output_path and len(output_path) <= 6) or 
            output_path.isdigit() or
            len(output_path) <= 3
        )
        
        # LOGIC FIX: Override path with extracted_params if:
        # 1. No path was explicitly provided (output_path_arg is None)
        # 2. OR the explicit path is the default "Documents" placeholder
        # 3. OR the path looks suspicious/invalid (time format, etc.)
        # This works regardless of system language (Documents vs Dokumente)
        if (not output_path_arg or output_path == "Documents" or is_suspicious_path) and "output_path" in extracted_params:
            output_path = extracted_params["output_path"]
        
        # Safety fallback: if path is still suspicious after extraction, use default
        if is_suspicious_path and output_path == output_path_arg:
            output_path = "Documents"
        
        # Generate a better name from prompt if name is generic (language-independent)
        # Check if name is generic: untitled, just digits, time format (HH:MM), or very short
        is_generic_name = (
            name == "untitled" or 
            name.isdigit() or 
            (":" in name and len(name) <= 6) or
            len(name) <= 3
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
        valid_frequencies = ["hourly", "daily", "weekly", "monthly"]
        if frequency not in valid_frequencies:
            # Try to infer from common patterns
            frequency_lower = str(frequency).lower()
            if "täglich" in frequency_lower or "daily" in frequency_lower or "jeden tag" in frequency_lower or "every day" in frequency_lower:
                frequency = "daily"
            elif "wöchentlich" in frequency_lower or "weekly" in frequency_lower:
                frequency = "weekly"
            elif "stündlich" in frequency_lower or "hourly" in frequency_lower:
                frequency = "hourly"
            elif "monatlich" in frequency_lower or "monthly" in frequency_lower:
                frequency = "monthly"
            else:
                # Default to daily if frequency looks like a time (contains :)
                if ":" in str(frequency):
                    frequency = "daily"
                else:
                    frequency = "daily"  # Safe default
        
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
                else:
                    # Different time - inform user but allow creation
                    similar_tasks = [t for t in existing_tasks if t.id != existing_task.id and 
                                   ((t.name.lower() == name.lower()) or 
                                    (t.prompt and prompt and len(set(t.prompt.lower().split()) & set(prompt.lower().split())) / max(len(t.prompt.lower().split()), len(prompt.lower().split())) > 0.6))]
                    
                    if similar_tasks:
                        similar_info = "\n".join([f"- '{t.name}' ({t.id}) - {t.frequency} at {t.time}" for t in similar_tasks[:3]])
                        return (
                            f"ℹ️ **Ähnliche Automatisierungen existieren bereits (aber zu anderen Zeiten):**\n\n"
                            f"**Gefundene ähnliche Automatisierungen:**\n"
                            f"- '{existing_task.name}' ({existing_task.id}) - {existing_task.frequency} at {existing_task.time}\n"
                            f"{similar_info if similar_tasks else ''}\n\n"
                            f"**Neue Automatisierung:**\n"
                            f"- Name: {name}\n"
                            f"- Zeit: {schedule_time} ({frequency})\n\n"
                            f"**Optionen:**\n"
                            f"1. **Neue Automatisierung erstellen** (wird fortgesetzt) - da die Zeit unterschiedlich ist, ist das in Ordnung\n"
                            f"2. **Bestehende aktualisieren** - verwende `read_automation(task_id='{existing_task.id}')` und dann `update_automation`\n\n"
                            f"**Hinweis:** Da die Zeiten unterschiedlich sind ({existing_task.time} vs {schedule_time}), können beide Automatisierungen parallel laufen."
                        )
                    else:
                        # Just one similar task at different time - allow creation
                        return (
                            f"ℹ️ **Ähnliche Automatisierung existiert bereits (aber zu anderer Zeit):**\n\n"
                            f"Eine ähnliche Automatisierung '{existing_task.name}' ({existing_task.id}) existiert bereits, "
                            f"aber zu einer anderen Zeit ({existing_task.time} vs {schedule_time}).\n\n"
                            f"**Da die Zeiten unterschiedlich sind, wird die neue Automatisierung erstellt.**\n"
                            f"Beide können parallel laufen.\n\n"
                            f"**Falls du stattdessen die bestehende aktualisieren möchtest:**\n"
                            f"Verwende `read_automation(task_id='{existing_task.id}')` und dann `update_automation`."
                        )
            
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
            
            # Generate structured workflow using LLM (n8n-like)
            workflow_steps = self._generate_workflow_with_llm(
                task_description=prompt,
                format=format_type,
                output_path=output_path
            )
            
            # If workflow generation failed, log a warning but still create the task
            if not workflow_steps or len(workflow_steps) == 0:
                from vaf.cli.ui import UI
                UI.event("Warning", "Workflow generation failed - automation will use prompt-based execution", style="yellow")
            
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
            
            # Try to auto-start scheduler if not already running
            scheduler_started = False
            try:
                if not manager._running:
                    manager.start_scheduler()
                    scheduler_started = True
            except Exception:
                # Scheduler might not be available or already running
                pass
            
            result = f"""✅ **Automation Created Successfully!**

**Name:** {task.name}
**ID:** {task.id}
**Schedule:** {task.frequency} at {task.time}
**Next Run:** {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}
**Output:** {task.output_path}"""
            
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
            
            return result
            
        except Exception as e:
            return f"Error creating automation: {e}"
    
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

Return ONLY a JSON array of workflow steps. Each step should have:
- "tool": tool name (e.g., "web_search", "coding_agent", "write_file")
- "args": dictionary with arguments for the tool
- "description": what this step does
- "output": optional output name for next steps to reference

Rules:
1. Break down the task into logical steps (like n8n nodes)
2. First step should gather data (web_search if needed)
3. Middle steps should process/generate content (coding_agent)
4. Last step should save the result (write_file)
5. Use {{previous_step.output}} to reference previous step outputs
6. For write_file, use descriptive filename with date if needed

Example for weather report:
[
  {{"tool": "web_search", "args": {{"query": "weather Berlin tomorrow"}}, "description": "Get weather data", "output": "weather_data"}},
  {{"tool": "web_search", "args": {{"query": "motivational quote today"}}, "description": "Get motivational quote", "output": "quote_data"}},
  {{"tool": "coding_agent", "args": {{"task": "CONTENT_ONLY: Generate ONLY the complete HTML document content (with <!DOCTYPE html>, <head>, <body>, embedded CSS styling) for a weather report.\\n\\nHere is the weather data from the previous step:\\n{{weather_data}}\\n\\nHere is the motivational quote from the previous step:\\n{{quote_data}}\\n\\nReturn ONLY the HTML code as a single string, no explanations, no project structure, no file paths, just the complete HTML document content."}}, "description": "Generate HTML content", "output": "html_content"}},
  {{"tool": "write_file", "args": {{"path": "{output_path}/weather_berlin_{{date}}.html", "content": "{{html_content}}"}}, "description": "Save HTML file"}}
]

Return ONLY valid JSON array, no explanations, no markdown code blocks."""
            
            # Call LLM to generate workflow
            messages = [{"role": "user", "content": prompt}]
            
            # Try to use existing server (silent - no agent initialization)
            # The server should already be running if VAF is active in the main window
            try:
                import requests
                
                # First check if server is running
                try:
                    health_check = requests.get("http://127.0.0.1:8080/health", timeout=2)
                    if health_check.status_code != 200:
                        from vaf.cli.ui import UI
                        UI.event("Debug", "Workflow generation: Server not ready, initializing Agent...", style="dim")
                        # Fallback: Initialize Agent if server not ready
                        from vaf.core.agent import Agent
                        agent = Agent(verbose=False)
                        agent.load_model()
                        agent.init_chat()
                        
                        # Use agent's chat_step to get response
                        response_parts = []
                        agent.chat_step(prompt, stream_callback=lambda x: response_parts.append(x), skip_input=True)
                        content = "".join(response_parts).strip()
                        agent.shutdown()
                        
                        # Try to extract JSON from response
                        json_match = re.search(r'\[.*\]', content, re.DOTALL)
                        if not json_match:
                            from vaf.cli.ui import UI
                            UI.event("Debug", "Workflow generation: No JSON array found in response, using prompt-based execution", style="dim")
                            return []
                        
                        content = json_match.group(0).strip()
                        if not content:
                            from vaf.cli.ui import UI
                            UI.event("Debug", "Workflow generation: Empty JSON content, using prompt-based execution", style="dim")
                            return []
                        
                        try:
                            workflow_steps = json.loads(content)
                        except json.JSONDecodeError as e:
                            from vaf.cli.ui import UI
                            UI.event("Debug", f"Workflow generation: JSON parse error ({e}), using prompt-based execution", style="dim")
                            return []
                        
                        # Validate and return
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
                        else:
                            from vaf.cli.ui import UI
                            UI.event("Debug", "Workflow generation: Empty or invalid workflow steps", style="dim")
                            return []
                except requests.exceptions.RequestException:
                    # Server not running - fallback to initializing Agent
                    from vaf.cli.ui import UI
                    UI.event("Debug", "Workflow generation: Server not running, initializing Agent...", style="dim")
                    from vaf.core.agent import Agent
                    agent = Agent(verbose=False)
                    agent.load_model()
                    agent.init_chat()
                    
                    # Use agent's chat_step to get response
                    response_parts = []
                    agent.chat_step(prompt, stream_callback=lambda x: response_parts.append(x), skip_input=True)
                    content = "".join(response_parts).strip()
                    agent.shutdown()
                    
                    # Try to extract JSON from response
                    json_match = re.search(r'\[.*\]', content, re.DOTALL)
                    if not json_match:
                        from vaf.cli.ui import UI
                        UI.event("Debug", "Workflow generation: No JSON array found in response, using prompt-based execution", style="dim")
                        return []
                    
                    content = json_match.group(0).strip()
                    if not content:
                        from vaf.cli.ui import UI
                        UI.event("Debug", "Workflow generation: Empty JSON content, using prompt-based execution", style="dim")
                        return []
                    
                    try:
                        workflow_steps = json.loads(content)
                    except json.JSONDecodeError as e:
                        from vaf.cli.ui import UI
                        UI.event("Debug", f"Workflow generation: JSON parse error ({e}), using prompt-based execution", style="dim")
                        return []
                    
                    # Validate and return
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
                    else:
                        from vaf.cli.ui import UI
                        UI.event("Debug", "Workflow generation: Empty or invalid workflow steps", style="dim")
                        return []
                
                # Server is running - use it directly
                payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.2}
                res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=30)
                res.raise_for_status()
                response_data = res.json()
                
                if 'choices' not in response_data or len(response_data['choices']) == 0:
                    from vaf.cli.ui import UI
                    UI.event("Debug", "Workflow generation: Empty response from LLM", style="dim")
                    return []
                
                content = response_data['choices'][0]['message']['content'].strip()
                
                # Try to extract JSON from response (might be wrapped in markdown)
                json_match = re.search(r'\[.*\]', content, re.DOTALL)
                if not json_match:
                    from vaf.cli.ui import UI
                    UI.event("Debug", "Workflow generation: No JSON array found in response, using prompt-based execution", style="dim")
                    return []
                
                content = json_match.group(0).strip()
                if not content:
                    from vaf.cli.ui import UI
                    UI.event("Debug", "Workflow generation: Empty JSON content, using prompt-based execution", style="dim")
                    return []
                
                try:
                    workflow_steps = json.loads(content)
                except json.JSONDecodeError as e:
                    from vaf.cli.ui import UI
                    UI.event("Debug", f"Workflow generation: JSON parse error ({e}), using prompt-based execution", style="dim")
                    return []
                
                # Validate workflow steps
                if isinstance(workflow_steps, list) and len(workflow_steps) > 0:
                    # Ensure all steps have required fields
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
                else:
                    from vaf.cli.ui import UI
                    UI.event("Debug", "Workflow generation: Empty or invalid workflow steps", style="dim")
                    return []
            except requests.exceptions.RequestException as e:
                # Network/server error
                from vaf.cli.ui import UI
                UI.event("Debug", f"Workflow generation: Server error ({e}), using prompt-based execution", style="dim")
                return []
            except json.JSONDecodeError as e:
                # JSON parsing error
                from vaf.cli.ui import UI
                UI.event("Debug", f"Workflow generation: JSON parse error ({e}), using prompt-based execution", style="dim")
                return []
            except Exception as e:
                # Other errors
                from vaf.cli.ui import UI
                UI.event("Debug", f"Workflow generation failed: {e}, using prompt-based execution", style="dim")
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
                
                try:
                    # Try server first (same as workflow generation)
                    payload = {
                        "messages": messages, 
                        "max_tokens": 50,  # Names are short, 50 is enough
                        "temperature": 0.2  # Low temperature for consistent, predictable names
                    }
                    res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=5)
                    if res.status_code == 200:
                        response_data = res.json()
                        if 'choices' in response_data and len(response_data['choices']) > 0:
                            name = response_data['choices'][0]['message']['content'].strip()
                            # Clean up: remove quotes, extra whitespace, markdown code blocks
                            name = name.strip('"\'` \n\t')
                            # Remove markdown code blocks if present
                            if name.startswith('```'):
                                lines = name.split('\n')
                                name = '\n'.join(lines[1:-1]) if len(lines) > 2 else name
                                name = name.strip()
                            # Validate and sanitize
                            if name and len(name) <= 50:
                                # Use safe_name helper to ensure valid filename
                                def safe_name(text):
                                    if not text:
                                        return ""
                                    sanitized = re.sub(r'[^\w]', '_', str(text))
                                    return sanitized.strip('_')[:30]
                                return safe_name(name)
                except requests.exceptions.RequestException:
                    # Server not running - fallback to Agent initialization
                    from vaf.core.agent import Agent
                    agent = Agent(verbose=False)
                    agent.load_model()
                    agent.init_chat()
                    
                    # Use agent's chat_step to get response
                    response_parts = []
                    agent.chat_step(llm_prompt, stream_callback=lambda x: response_parts.append(x), skip_input=True)
                    content = "".join(response_parts).strip()
                    agent.shutdown()
                    
                    # Clean up response
                    name = content.strip('"\'` \n\t')
                    if name.startswith('```'):
                        lines = name.split('\n')
                        name = '\n'.join(lines[1:-1]) if len(lines) > 2 else name
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
    description = "List all scheduled automation tasks with their full details including prompts and parameters."
    
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        try:
            manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
            tasks = manager.list()
            
            if not tasks:
                return "No automations configured yet. Use `create_automation` to create one."
            
            result = "**📋 Scheduled Automations:**\n\n"
            
            for task in tasks:
                status = "✅ Active" if task.enabled else "⏸️ Disabled"
                result += f"• **{task.name}** ({task.id}) - {status}\n"
                result += f"  Schedule: {task.frequency} at {task.time}\n"
                result += f"  Next: {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}\n"
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
            manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
            task = manager.get(task_id)
            
            if not task:
                return f"Error: Automation task '{task_id}' not found. Use `list_automations` to see available tasks."
            
            result = f"""**📄 Automation Details: {task.name}**

**ID:** {task.id}
**Status:** {'✅ Active' if task.enabled else '⏸️ Disabled'}
**Schedule:** {task.frequency} at {task.time}
**Next Run:** {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}
**Last Run:** {task.last_run or 'Never'}
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
    description = """Update an existing automation task by ID. Use this when user wants to modify an existing automation (change time, frequency, prompt, etc.) instead of creating a new one.
    
**IMPORTANT:** Always check existing automations with `list_automations` first to find the correct ID, then use `read_automation` to see current values before updating."""
    
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
                "description": "New time in HH:MM format (optional)"
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
            manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
            task = manager.get(task_id)
            
            if not task:
                return f"Error: Automation with ID '{task_id}' not found. Use `list_automations` to see available automations."
            
            # Check for time conflicts if time is being updated
            if "time" in kwargs and kwargs["time"]:
                new_time = kwargs["time"]
                new_frequency = kwargs.get("frequency", task.frequency)
                
                # Check for conflicts with other automations
                existing_tasks = manager.list()
                for existing_task in existing_tasks:
                    if existing_task.id != task_id and existing_task.enabled:
                        if existing_task.time == new_time and existing_task.frequency == new_frequency:
                            return (
                                f"ERROR: Automation already exists at the same time!\n\n"
                                f"❌ **DO NOT RETRY update_automation** - An active automation '{existing_task.name}' ({existing_task.id}) "
                                f"already runs at the same time ({new_time}) with the same frequency ({new_frequency}).\n\n"
                                f"**Solution:**\n"
                                f"- Choose a different time for this automation\n"
                                f"- Or disable the other automation '{existing_task.name}' first\n\n"
                                f"**Note:** Only one automation can run at the same time."
                            )
            
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
