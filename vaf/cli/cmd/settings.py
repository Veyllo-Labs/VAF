import typer
import inquirer
from rich.table import Table
from rich.text import Text
from rich.console import Console
from rich.terminal_theme import TerminalTheme
from huggingface_hub import HfApi, hf_hub_download
from vaf.core.config import Config
from vaf.cli.ui import UI
import os
import sys
import time
from io import StringIO

app = typer.Typer()

def search_models_menu():
    while True:
        UI.clear()
        query = UI.console.input("\n[bold cyan]Search Hugging Face GGUF (Enter empty to cancel):[/bold cyan] ")
        if not query.strip(): break
        
        UI.event("System", f"Searching for '{query}'...", style="dim")
        try:
            api = HfApi()
            models = api.list_models(search=query, filter="gguf", limit=5, sort="downloads", direction=-1)
            
            choices = []
            for m in models:
                choices.append((f"{m.modelId} ({m.downloads} downloads)", m.modelId))
            
            if not choices:
                UI.error("No GGUF models found.")
                continue

            choices.append(("Cancel", None))

            q = [inquirer.List('model', message="Select a model to download", choices=choices)]
            answers = inquirer.prompt(q)
            
            if not answers or not answers['model']:
                continue
                
            download_model_flow(answers['model'])
            
        except Exception as e:
            UI.error(f"Search failed: {e}")

def download_model_flow(repo_id: str):
    try:
        api = HfApi()
        files = api.list_repo_files(repo_id=repo_id)
        gguf_files = [f for f in files if f.endswith('.gguf')]
        
        choices = gguf_files
        if not choices:
            UI.error("No .gguf files found in this repo.")
            return

        q = [inquirer.List('file', message="Select quantization file", choices=choices)]
        answers = inquirer.prompt(q)
        
        if not answers: return

        filename = answers['file']
        
        UI.event("System", f"Downloading {filename}...", style="warning")
        
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        models_dir = os.path.join(base_dir, "models")
        
        hf_hub_download(repo_id=repo_id, filename=filename, local_dir=models_dir)
        
        UI.event("Success", "Download verified.", style="success")
        
        if typer.confirm("Set as active model?"):
            Config.set("model", f"{repo_id}/{filename}")
            UI.success(f"Active model set to {repo_id}/{filename}")
            
    except Exception as e:
        UI.error(str(e))


def set_context_limit_menu():
    current = Config.get("n_ctx", 8192)
    UI.clear()
    UI.panel(f"Current Limit: {current}", title="Context Window Settings", style="highlight")
    
    questions = [
        inquirer.List('ctx',
                      message="Select Context Limit (Tokens)",
                      choices=[
                          ('4096 (Speed)', 4096),
                          ('8192 (Balanced)', 8192),
                          ('16384 (Large)', 16384),
                          ('Custom...', 'custom'),
                          ('Back', 'back'),
                      ],
        ),
    ]
    answers = inquirer.prompt(questions)
    if not answers: return

    selection = answers['ctx']
    
    if selection == 'back':
        return
        
    final_val = selection
    
    if selection == 'custom':
        while True:
            val = UI.console.input("[bold cyan]Enter custom limit (e.g. 2048): [/bold cyan]")
            if not val: return
            try:
                final_val = int(val)
                if final_val < 512:
                    UI.error("Too small. Min 512.")
                    continue
                break
            except ValueError:
                UI.error("Invalid number.")
    
    Config.set("n_ctx", final_val)
    UI.success(f"Context limit set to {final_val}")
    UI.console.input("[dim]Press Enter to continue...[/dim]")


def select_model_menu():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    models_dir = os.path.join(base_dir, "models")
    
    if not os.path.exists(models_dir):
        UI.error(f"Models directory not found: {models_dir}")
        UI.console.input("[dim]Press Enter to continue...[/dim]")
        return

    files = [f for f in os.listdir(models_dir) if f.endswith(".gguf")]
    if not files:
        UI.error("No models found in models/ directory.")
        UI.console.input("[dim]Press Enter to continue...[/dim]")
        return

    current = Config.get("model")
    current_name = current.split("/")[-1] if "/" in current else current
    if not current_name.endswith(".gguf"): current_name += ".gguf"

    choices = []
    for f in files:
        label = f
        if f == current_name or (current_name in f):
             label = f"{f} (Active)"
        choices.append(label)
    
    choices.append("Cancel")

    UI.clear()
    UI.panel("Select Model to Load", style="highlight")

    questions = [
        inquirer.List('model',
                      message="Available Models",
                      choices=choices,
        ),
    ]
    answers = inquirer.prompt(questions)
    if not answers or answers['model'] == "Cancel":
        return

    selected = answers['model'].replace(" (Active)", "")
    
    Config.set("model", selected)
    UI.success(f"Model switched to: {selected}")
    UI.console.input("[dim]Press Enter to continue...[/dim]")


def _get_color_box(color: str) -> str:
    """Create a small color box by rendering with Rich and extracting ANSI codes."""
    # Use Rich to render the color box, then extract the ANSI string
    # This ensures compatibility with inquirer
    console = Console(force_terminal=True, color_system="truecolor", file=StringIO())
    console.print(f"[{color} on {color}]██[/{color} on {color}]", end="")
    ansi_string = console.file.getvalue()
    console.file.close()
    
    return ansi_string


def select_theme_menu():
    """Select and apply a theme."""
    try:
        from vaf.cli.themes import ThemeManager
    except ImportError:
        UI.error("Theme manager not available.")
        return
    
    UI.clear()
    
    current = Config.get("theme", "vaf")
    themes = ThemeManager.list_themes()
    
    choices = []
    for theme_name in themes:
        theme = ThemeManager.get_theme(theme_name)
        primary_color = theme.get("primary", "#00d4ff")
        color_box = _get_color_box(primary_color)
        label = f"{color_box} {theme_name}"
        if theme_name == current:
            label = f"{label} (Active)"
        # Store as tuple: (display_label, actual_value)
        choices.append((label, theme_name))
    
    choices.append(("Cancel", "cancel"))
    
    UI.panel(f"Current Theme: {current}", title="Theme Settings", style="highlight")
    
    questions = [
        inquirer.List('theme',
                      message="Select Theme",
                      choices=choices,
        ),
    ]
    answers = inquirer.prompt(questions)
    
    if not answers or answers['theme'] == "cancel":
        return
    
    selected = answers['theme']
    
    if ThemeManager.set_theme(selected):
        Config.set("theme", selected)
        UI.success(f"Theme changed to: {selected}")
    else:
        UI.error(f"Failed to set theme: {selected}")
    
    UI.console.input("[dim]Press Enter to continue...[/dim]")


def show_tools_menu(agent):
    """Show ALL tools - both Main Agent and Sub-Agent tools."""
    UI.clear()
    UI.print("[bold cyan]All Available Tools[/bold cyan]\n")
    
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Available To", style="dim")
    
    # 1. Main Agent tools
    if agent and hasattr(agent, "tools"):
        for name, tool in agent.tools.items():
            t_type = "Main Agent"
            if "CodingAgent" in str(type(tool)) or "Librarian" in str(type(tool)): 
                t_type = "Sub-Agent Delegator"
            if "WebSearch" in str(type(tool)): 
                t_type = "Main Agent (Research)"
            if "WebFetch" in str(type(tool)):
                t_type = "Main Agent (Research)"
            
            desc = tool.description[:55] + "..." if len(tool.description) > 55 else tool.description
            table.add_row(name, desc, t_type)
    
    # 2. Sub-Agent only tools (manually list them)
    sub_agent_tools = [
        ("write_file", "Write content to a file", "Coder Sub-Agent"),
        ("read_file", "Read a file's contents", "Coder Sub-Agent"),
        ("list_files", "List files in directory", "Coder Sub-Agent"),
        ("bash", "Execute shell commands (build, test, git)", "Coder Sub-Agent"),
        ("codesearch", "Search for code patterns/symbols", "Coder Sub-Agent"),
        ("batch", "Execute multiple tools in parallel", "Coder Sub-Agent"),
    ]
    
    for name, desc, available_to in sub_agent_tools:
        table.add_row(f"[dim]{name}[/dim]", f"[dim]{desc}[/dim]", f"[yellow]{available_to}[/yellow]")
    
    UI.console.print(table)
    UI.print("\n[dim]Note: Dim tools are only available to Sub-Agents[/dim]")
    UI.console.input("\n[dim]Press Enter to return...[/dim]")


def show_theme_menu():
    """Select UI theme."""
    try:
        from vaf.cli.themes import ThemeManager
    except ImportError:
        UI.error("Theme system not available.")
        UI.console.input("[dim]Press Enter to continue...[/dim]")
        return
    
    UI.clear()
    
    current = ThemeManager.current()
    themes = ThemeManager.list_themes()
    
    # Show color preview before menu
    UI.print("\n[bold]Theme Color Preview:[/bold]")
    preview_line = ""
    for theme_name in themes[:12]:  # Show first 12 themes
        theme = ThemeManager.get_theme(theme_name)
        primary_color = theme.get("primary", "#00d4ff")
        color_box = _get_color_box(primary_color)
        preview_line += f"{color_box} "
    UI.console.print(preview_line)
    UI.print()  # Empty line
    
    # Build choices with theme names (color boxes shown above)
    choices = []
    for theme_name in themes:
        theme = ThemeManager.get_theme(theme_name)
        label = theme.get('name', theme_name.title())
        if theme_name == current:
            label = f"{label} (Current)"
        choices.append((label, theme_name))
    
    choices.append(("Back", "back"))
    
    UI.panel(f"Current Theme: {current}", title="Theme Settings", style="highlight")
    
    questions = [
        inquirer.List('theme',
                      message="Select Theme",
                      choices=choices,
        ),
    ]
    answers = inquirer.prompt(questions)
    
    if not answers or answers['theme'] == 'back':
        return
    
    selected = answers['theme']
    
    if ThemeManager.set_theme(selected):
        # Save to config
        Config.set("theme", selected)
        
        # Show preview
        theme = ThemeManager.get_theme(selected)
        UI.clear()
        UI.print(f"\n[bold]Theme Preview: {theme.get('name', selected)}[/bold]\n")
        UI.console.print(f"  [{theme['primary']}]Primary[/{theme['primary']}] | " +
                        f"[{theme['secondary']}]Secondary[/{theme['secondary']}] | " +
                        f"[{theme['accent']}]Accent[/{theme['accent']}]")
        UI.console.print(f"  [{theme['success']}]Success[/{theme['success']}] | " +
                        f"[{theme['warning']}]Warning[/{theme['warning']}] | " +
                        f"[{theme['error']}]Error[/{theme['error']}]")
        UI.success(f"\nTheme set to: {selected}")
    else:
        UI.error(f"Failed to set theme: {selected}")
    
    UI.console.input("\n[dim]Press Enter to continue...[/dim]")


def show_automations_menu():
    """Manage scheduled automations."""
    from rich.table import Table
    
    try:
        from vaf.core.automation import AutomationManager
    except ImportError:
        UI.error("Automation system not available.")
        UI.console.input("[dim]Press Enter to continue...[/dim]")
        return
    
    manager = AutomationManager()
    
    while True:
        UI.clear()
        
        tasks = manager.list()
        
        UI.print("\n[bold cyan]⚡ Scheduled Automations[/bold cyan]\n")
        
        if not tasks:
            UI.print("[yellow]No automations configured yet.[/yellow]")
            UI.print(f"\n[dim]Storage: {manager.storage_dir}[/dim]")
            UI.print("[dim]Create with: vaf automation create[/dim]")
        else:
            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("#", style="dim", width=3)
            table.add_column("Name", style="cyan")
            table.add_column("Schedule")
            table.add_column("Next Run")
            table.add_column("Status")
            
            for i, task in enumerate(tasks, 1):
                status = "[green]● Enabled[/green]" if task.enabled else "[red]○ Disabled[/red]"
                next_run = task.next_run[:16] if task.next_run else "-"
                schedule = f"{task.frequency} @ {task.time}"
                
                table.add_row(str(i), task.name, schedule, next_run, status)
            
            UI.console.print(table)
            UI.print(f"\n[dim]Storage: {manager.storage_dir}[/dim]")
        
        # Menu options
        choices = []
        
        if tasks:
            choices.append(('Enable/Disable Automation', 'toggle'))
        
        choices.extend([
            ('Refresh List', 'refresh'),
            ('Open Automations Folder', 'open_folder'),
            ('Back', 'back'),
        ])
        
        questions = [
            inquirer.List('action',
                          message="Action",
                          choices=choices,
            ),
        ]
        answers = inquirer.prompt(questions)
        
        if not answers or answers['action'] == 'back':
            break
        
        action = answers['action']
        
        if action == 'refresh':
            continue
        
        elif action == 'open_folder':
            # Cross-platform folder open
            import subprocess
            folder = str(manager.storage_dir)
            
            if sys.platform == 'win32':
                subprocess.run(['explorer', folder], check=False)
            elif sys.platform == 'darwin':
                subprocess.run(['open', folder], check=False)
            else:  # Linux
                subprocess.run(['xdg-open', folder], check=False)
            
            UI.success(f"Opened: {folder}")
            time.sleep(1)
        
        elif action == 'toggle':
            if not tasks:
                continue
            
            # Select which automation to toggle
            task_choices = [(f"{t.name} ({'Enabled' if t.enabled else 'Disabled'})", t.id) for t in tasks]
            task_choices.append(('Cancel', None))
            
            q = [inquirer.List('task_id', message="Select automation", choices=task_choices)]
            ans = inquirer.prompt(q)
            
            if ans and ans['task_id']:
                task = manager.get(ans['task_id'])
                if task:
                    new_status = not task.enabled
                    manager.update(task.id, enabled=new_status)
                    status_str = "Enabled" if new_status else "Disabled"
                    UI.success(f"{task.name}: {status_str}")
                    time.sleep(1)


def show_about():
    UI.clear()
    
    # VAF Robot Face Mascot (compact)
    mascot = r"""
&&&&&&&&&&$$x+++x$$&&&&&&&&&&
&&&&&&$+::;+xxxxx+;::+$&&&&&&
&&&$X:;xxxxxxxxxxxxxxx::X&&&&
&&$;;xxxxxxxxxxxxxxxxxxx:;$&&
&$.+xxxxxxxxxxxxxxxxxxxxx;:$&
$;;xxxx+++xxxxxxxx+++xxxxx:;$
X:xxx;.    .xxxx:     :xxxx:X
X:xxx.      :xx+.     .+xxx:X
X:xxx;.    .xxxx:     :xxxx:X
$;+xxxx+++xxxxxxxx+++xxxxx;;$
&$.+xxxxxxxxxxxxxxxxxxxxx;.$&
&&$;;xxxxxxxxxxxxxxxxxxx:;$&&
&&&$X:;xxxxxxxxxxxxxxx;:X&&&&
&&&&&&$;:;++xxxxx++::+$&&&&&&
&&&&&&&&&&$$x+++x$$&&&&&&&&&&
    """
    
    UI.console.print(mascot, justify="center", style="bold cyan")
    UI.print()
    
    UI.console.print("[bold white]Veyllo Agent Framework (VAF)[/bold white]", justify="center")
    UI.console.print("[dim]Version 0.2.0[/dim]\n", justify="center")
    
    UI.console.print("[bold magenta]Created by Mert Can Elsner[/bold magenta]", justify="center")
    UI.console.print("[cyan]Veyllo Labs[/cyan]\n", justify="center")
    
    # System info
    from pathlib import Path
    try:
        from vaf.core.platform import Platform
        info = Platform.info()
        UI.console.print(f"[dim]Platform: {info['platform']} | Python: {info['python']} | Arch: {Platform.arch()}[/dim]", justify="center")
    except ImportError:
        UI.console.print(f"[dim]Platform: {sys.platform} | Python: {sys.version_info.major}.{sys.version_info.minor}[/dim]", justify="center")
    
    UI.print()
    
    panel_text = (
        "[bold]MIT License[/bold]\n\n"
        "Copyright (c) 2025 Mert Can Elsner / Veyllo Labs\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy "
        "of this software and associated documentation files (the \"Software\"), to deal "
        "in the Software without restriction, including without limitation the rights "
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell "
        "copies of the Software, and to permit persons to whom the Software is "
        "furnished to do so, subject to the following conditions:\n\n"
        "[bold yellow]The above copyright notice and this permission notice shall be included in all "
        "copies or substantial portions of the Software.[/bold yellow]\n\n"
        "See explicit [bold]LICENSE[/bold] file for full legal text."
    )
    
    from rich.panel import Panel
    from rich.align import Align
    
    UI.console.print(Align.center(Panel(panel_text, title="License Agreement", border_style="dim", width=70)))
    
    UI.console.input("\n[dim]Press Enter to return...[/dim]")


def main_menu(agent=None):
    while True:
        UI.clear()
        UI.print()
        
        # Dynamic Config Check
        persist = agent.config.get("persist_server", False) if agent else False
        p_label = "[ON]" if persist else "[OFF]"
        
        # Get current theme with color box
        try:
            from vaf.cli.themes import ThemeManager
            current_theme = ThemeManager.current()
            theme = ThemeManager.get_theme(current_theme)
            primary_color = theme.get("primary", "#00d4ff")
            theme_color_box = _get_color_box(primary_color)
            theme_label = f"{theme_color_box} Theme: {current_theme}"
        except ImportError:
            current_theme = "default"
            theme_label = f"Theme: {current_theme}"

        # UX toggles
        auto_links = bool(Config.get("ux_auto_open_links"))
        auto_outputs = bool(Config.get("ux_auto_open_outputs"))
        max_tabs = int(Config.get("ux_auto_open_max_tabs", 8) or 8)
        links_label = f"UX: Auto-open browser links [{'ON' if auto_links else 'OFF'}] (max {max_tabs})"
        outputs_label = f"UX: Auto-open output folders/files [{'ON' if auto_outputs else 'OFF'}]"
        
        # Get automation count
        try:
            from vaf.core.automation import AutomationManager
            auto_count = len(AutomationManager().list())
            auto_label = f'⚡ Automations ({auto_count})'
        except:
            auto_label = '⚡ Automations'
        
        # Get tools count
        try:
            tool_count = len(agent.tools) if agent and hasattr(agent, 'tools') else 0
            tools_label = f'🔧 Show All Tools ({tool_count})'
        except:
            tools_label = '🔧 Show All Tools'
        
        questions = [
            inquirer.List('action',
                          message="Settings Menu",
                          choices=[
                              ('Set Context Limit', 'context'),
                              ('Select Active Model', 'list'),
                              ('Search & Download New Models', 'search'),
                              ('─────────────────', None),
                              (theme_label, 'theme'),
                              (links_label, 'ux_links'),
                              (outputs_label, 'ux_outputs'),
                              (auto_label, 'automations'),
                              ('─────────────────', None),
                              (tools_label, 'tools'),
                              (f'Server Persistence {p_label}', 'persist'),
                              ('About', 'about'),
                              ('Exit Settings', 'exit'),
                          ],
            ),
        ]
        answers = inquirer.prompt(questions)
        if not answers:
            break
        
        action = answers['action']
        
        if action is None:  # Separator
            continue
        
        if action == 'persist':
            new_state = not persist
            agent.config["persist_server"] = new_state
            Config.save(agent.config)  # Config already imported at top of file
            UI.event("Settings", f"Server Persistence set to {new_state}. (Takes effect on exit)", style="success")
            time.sleep(1.5)
            
        if action == 'exit':
            break
        elif action == 'context':
            set_context_limit_menu()
        elif action == 'search':
            search_models_menu()
        elif action == 'list':
            select_model_menu()
        elif action == 'tools':
            show_tools_menu(agent)
        elif action == 'theme':
            show_theme_menu()
        elif action == 'automations':
            show_automations_menu()
        elif action == 'about':
            show_about()
        elif action == 'ux_links':
            # Toggle links
            new_val = not bool(Config.get("ux_auto_open_links"))
            Config.set("ux_auto_open_links", new_val)
            # Prompt for max tabs when enabling
            if new_val:
                try:
                    val = UI.console.input("[bold cyan]Max tabs to auto-open (1-20, default 8): [/bold cyan]").strip()
                    if val:
                        n = int(val)
                        n = max(1, min(n, 20))
                        Config.set("ux_auto_open_max_tabs", n)
                except Exception:
                    pass
            UI.event("Settings", f"Auto-open browser links set to {new_val}", style="success")
            time.sleep(1.0)
        elif action == 'ux_outputs':
            new_val = not bool(Config.get("ux_auto_open_outputs"))
            Config.set("ux_auto_open_outputs", new_val)
            UI.event("Settings", f"Auto-open outputs set to {new_val}", style="success")
            time.sleep(1.0)
