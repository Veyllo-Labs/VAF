"""
VAF Automation Tool - Allow the model to create scheduled tasks
"""
import json
from typing import List, Dict, Any
from vaf.tools.base import BaseTool
from vaf.core.automation import AutomationManager, AutomationTask, AutomationClarifier


class AutomationTool(BaseTool):
    """Tool for creating and managing automated tasks."""
    
    name = "create_automation"
    description = """Create a scheduled automation task that runs at specified times.
Use this when user wants to schedule recurring tasks like:
- Daily news/weather reports
- Periodic backups
- Scheduled reminders
- Regular document generation

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
- Inform user: "I'll update the existing automation [name] instead of creating a new one" """
    
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
                "enum": ["hourly", "daily", "weekly", "monthly"],
                "description": "How often to run the task"
            },
            "time": {
                "type": "string",
                "description": "Time to run in HH:MM format (e.g., '06:00', '18:30')"
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
        frequency = kwargs.get("frequency", "daily")
        time = kwargs.get("time", "06:00")
        # Default to Documents if not specified
        output_path = kwargs.get("output_path", "Documents")
        params = kwargs.get("parameters", {})
        
        if not prompt:
            return "Error: No prompt provided for automation."
        
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
        if not isinstance(time, str) or ":" not in time:
            return f"Error: Invalid time format '{time}'. Expected HH:MM format (e.g., '22:46')."
        
        try:
            from datetime import datetime
            datetime.strptime(time, "%H:%M")
        except ValueError:
            return f"Error: Invalid time format '{time}'. Expected HH:MM format (e.g., '22:46')."
        
        try:
            manager = AutomationManager()
            
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
                if task.time == time and task.frequency == frequency and task.enabled:
                    conflicting_task = task
                    break
            
            if conflicting_task and conflicting_task.id != (existing_task.id if existing_task else None):
                return (
                    f"❌ **Fehler: Automatisierung zur gleichen Zeit existiert bereits!**\n\n"
                    f"Es gibt bereits eine aktive Automatisierung '{conflicting_task.name}' ({conflicting_task.id}), "
                    f"die zur gleichen Zeit ({time}) mit der gleichen Frequenz ({frequency}) läuft.\n\n"
                    f"**Lösung:**\n"
                    f"- Aktualisiere die bestehende Automatisierung '{conflicting_task.name}' mit `read_automation` und dann `update_automation`\n"
                    f"- Oder wähle eine andere Zeit für die neue Automatisierung\n"
                    f"- Oder deaktiviere die bestehende Automatisierung zuerst\n\n"
                    f"**Hinweis:** Es kann nur eine Automatisierung zur gleichen Zeit ausgeführt werden."
                )
            
            # If existing task found, check if it's at the same time or different time
            if existing_task:
                same_time = existing_task.time == time and existing_task.frequency == frequency
                
                if same_time:
                    # Same time - suggest updating
                    return (
                        f"⚠️ **Automatisierung existiert bereits zur gleichen Zeit!**\n\n"
                        f"Eine Automatisierung mit dem Namen '{existing_task.name}' ({existing_task.id}) existiert bereits "
                        f"mit der gleichen Zeit ({time}) und Frequenz ({frequency}).\n\n"
                        f"**Aktuelle Einstellungen:**\n"
                        f"- Schedule: {existing_task.frequency} at {existing_task.time}\n"
                        f"- Prompt: {existing_task.prompt[:100]}{'...' if len(existing_task.prompt) > 100 else ''}\n"
                        f"- Status: {'✅ Aktiv' if existing_task.enabled else '⏸️ Deaktiviert'}\n\n"
                        f"**Um die bestehende Automatisierung zu aktualisieren:**\n"
                        f"1. Verwende `read_automation(task_id='{existing_task.id}')` um die vollständigen Details zu sehen\n"
                        f"2. Verwende dann `update_automation(task_id='{existing_task.id}', ...)` mit den neuen Werten\n\n"
                        f"**Oder:** Wenn du wirklich eine neue Automatisierung erstellen möchtest, wähle einen anderen Namen oder eine andere Zeit."
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
                            f"- Zeit: {time} ({frequency})\n\n"
                            f"**Optionen:**\n"
                            f"1. **Neue Automatisierung erstellen** (wird fortgesetzt) - da die Zeit unterschiedlich ist, ist das in Ordnung\n"
                            f"2. **Bestehende aktualisieren** - verwende `read_automation(task_id='{existing_task.id}')` und dann `update_automation`\n\n"
                            f"**Hinweis:** Da die Zeiten unterschiedlich sind ({existing_task.time} vs {time}), können beide Automatisierungen parallel laufen."
                        )
                    else:
                        # Just one similar task at different time - allow creation
                        return (
                            f"ℹ️ **Ähnliche Automatisierung existiert bereits (aber zu anderer Zeit):**\n\n"
                            f"Eine ähnliche Automatisierung '{existing_task.name}' ({existing_task.id}) existiert bereits, "
                            f"aber zu einer anderen Zeit ({existing_task.time} vs {time}).\n\n"
                            f"**Da die Zeiten unterschiedlich sind, wird die neue Automatisierung erstellt.**\n"
                            f"Beide können parallel laufen.\n\n"
                            f"**Falls du stattdessen die bestehende aktualisieren möchtest:**\n"
                            f"Verwende `read_automation(task_id='{existing_task.id}')` und dann `update_automation`."
                        )
            
            # Check cooldown BEFORE creating automation (so agent can inform user)
            # Only apply cooldown if there's a time conflict with existing automations
            can_create, error_msg = manager.check_can_create_automation(new_time=time, new_frequency=frequency)
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
            
            # Create the task
            task = AutomationTask(
                name=name,
                prompt=prompt,  # Keep for backwards compatibility
                workflow_steps=workflow_steps if workflow_steps else [],  # NEW: Structured workflow
                frequency=frequency,
                time=time,
                output_path=output_path,
                output_format=format_type,  # IMPORTANT: Set the output format!
                parameters=params
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
**Next Run:** {task.next_run}
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
                        try:
                            UI.debug(f"Automation '{task.name}' started: {result_msg}")
                        except AttributeError:
                            # Fallback if debug method doesn't exist (shouldn't happen, but defensive)
                            pass
                    except Exception as e2:
                        # Log error but don't fail the creation
                        from vaf.cli.ui import UI
                        import traceback
                        # Only log to debug, don't show in main terminal
                        try:
                            UI.debug(f"Failed to run automation immediately: {e2}")
                            UI.debug(f"Traceback: {traceback.format_exc()}")
                        except AttributeError:
                            # Fallback if debug method doesn't exist (shouldn't happen, but defensive)
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
                        if json_match:
                            content = json_match.group(0)
                        
                        try:
                            workflow_steps = json.loads(content)
                        except json.JSONDecodeError:
                            from vaf.cli.ui import UI
                            UI.event("Debug", "Workflow generation: JSON parse error, using prompt-based execution", style="dim")
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
                    if json_match:
                        content = json_match.group(0)
                    
                    try:
                        workflow_steps = json.loads(content)
                    except json.JSONDecodeError:
                        from vaf.cli.ui import UI
                        UI.event("Debug", "Workflow generation: JSON parse error, using prompt-based execution", style="dim")
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
                if json_match:
                    content = json_match.group(0)
                
                workflow_steps = json.loads(content)
                
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
            manager = AutomationManager()
            tasks = manager.list()
            
            if not tasks:
                return "No automations configured yet. Use `create_automation` to create one."
            
            result = "**📋 Scheduled Automations:**\n\n"
            
            for task in tasks:
                status = "✅ Active" if task.enabled else "⏸️ Disabled"
                result += f"• **{task.name}** ({task.id}) - {status}\n"
                result += f"  Schedule: {task.frequency} at {task.time}\n"
                result += f"  Next: {task.next_run[:16] if task.next_run else 'N/A'}\n"
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
            manager = AutomationManager()
            task = manager.get(task_id)
            
            if not task:
                return f"Error: Automation task '{task_id}' not found. Use `list_automations` to see available tasks."
            
            result = f"""**📄 Automation Details: {task.name}**

**ID:** {task.id}
**Status:** {'✅ Active' if task.enabled else '⏸️ Disabled'}
**Schedule:** {task.frequency} at {task.time}
**Next Run:** {task.next_run or 'Not scheduled'}
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
                "enum": ["hourly", "daily", "weekly", "monthly"],
                "description": "New frequency (optional)"
            },
            "time": {
                "type": "string",
                "description": "New time in HH:MM format (optional)"
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
            manager = AutomationManager()
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
                                f"❌ **Fehler: Automatisierung zur gleichen Zeit existiert bereits!**\n\n"
                                f"Es gibt bereits eine aktive Automatisierung '{existing_task.name}' ({existing_task.id}), "
                                f"die zur gleichen Zeit ({new_time}) mit der gleichen Frequenz ({new_frequency}) läuft.\n\n"
                                f"**Lösung:**\n"
                                f"- Wähle eine andere Zeit für diese Automatisierung\n"
                                f"- Oder deaktiviere die andere Automatisierung '{existing_task.name}' zuerst\n\n"
                                f"**Hinweis:** Es kann nur eine Automatisierung zur gleichen Zeit ausgeführt werden."
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
**Next Run:** {updated_task.next_run}
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
            manager = AutomationManager()
            task = manager.get(task_id)
            
            if not task:
                return f"Error: Automation with ID '{task_id}' not found."
            
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
            manager = AutomationManager()
            
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
**Next Run:** {task.next_run}

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
            manager = AutomationManager()
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
