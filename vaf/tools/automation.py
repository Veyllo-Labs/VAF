"""
VAF Automation Tool - Allow the model to create scheduled tasks
"""
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

The automation will run even when VAF is not actively in use (requires scheduler daemon)."""
    
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
                "description": "Where to save results (e.g., '~/Desktop', '~/Documents')"
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
        output_path = kwargs.get("output_path", "~/Desktop")
        params = kwargs.get("parameters", {})
        
        if not prompt:
            return "Error: No prompt provided for automation."
        
        try:
            manager = AutomationManager()
            
            # Create the task
            task = AutomationTask(
                name=name,
                prompt=prompt,
                frequency=frequency,
                time=time,
                output_path=output_path,
                parameters=params
            )
            
            task = manager.create(task)
            
            # Check if the scheduled time has already passed today
            from datetime import datetime
            now = datetime.now()
            task_time = datetime.strptime(task.time, "%H:%M").time()
            current_time = now.time()
            
            # If time has passed and it's a daily task, run it immediately
            should_run_now = False
            if task.frequency == "daily" and current_time >= task_time:
                # Time has passed today, run immediately
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
                result += f"""

⚡ **Running immediately in new terminal** (scheduled time {task.time} has passed today)"""
                # Run the task in a new terminal window
                try:
                    manager.run_task(task, new_terminal=True)
                except Exception as e:
                    # Fallback: try without new terminal
                    try:
                        manager.run_task(task, new_terminal=False)
                    except Exception:
                        pass  # Errors are logged by run_task
            
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


class ListAutomationsTool(BaseTool):
    """Tool for listing automation tasks."""
    
    name = "list_automations"
    description = "List all scheduled automation tasks."
    
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
                result += f"• **{task.name}** ({task.id})\n"
                result += f"  Schedule: {task.frequency} at {task.time}\n"
                result += f"  Next: {task.next_run[:16] if task.next_run else 'N/A'}\n"
                result += f"  Status: {status}\n\n"
            
            return result
            
        except Exception as e:
            return f"Error listing automations: {e}"


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
