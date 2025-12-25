"""
VAF Automation System - Time-based task automation
Cross-Platform: Windows, macOS, Linux

Features:
- Schedule tasks at specific times (daily, weekly, hourly)
- Model can create automations via coding_agent
- Clarification prompts for incomplete tasks
- Animated terminal execution
"""
import os
import sys
import json
import uuid
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum

# Cross-platform scheduler
try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class Frequency(str, Enum):
    """Task execution frequency."""
    ONCE = "once"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class AutomationTask:
    """A scheduled automation task."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: str = ""
    prompt: str = ""  # The prompt to send to VAF
    frequency: str = "daily"
    time: str = "06:00"  # HH:MM format
    weekday: Optional[str] = None  # For weekly: monday, tuesday, etc.
    day: Optional[int] = None  # For monthly: 1-31
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    output_path: Optional[str] = None  # Where to save results
    output_format: str = "markdown"  # markdown, json, txt
    
    # Task parameters (filled by clarification)
    parameters: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "AutomationTask":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def calculate_next_run(self) -> datetime:
        """Calculate the next execution time."""
        now = datetime.now()
        hour, minute = map(int, self.time.split(":"))
        
        if self.frequency == Frequency.ONCE:
            # Already scheduled, return as-is or None
            if self.next_run:
                return datetime.fromisoformat(self.next_run)
            return now
        
        elif self.frequency == Frequency.HOURLY:
            next_time = now.replace(minute=minute, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(hours=1)
            return next_time
        
        elif self.frequency == Frequency.DAILY:
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(days=1)
            return next_time
        
        elif self.frequency == Frequency.WEEKLY:
            weekdays = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6
            }
            target_day = weekdays.get(self.weekday.lower(), 0) if self.weekday else 0
            days_ahead = target_day - now.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            next_time += timedelta(days=days_ahead)
            return next_time
        
        elif self.frequency == Frequency.MONTHLY:
            target_day = self.day or 1
            next_time = now.replace(day=target_day, hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                # Move to next month
                if now.month == 12:
                    next_time = next_time.replace(year=now.year + 1, month=1)
                else:
                    next_time = next_time.replace(month=now.month + 1)
            return next_time
        
        return now


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOMATION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class AutomationManager:
    """Manages automation tasks and scheduling."""
    
    def __init__(self, storage_dir: str = None):
        if storage_dir:
            self.storage_dir = Path(storage_dir)
        else:
            self.storage_dir = Path.home() / ".vaf" / "automations"
        
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Trash directory (system-independent)
        self.trash_dir = self.storage_dir / "trash"
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        
        self.tasks: Dict[str, AutomationTask] = {}
        self._scheduler_thread: Optional[threading.Thread] = None
        self._running = False
        self._create_readme()
        self._load_tasks()
    
    def _create_readme(self):
        """Create README in automations folder if it doesn't exist."""
        readme_path = self.storage_dir / "README.md"
        
        if readme_path.exists():
            return
        
        readme_content = """# 🤖 VAF Automations

This folder contains your scheduled automation tasks.

## How It Works

1. **Create**: Use `vaf automation create` or ask VAF to create one
2. **View**: Check Settings → ⚡ Automations or `vaf automation list`
3. **Start**: Run `vaf automation start` to activate the scheduler
4. **Manage**: Enable/disable in Settings, delete manually

## Files

- `*.json` - Automation task definitions
- Each file = one scheduled task

## Task Format

```json
{
  "id": "abc123",
  "name": "daily_news",
  "prompt": "Create a summary of today's tech news",
  "frequency": "daily",
  "time": "06:00",
  "enabled": true,
  "output_path": "~/Desktop"
}
```

## Frequencies

- `hourly` - Every hour at :MM
- `daily` - Every day at HH:MM
- `weekly` - Every week on weekday at HH:MM
- `monthly` - Every month on day at HH:MM

## Commands

```bash
vaf automation list          # View all
vaf automation create        # Create new (interactive)
vaf automation run <id>      # Run manually
vaf automation start         # Start scheduler daemon
vaf automation enable <id>   # Enable task
vaf automation disable <id>  # Disable task
vaf automation delete <id>   # Delete task
```

## Tips

- Automations are **enabled by default** when created
- Disable in Settings without deleting
- To delete: remove the .json file or use CLI
- Scheduler must be running for timed execution

---
*Generated by VAF - Veyllo Agentic Framework*
"""
        readme_path.write_text(readme_content, encoding='utf-8')
    
    def _load_tasks(self):
        """Load all tasks from storage."""
        for filepath in self.storage_dir.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                task = AutomationTask.from_dict(data)
                self.tasks[task.id] = task
            except Exception:
                continue
    
    def _save_task(self, task: AutomationTask):
        """Save a task to storage."""
        filepath = self.storage_dir / f"{task.id}.json"
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(task.to_dict(), f, indent=2, ensure_ascii=False)
    
    def create(self, task: AutomationTask) -> AutomationTask:
        """Create a new automation task."""
        task.next_run = task.calculate_next_run().isoformat()
        self.tasks[task.id] = task
        self._save_task(task)
        
        # If scheduler is already running, schedule this task immediately
        if self._running:
            self._schedule_task(task)
        
        return task
    
    def update(self, task_id: str, **kwargs) -> Optional[AutomationTask]:
        """Update an existing task."""
        if task_id not in self.tasks:
            return None
        
        task = self.tasks[task_id]
        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)
        
        task.next_run = task.calculate_next_run().isoformat()
        self._save_task(task)
        return task
    
    def delete(self, task_id: str, permanent: bool = False) -> bool:
        """Delete a task (moves to trash by default, or permanently if specified)."""
        if task_id not in self.tasks:
            return False
        
        task = self.tasks[task_id]
        filepath = self.storage_dir / f"{task_id}.json"
        trash_path = self.trash_dir / f"{task_id}.json"
        
        if filepath.exists():
            if permanent:
                # Permanent deletion
                filepath.unlink()
            else:
                # Move to trash
                import shutil
                shutil.move(str(filepath), str(trash_path))
        
        del self.tasks[task_id]
        return True
    
    def move_to_trash(self, task_id: str) -> bool:
        """Move a task to trash (recoverable deletion)."""
        return self.delete(task_id, permanent=False)
    
    def restore_from_trash(self, task_id: str) -> bool:
        """Restore a task from trash."""
        trash_path = self.trash_dir / f"{task_id}.json"
        if not trash_path.exists():
            return False
        
        try:
            # Load task from trash
            with open(trash_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            task = AutomationTask.from_dict(data)
            
            # Move back to storage
            filepath = self.storage_dir / f"{task_id}.json"
            import shutil
            shutil.move(str(trash_path), str(filepath))
            
            # Add back to tasks
            self.tasks[task_id] = task
            return True
        except Exception:
            return False
    
    def list_trash(self) -> List[AutomationTask]:
        """List all tasks in trash."""
        tasks = []
        for filepath in self.trash_dir.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                task = AutomationTask.from_dict(data)
                tasks.append(task)
            except Exception:
                continue
        return tasks
    
    def empty_trash(self) -> int:
        """Permanently delete all tasks in trash. Returns number of deleted tasks."""
        count = 0
        for filepath in self.trash_dir.glob("*.json"):
            try:
                filepath.unlink()
                count += 1
            except Exception:
                continue
        return count
    
    def get(self, task_id: str) -> Optional[AutomationTask]:
        """Get a task by ID."""
        return self.tasks.get(task_id)
    
    def list(self, enabled_only: bool = False) -> List[AutomationTask]:
        """List all tasks."""
        tasks = list(self.tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        return sorted(tasks, key=lambda t: t.next_run or "")
    
    def run_task(self, task: AutomationTask, callback: Callable = None, new_terminal: bool = True) -> str:
        """
        Execute an automation task.
        
        Args:
            task: The automation task to run
            callback: Optional callback for progress updates
            new_terminal: If True, run in a new terminal window (default: True)
        """
        from vaf.cli.ui import UI
        from vaf.core.platform import Platform
        import sys
        import subprocess
        
        UI.event("Automation", f"Running: {task.name}", style="info")
        
        # If new_terminal is True, open in new terminal window
        if new_terminal:
            # Build command to run automation
            # Use 'vaf automation run <id>' command
            vaf_cmd = f'vaf automation run {task.id}'
            
            # Try to open in new terminal
            title = f"VAF Automation: {task.name}"
            if Platform.open_new_terminal(vaf_cmd, title=title):
                UI.success(f"Automation started in new terminal window: {task.name}")
                return f"Automation '{task.name}' started in new terminal window"
            else:
                # Fallback: run in current process if new terminal fails
                UI.warning(f"Could not open new terminal, running in background...")
        
        # Build the prompt with parameters
        prompt = task.prompt
        for key, value in task.parameters.items():
            prompt = prompt.replace(f"{{{key}}}", str(value))
        
        result = ""
        
        try:
            # Import agent and run
            from vaf.core.agent import Agent
            
            agent = Agent(verbose=False)
            agent.load_model()
            agent.init_chat()
            
            # Capture response
            response_parts = []
            def capture(text):
                response_parts.append(text)
                if callback:
                    callback(text)
            
            agent.chat_step(prompt, stream_callback=capture)
            result = "".join(response_parts)
            
            # Save output if path specified
            if task.output_path:
                output_path = Path(task.output_path).expanduser()
                
                # Create filename with date
                date_str = datetime.now().strftime("%Y-%m-%d")
                if task.output_format == "markdown":
                    filename = f"{task.name}_{date_str}.md"
                    content = f"# {task.name}\n\n*Generated: {datetime.now().isoformat()}*\n\n{result}"
                elif task.output_format == "json":
                    filename = f"{task.name}_{date_str}.json"
                    content = json.dumps({"task": task.name, "date": date_str, "content": result}, indent=2)
                else:
                    filename = f"{task.name}_{date_str}.txt"
                    content = result
                
                output_file = output_path / filename
                output_path.mkdir(parents=True, exist_ok=True)
                output_file.write_text(content, encoding='utf-8')
                
                UI.success(f"Output saved: {output_file}")
            
            # Update task
            task.last_run = datetime.now().isoformat()
            task.next_run = task.calculate_next_run().isoformat()
            self._save_task(task)
            
            agent.shutdown()
            
        except Exception as e:
            result = f"Error: {e}"
            UI.error(f"Automation failed: {e}")
        
        return result
    
    def start_scheduler(self):
        """Start the background scheduler."""
        if not HAS_SCHEDULE:
            raise ImportError("'schedule' package required. Install: pip install schedule")
        
        if self._running:
            return
        
        self._running = True
        
        def scheduler_loop():
            while self._running:
                schedule.run_pending()
                time.sleep(30)  # Check every 30 seconds
        
        # Schedule all enabled tasks
        for task in self.list(enabled_only=True):
            self._schedule_task(task)
        
        self._scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
        self._scheduler_thread.start()
    
    def stop_scheduler(self):
        """Stop the background scheduler."""
        self._running = False
        schedule.clear()
    
    def _schedule_task(self, task: AutomationTask):
        """Add a task to the scheduler."""
        if not task.enabled:
            return
        
        # Run in new terminal window by default
        job_func = lambda t=task: self.run_task(t, new_terminal=True)
        
        if task.frequency == Frequency.HOURLY:
            schedule.every().hour.at(f":{task.time.split(':')[1]}").do(job_func)
        
        elif task.frequency == Frequency.DAILY:
            schedule.every().day.at(task.time).do(job_func)
        
        elif task.frequency == Frequency.WEEKLY:
            weekday = task.weekday or "monday"
            getattr(schedule.every(), weekday).at(task.time).do(job_func)
        
        elif task.frequency == Frequency.MONTHLY:
            # Monthly is trickier - check daily and run if day matches
            def monthly_check(t=task):
                if datetime.now().day == (t.day or 1):
                    self.run_task(t, new_terminal=True)
            schedule.every().day.at(task.time).do(monthly_check)


# ═══════════════════════════════════════════════════════════════════════════════
# CLARIFICATION HELPER
# ═══════════════════════════════════════════════════════════════════════════════

class AutomationClarifier:
    """Helps clarify incomplete automation requests."""
    
    # Required parameters for common task types
    REQUIRED_PARAMS = {
        "weather": ["city"],
        "news": ["category"],  # tech, politics, sports, all
        "stock": ["symbol"],
        "reminder": ["message"],
        "email_summary": ["email_account"],
        "backup": ["source_path", "destination_path"],
    }
    
    # Questions to ask for each parameter
    QUESTIONS = {
        "city": "Für welche Stadt soll das Wetter abgerufen werden?",
        "category": "Welche Nachrichten-Kategorie? (tech, politik, sport, wirtschaft, alle)",
        "symbol": "Welches Aktiensymbol? (z.B. AAPL, GOOGL, MSFT)",
        "message": "Was soll die Erinnerung sagen?",
        "email_account": "Welcher E-Mail-Account soll zusammengefasst werden?",
        "source_path": "Welcher Ordner soll gesichert werden?",
        "destination_path": "Wohin soll die Sicherung gespeichert werden?",
        "time": "Um welche Uhrzeit? (Format: HH:MM, z.B. 06:00)",
        "frequency": "Wie oft? (täglich, wöchentlich, monatlich)",
        "output_path": "Wohin soll das Ergebnis gespeichert werden? (z.B. ~/Desktop)",
    }
    
    @classmethod
    def detect_task_type(cls, prompt: str) -> Optional[str]:
        """Detect the type of automation from the prompt."""
        prompt_lower = prompt.lower()
        
        if any(w in prompt_lower for w in ["wetter", "weather", "temperatur"]):
            return "weather"
        if any(w in prompt_lower for w in ["nachrichten", "news", "headlines"]):
            return "news"
        if any(w in prompt_lower for w in ["aktie", "stock", "börse"]):
            return "stock"
        if any(w in prompt_lower for w in ["erinner", "remind", "alarm"]):
            return "reminder"
        if any(w in prompt_lower for w in ["email", "mail", "inbox"]):
            return "email_summary"
        if any(w in prompt_lower for w in ["backup", "sicher", "kopie"]):
            return "backup"
        
        return None
    
    @classmethod
    def get_missing_params(cls, task_type: str, existing_params: Dict) -> List[str]:
        """Get list of missing required parameters."""
        required = cls.REQUIRED_PARAMS.get(task_type, [])
        return [p for p in required if p not in existing_params]
    
    @classmethod
    def extract_params(cls, prompt: str) -> Dict[str, Any]:
        """Extract parameters from a prompt."""
        import re
        params = {}
        prompt_lower = prompt.lower()
        
        # Extract city (common German/international cities)
        cities = ["berlin", "hamburg", "münchen", "köln", "frankfurt", "düsseldorf",
                  "stuttgart", "london", "paris", "new york", "tokyo", "wien", "zürich"]
        for city in cities:
            if city in prompt_lower:
                params["city"] = city.title()
                break
        
        # Extract time (HH:MM pattern)
        time_match = re.search(r'(\d{1,2})[:\.](\d{2})', prompt)
        if time_match:
            hour = int(time_match.group(1))
            minute = time_match.group(2)
            params["time"] = f"{hour:02d}:{minute}"
        
        # Extract frequency
        if any(w in prompt_lower for w in ["täglich", "daily", "jeden tag"]):
            params["frequency"] = "daily"
        elif any(w in prompt_lower for w in ["wöchentlich", "weekly", "jede woche"]):
            params["frequency"] = "weekly"
        elif any(w in prompt_lower for w in ["stündlich", "hourly", "jede stunde"]):
            params["frequency"] = "hourly"
        elif any(w in prompt_lower for w in ["monatlich", "monthly", "jeden monat"]):
            params["frequency"] = "monthly"
        
        # Extract output path
        if "desktop" in prompt_lower:
            params["output_path"] = str(Path.home() / "Desktop")
        elif "dokumente" in prompt_lower or "documents" in prompt_lower:
            params["output_path"] = str(Path.home() / "Documents")
        elif "downloads" in prompt_lower:
            params["output_path"] = str(Path.home() / "Downloads")
        
        # Extract news category
        if "tech" in prompt_lower:
            params["category"] = "tech"
        elif "politik" in prompt_lower or "politics" in prompt_lower:
            params["category"] = "politics"
        elif "sport" in prompt_lower:
            params["category"] = "sports"
        elif "wirtschaft" in prompt_lower or "business" in prompt_lower:
            params["category"] = "business"
        
        return params
    
    @classmethod
    def build_clarification_prompt(cls, task_type: str, missing_params: List[str]) -> str:
        """Build a clarification prompt for missing parameters."""
        questions = []
        for param in missing_params:
            if param in cls.QUESTIONS:
                questions.append(f"• {cls.QUESTIONS[param]}")
        
        if questions:
            return "Um die Automatisierung zu erstellen, brauche ich noch ein paar Infos:\n\n" + "\n".join(questions)
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

import typer

automation_app = typer.Typer(help="Manage scheduled automations")

_manager: Optional[AutomationManager] = None

def get_manager() -> AutomationManager:
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager


@automation_app.command("list")
def list_automations():
    """List all automation tasks."""
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    manager = get_manager()
    tasks = manager.list()
    
    if not tasks:
        console.print("[yellow]No automations configured.[/yellow]")
        console.print("\n[dim]Create one with: vaf automation create[/dim]")
        return
    
    table = Table(title="⚡ Scheduled Automations", show_header=True)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Frequency")
    table.add_column("Time")
    table.add_column("Next Run")
    table.add_column("Status")
    
    for task in tasks:
        status = "[green]●[/green] Active" if task.enabled else "[red]○[/red] Disabled"
        next_run = task.next_run[:16] if task.next_run else "-"
        
        table.add_row(
            task.id,
            task.name[:20],
            task.frequency,
            task.time,
            next_run,
            status
        )
    
    console.print(table)


@automation_app.command("create")
def create_automation(
    name: str = typer.Option(..., "--name", "-n", prompt="Task name"),
    prompt: str = typer.Option(..., "--prompt", "-p", prompt="What should VAF do?"),
    frequency: str = typer.Option("daily", "--frequency", "-f", help="daily, weekly, hourly, monthly"),
    time: str = typer.Option("06:00", "--time", "-t", help="Execution time (HH:MM)"),
    output: str = typer.Option(None, "--output", "-o", help="Output directory")
):
    """Create a new automation task."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    # Extract and clarify parameters
    clarifier = AutomationClarifier()
    task_type = clarifier.detect_task_type(prompt)
    params = clarifier.extract_params(prompt)
    
    if task_type:
        missing = clarifier.get_missing_params(task_type, params)
        if missing:
            console.print(f"\n[yellow]{clarifier.build_clarification_prompt(task_type, missing)}[/yellow]\n")
            
            for param in missing:
                question = clarifier.QUESTIONS.get(param, f"Value for {param}?")
                value = typer.prompt(question)
                params[param] = value
    
    # Create task
    task = AutomationTask(
        name=name,
        prompt=prompt,
        frequency=frequency,
        time=time,
        output_path=output or str(Path.home() / "Desktop"),
        parameters=params
    )
    
    task = manager.create(task)
    
    console.print(f"\n[green]✓ Automation created![/green]")
    console.print(f"  [dim]ID:[/dim] {task.id}")
    console.print(f"  [dim]Next run:[/dim] {task.next_run}")
    console.print(f"\n[dim]Start scheduler with: vaf automation start[/dim]")


@automation_app.command("run")
def run_automation(
    task_id: str = typer.Argument(..., help="Task ID to run")
):
    """Manually run an automation task."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    task = manager.get(task_id)
    if not task:
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    
    console.print(f"\n[cyan]⚡ Running: {task.name}[/cyan]\n")
    
    def print_output(text):
        console.print(text, end="")
    
    # Don't open new terminal when called directly from CLI
    result = manager.run_task(task, callback=print_output, new_terminal=False)
    console.print("\n")


@automation_app.command("delete")
def delete_automation(
    task_id: str = typer.Argument(..., help="Task ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation")
):
    """Delete an automation task."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    task = manager.get(task_id)
    if not task:
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    
    if not force:
        confirm = typer.confirm(f"Delete automation '{task.name}'?")
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            return
    
    manager.delete(task_id)
    console.print(f"[green]✓ Deleted: {task.name}[/green]")


@automation_app.command("start")
def start_scheduler():
    """Start the automation scheduler daemon."""
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    
    console = Console()
    
    if not HAS_SCHEDULE:
        console.print("[red]Missing dependency: schedule[/red]")
        console.print("[dim]Install with: pip install schedule[/dim]")
        raise typer.Exit(1)
    
    manager = get_manager()
    tasks = manager.list(enabled_only=True)
    
    if not tasks:
        console.print("[yellow]No enabled automations to run.[/yellow]")
        return
    
    console.print(f"\n[cyan]⚡ Starting VAF Automation Scheduler[/cyan]")
    console.print(f"[dim]Running {len(tasks)} task(s). Press Ctrl+C to stop.[/dim]\n")
    
    for task in tasks:
        console.print(f"  • {task.name} @ {task.time} ({task.frequency})")
    
    console.print()
    
    manager.start_scheduler()
    
    try:
        # Keep running with status updates
        while True:
            time.sleep(60)
            # Could add live status display here
    except KeyboardInterrupt:
        manager.stop_scheduler()
        console.print("\n[yellow]Scheduler stopped.[/yellow]")


@automation_app.command("enable")
def enable_automation(task_id: str = typer.Argument(..., help="Task ID")):
    """Enable an automation task."""
    manager = get_manager()
    task = manager.update(task_id, enabled=True)
    if task:
        print(f"✓ Enabled: {task.name}")
    else:
        print(f"Task not found: {task_id}")


@automation_app.command("disable")
def disable_automation(task_id: str = typer.Argument(..., help="Task ID")):
    """Disable an automation task."""
    manager = get_manager()
    task = manager.update(task_id, enabled=False)
    if task:
        print(f"✓ Disabled: {task.name}")
    else:
        print(f"Task not found: {task_id}")

