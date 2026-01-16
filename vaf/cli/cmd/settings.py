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
            # Search with more fields
            models = api.list_models(
                search=query, 
                filter="gguf", 
                limit=10, 
                sort="downloads", 
                direction=-1,
                full=True # Get full details like likes, created_at
            )
            
            # Prioritize exact match if query looks like "user/repo"
            results = list(models)
            exact_match = None
            if "/" in query:
                for i, m in enumerate(results):
                    if m.modelId.lower() == query.lower():
                        exact_match = results.pop(i)
                        break
                
                # If exact match found (or forced fetch needed), put it top
                if exact_match:
                    results.insert(0, exact_match)
                elif not results:
                    # Try fetching direct if search failed but it looks like an ID
                    try:
                        direct = api.model_info(repo_id=query)
                        # Check if it has GGUF
                        if any(f.rfilename.endswith(".gguf") for f in direct.siblings):
                            results.append(direct)
                    except: pass

            choices = []
            for m in results:
                # Format date (YYYY-MM)
                date_str = str(m.created_at)[:7] if hasattr(m, 'created_at') and m.created_at else "?"
                
                # Format metrics
                downloads = f"{m.downloads}"
                if m.downloads > 1000: downloads = f"{m.downloads/1000:.1f}k"
                if m.downloads > 1000000: downloads = f"{m.downloads/1000000:.1f}M"
                
                likes = f"{m.likes}"
                if m.likes > 1000: likes = f"{m.likes/1000:.1f}k"
                
                label = f"{m.modelId:<40} | ⬇ {downloads:<6} | ❤️ {likes:<5} | 📅 {date_str}"
                choices.append((label, m.modelId))
            
            if not choices:
                UI.error("No GGUF models found.")
                continue

            choices.append(("Cancel", None))

            UI.print("\n[bold]Select a model to download:[/bold]")
            for i, (label, value) in enumerate(choices, 1):
                UI.print(f"  [{i}] {label}")
            
            try:
                choice_str = UI.console.input("\n[bold cyan]Enter number: [/bold cyan]")
                if not choice_str.strip():
                    continue
                choice_idx = int(choice_str) - 1
                if 0 <= choice_idx < len(choices):
                    selected_model = choices[choice_idx][1]
                    if selected_model is None: # Cancel
                        continue
                else:
                    UI.error("Invalid selection.")
                    time.sleep(1)
                    continue
            except (ValueError, IndexError):
                UI.error("Invalid selection.")
                time.sleep(1)
                continue

            download_model_flow(selected_model)
            
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
    """Select model - supports both local and API providers"""
    current_provider = Config.get("provider", "local")
    
    UI.clear()
    
    # Check if using API provider
    if current_provider != "local":
        UI.panel(f"Select {current_provider.upper()} Model", style="highlight")
        _select_api_model(current_provider)
        UI.console.input("\n[dim]Press Enter to continue...[/dim]")
        return
    
    # Local model selection
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


def _select_api_model(provider: str):
    """Helper function to select model for API provider"""
    from vaf.core.api_backend import APIBackendManager
    
    UI.event("Loading", f"Fetching available {provider.upper()} models...", style="dim")
    models = APIBackendManager.get_available_models(provider)
    
    if models:
        # Add current model to list if not already there
        current_model = Config.get(f"api_model_{provider}")
        if current_model and current_model not in models:
            models.insert(0, f"{current_model} (current)")
        
        # Add "Keep current" and "Enter custom" options
        model_choices = models + ["Keep current", "Enter custom model ID"]
        
        model_q = [inquirer.List('model', 
                                 message=f"Select {provider.upper()} model ({len(models)} available)",
                                 choices=model_choices)]
        model_ans = inquirer.prompt(model_q)
        
        if model_ans:
            if model_ans['model'] == "Enter custom model ID":
                # Allow manual input for new/unlisted models
                custom = UI.console.input("[bold cyan]Enter custom model ID: [/bold cyan]")
                if custom.strip():
                    Config.set(f"api_model_{provider}", custom.strip())
                    UI.success(f"Model set to: {custom.strip()}")
            elif model_ans['model'] != "Keep current":
                # Remove "(current)" suffix if present
                model_id = model_ans['model'].replace(" (current)", "")
                Config.set(f"api_model_{provider}", model_id)
                UI.success(f"Model set to: {model_id}")
    else:
        UI.warning("Could not fetch models. Using default.")


def api_provider_menu():
    """Configure AI provider and API keys - Best Practice implementation"""
    UI.clear()
    
    current_provider = Config.get("provider", "local")
    
    # Display current status
    UI.panel(f"Current Provider: {current_provider.upper()}", title="🌐 AI Provider Settings", style="highlight")
    
    providers = [
        ("🖥️  Local (llama-server)", "local"),
        ("🤖 OpenAI (GPT-4, etc.)", "openai"),
        ("🧠 Anthropic (Claude)", "anthropic"),
        ("💫 DeepSeek", "deepseek"),
        ("✨ Google AI Studio (Gemini)", "google"),
        ("🌐 OpenRouter (Multi-provider)", "openrouter"),
        ("Back", "back"),
    ]
    
    questions = [inquirer.List('provider', message="Select AI Provider", choices=providers)]
    answers = inquirer.prompt(questions)
    
    if not answers or answers['provider'] == 'back':
        return
    
    selected = answers['provider']
    
    if selected == "local":
        Config.set("provider", "local")
        UI.success("✓ Provider set to: Local (llama-server)")
        
        # Ask about auto-start
        auto_start = Config.get("auto_start_local_server", True)
        toggle_q = [inquirer.Confirm('auto_start', 
                                      message=f"Auto-start llama-server on launch? (currently: {'ON' if auto_start else 'OFF'})",
                                      default=auto_start)]
        toggle_ans = inquirer.prompt(toggle_q)
        if toggle_ans:
            Config.set("auto_start_local_server", toggle_ans['auto_start'])
            UI.event("Settings", f"Auto-start set to: {toggle_ans['auto_start']}", style="info")
    else:
        # API Provider - check if key already exists
        current_key = Config.get_api_key(selected)
        masked_key = Config.mask_api_key(current_key)
        
        # If key exists, offer to change model or key
        if current_key:
            UI.print(f"\n[bold]Current API Key:[/bold] {masked_key}")
            UI.print(f"[bold]Current Provider:[/bold] {selected.upper()}\n")
            
            action_choices = [
                ("Change Model", "model"),
                ("Change API Key", "key"),
                ("Keep current settings", "keep"),
            ]
            
            action_q = [inquirer.List('action', message="What would you like to do?", choices=action_choices)]
            action_ans = inquirer.prompt(action_q)
            
            if not action_ans or action_ans['action'] == 'keep':
                UI.console.input("[dim]Press Enter to continue...[/dim]")
                return
            
            if action_ans['action'] == 'model':
                # Jump directly to model selection
                Config.set("provider", selected)  # Ensure provider is set
                _select_api_model(selected)
                UI.console.input("\n[dim]Press Enter to continue...[/dim]")
                return
            
            # If 'key' selected, continue to key input below
        
        # Prompt for new API key
        UI.print(f"\n[bold]Current API Key:[/bold] {masked_key}")
        UI.print(f"[dim]Keys are stored with Base64 encoding in ~/.vaf/config.json[/dim]\n")
        
        # Best Practice: Use getpass for sensitive input (better paste support)
        import getpass
        try:
            UI.print(f"[bold cyan]Enter {selected.upper()} API key (leave empty to keep current):[/bold cyan]")
            UI.print(f"[dim]Note: Input is hidden for security. Paste with Ctrl+V, then press Enter.[/dim]")
            key_input = getpass.getpass("API Key (input hidden): ")
            
            # Visual feedback: show how many characters were entered
            if key_input.strip():
                UI.event("Input", f"Received {len(key_input.strip())} characters", style="dim")
        except (EOFError, KeyboardInterrupt):
            UI.warning("\nInput cancelled.")
            UI.console.input("[dim]Press Enter to continue...[/dim]")
            return
        
        if key_input.strip():
            # Best Practice: Validate key format before saving
            if len(key_input.strip()) < 10:
                UI.error("API key seems too short. Please check and try again.")
                UI.console.input("[dim]Press Enter to continue...[/dim]")
                return
            
            Config.set_api_key(selected, key_input.strip())
            
            # Test connection (Best Practice)
            UI.event("Testing", "Verifying API key...", style="dim")
            try:
                from vaf.core.api_backend import APIBackendManager
                if APIBackendManager.test_connection(selected):
                    Config.set("provider", selected)
                    UI.success(f"✓ API key verified! Provider set to: {selected.upper()}")
                    UI.event("Important", "Please type 'r' or '/reload' to reload the agent with new provider", style="warning")
                    
                    # Offer to select model
                    _select_api_model(selected)
                else:
                    UI.error("✗ API key verification failed. Provider not changed.")
            except Exception as e:
                UI.error(f"Error testing API: {e}")
                UI.event("Settings", "API key saved, but verification failed. Check key and try again.", style="warning")
        else:
            if current_key:
                Config.set("provider", selected)
                UI.success(f"✓ Provider set to: {selected.upper()} (using existing key)")
            else:
                UI.error("No API key configured. Cannot switch to this provider.")
    
    UI.console.input("\n[dim]Press Enter to continue...[/dim]")


def select_api_model_menu():
    """Select API model for current provider (dynamically fetched)"""
    UI.clear()
    
    current_provider = Config.get("provider", "local")
    
    if current_provider == "local":
        UI.warning("You are using local provider. Switch to an API provider first.")
        UI.console.input("[dim]Press Enter to continue...[/dim]")
        return
    
    current_model = Config.get(f"api_model_{current_provider}")
    
    UI.panel(
        f"Provider: {current_provider.upper()}\nCurrent Model: {current_model}", 
        title="🤖 Select API Model", 
        style="highlight"
    )
    
    # Dynamically fetch models
    UI.print("\n[dim]Fetching available models from API...[/dim]")
    try:
        from vaf.core.api_backend import APIBackendManager
        models = APIBackendManager.get_available_models(current_provider)
        
        if not models:
            UI.error("Could not fetch models. Check your API key.")
            UI.console.input("[dim]Press Enter to continue...[/dim]")
            return
        
        UI.print(f"[success]Found {len(models)} models[/success]\n")
        
        # Mark current model
        choices = []
        for model in models:
            if model == current_model:
                choices.append((f"✓ {model} (current)", model))
            else:
                choices.append((model, model))
        
        choices.extend([
            ("─────────────────", None),
            ("Enter custom model ID", "custom"),
            ("Back", "back")
        ])
        
        questions = [inquirer.List('model', message="Select Model", choices=choices)]
        answers = inquirer.prompt(questions)
        
        if not answers or answers['model'] == 'back' or answers['model'] is None:
            return
        
        selected = answers['model']
        
        if selected == "custom":
            custom = UI.console.input("[bold cyan]Enter custom model ID: [/bold cyan]")
            if custom.strip():
                Config.set(f"api_model_{current_provider}", custom.strip())
                UI.success(f"✓ Model set to: {custom.strip()}")
        else:
            Config.set(f"api_model_{current_provider}", selected)
            UI.success(f"✓ Model set to: {selected}")
    
    except Exception as e:
        UI.error(f"Error fetching models: {e}")
    
    UI.console.input("\n[dim]Press Enter to continue...[/dim]")


def subagent_provider_menu():
    """Configure whether sub-agents use same or different provider"""
    UI.clear()
    
    main_provider = Config.get("provider", "local")
    subagent_provider = Config.get("subagent_provider", "inherit")
    use_separate = Config.get("subagent_use_separate_provider", False)
    
    display_provider = subagent_provider if subagent_provider != "inherit" else f"{main_provider} (inherited)"
    
    UI.panel(
        f"Main Agent: {main_provider.upper()}\nSub-Agent: {display_provider.upper()}", 
        title="🔧 Sub-Agent Provider Settings", 
        style="highlight"
    )
    
    UI.print("\n[dim]Sub-agents can use a different AI provider than the main agent.[/dim]")
    UI.print("[dim]Example: Main uses Claude (API), Sub-agents use Local (free)[/dim]\n")
    
    choices = [
        ("Inherit from Main Agent", "inherit"),
        ("─────────────────", None),
        ("Use Local (llama-server)", "local"),
        ("Use OpenAI", "openai"),
        ("Use Anthropic (Claude)", "anthropic"),
        ("Use DeepSeek", "deepseek"),
        ("Use Google AI Studio", "google"),
        ("Use OpenRouter", "openrouter"),
        ("─────────────────", None),
        ("Back", "back"),
    ]
    
    questions = [inquirer.List('provider', message="Select Sub-Agent Provider", choices=choices)]
    answers = inquirer.prompt(questions)
    
    if not answers or answers['provider'] == 'back' or answers['provider'] is None:
        return
    
    selected = answers['provider']
    
    # Validate API key if not local or inherit
    if selected not in ["local", "inherit"]:
        api_key = Config.get_api_key(selected)
        if not api_key:
            UI.error(f"No API key configured for {selected}. Please set it up in AI Provider Settings first.")
            UI.console.input("[dim]Press Enter to continue...[/dim]")
            return
    
    Config.set("subagent_provider", selected)
    Config.set("subagent_use_separate_provider", selected != "inherit")
    
    if selected == "inherit":
        UI.success(f"✓ Sub-agents will use the same provider as main agent ({main_provider})")
    else:
        UI.success(f"✓ Sub-agents will use: {selected.upper()}")
    
    UI.console.input("\n[dim]Press Enter to continue...[/dim]")


def voice_settings_menu():
    """Configure Voice / STT / Wake Word settings."""
    
    # Show loading event because openwakeword import can be slow (Model/ONNX init)
    UI.event("Loading", "Initializing voice modules...", style="dim")
    
    # Check openWakeWord availability ONCE (outside loop)
    try:
        import openwakeword
        has_wake_word = True
    except ImportError:
        has_wake_word = False

    while True:
        UI.clear()
        
        stt_enabled = bool(Config.get("speech_stt_enabled", False))
        wake_word_enabled = bool(Config.get("stt_wake_word_enabled", False))
        current_wake_word = Config.get("stt_wake_word", "hey_jarvis")
        tts_engine = Config.get("speech_tts_engine", "piper")  # "piper" or "system"

        UI.panel(f"STT: {'ON' if stt_enabled else 'OFF'} | Wake Word: {'ON' if wake_word_enabled else 'OFF'} ({current_wake_word}) | TTS: {tts_engine.upper()}",
                 title="🎤 Voice Settings (100% Local & Free)", style="highlight")

        choices = [
            (f"🔊 TTS Engine: {tts_engine.upper()} ({'Neural' if tts_engine == 'piper' else 'Native'})", 'tts_engine'),
            ("─────────────────", None),
            (f"Speech-to-Text (STT) [{'ON' if stt_enabled else 'OFF'}]", 'toggle_stt'),
            ("Select Microphone", 'mic'),
            ("Select Input Language", 'lang'),
            ("─────────────────", None),
        ]

        if has_wake_word:
            choices.extend([
                (f"Wake Word Detection (Auto Mode) [{'ON' if wake_word_enabled else 'OFF'}]", 'toggle_wake'),
                (f"Select Wake Word (Current: {current_wake_word})", 'select_wake'),
            ])
        else:
            choices.append(("[dim]Wake Word (Install openwakeword first)[/dim]", None))
            
        choices.extend([
            ("─────────────────", None),
            ("Back", "back")
        ])
        
        questions = [inquirer.List('action', message="Voice Options", choices=choices)]
        answers = inquirer.prompt(questions)
        
        if not answers or answers['action'] == 'back':
            break
            
        action = answers['action']
        
        if action == 'tts_engine':
            # Toggle between piper and system
            new_engine = "system" if tts_engine == "piper" else "piper"
            Config.set("speech_tts_engine", new_engine)
            engine_name = "Neural (Piper)" if new_engine == "piper" else "Native (System)"
            UI.event("Settings", f"TTS Engine set to: {engine_name}", style="success")
            time.sleep(1.0)
        
        elif action == 'toggle_stt':
            new_val = not stt_enabled
            Config.set("speech_stt_enabled", new_val)
            UI.event("Settings", f"STT {'enabled' if new_val else 'disabled'}", style="success")
            
        elif action == 'mic':
            try:
                from vaf.core.speech import get_speech_manager
                mics = get_speech_manager().list_microphones()
                if not mics:
                    UI.error("No microphones found.")
                else:
                    mic_choices = [(f"{i}: {name}", i) for i, name in enumerate(mics)]
                    mic_choices.append(("Cancel", None))
                    q = [inquirer.List('mic', message="Select Microphone", choices=mic_choices)]
                    ans = inquirer.prompt(q)
                    if ans and ans['mic'] is not None:
                        # ans['mic'] is already the index (from the tuple value)
                        idx = ans['mic']
                        get_speech_manager().set_microphone(idx)
                        UI.success(f"Microphone set to index {idx}")
            except Exception as e:
                UI.error(f"Error: {e}")
                
        elif action == 'lang':
            lang_choices = [
                ("English (US)", "en-US"), ("German (DE)", "de-DE"), ("Turkish (TR)", "tr-TR"),
                ("French (FR)", "fr-FR"), ("Spanish (ES)", "es-ES"), ("Chinese (CN)", "zh-CN"),
                ("Russian (RU)", "ru-RU"), ("Italian (IT)", "it-IT"), ("Cancel", None)
            ]
            q = [inquirer.List('lang', message="Select Input Language", choices=lang_choices)]
            ans = inquirer.prompt(q)
            if ans and ans['lang']:
                Config.set("speech_language", ans['lang'])
                UI.success(f"Language set to: {ans['lang']}")
                
        elif action == 'toggle_wake':
            new_val = not wake_word_enabled
            Config.set("stt_wake_word_enabled", new_val)
            UI.event("Settings", f"Wake Word {'enabled' if new_val else 'disabled'}", style="success")

        elif action == 'select_wake':
            # Available openWakeWord models (free & local)
            from vaf.core.speech import WakeWordManager
            keywords = WakeWordManager.get_instance().get_available_models()
            q = [inquirer.List('kw', message="Select Wake Word Model", choices=keywords)]
            ans = inquirer.prompt(q)
            if ans and ans['kw']:
                Config.set("stt_wake_word", ans['kw'])
                UI.success(f"Wake Word set to: {ans['kw']}")
            
        time.sleep(0.5)


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
        separate_terminals = bool(Config.get("sub_agents_in_separate_terminals", True))
        timeout_enabled = bool(Config.get("subagent_timeout_enabled", True))
        timeout_minutes = int(Config.get("subagent_timeout_minutes", 120))

        # UX labels
        links_label = f"🔗 Auto-Open Links [{'ON' if auto_links else 'OFF'}]"
        outputs_label = f"📄 Auto-Open Outputs [{'ON' if auto_outputs else 'OFF'}] (max {max_tabs})"
        terminals_label = f"💻 Separate Terminals [{'ON' if separate_terminals else 'OFF'}]"
        timeout_label = f"⏱️ Sub-Agent Timeout [{'ON' if timeout_enabled else 'OFF'}] ({timeout_minutes}m)"

        # Speech toggles
        tts_enabled = bool(Config.get("speech_tts_enabled", False))
        tts_label = f"🔊 Speech Output (TTS) [{'ON' if tts_enabled else 'OFF'}]"

        voice_label = "🎤 Voice / STT / Wake Word Settings"
        
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
        
        # Get current provider
        current_provider = Config.get("provider", "local")
        provider_label = f"🌐 AI Provider: {current_provider.upper()}"
        
        subagent_provider = Config.get("subagent_provider", "inherit")
        if subagent_provider == "inherit":
            subagent_label = f"🔧 Sub-Agent Provider: {current_provider.upper()} (inherited)"
        else:
            subagent_label = f"🔧 Sub-Agent Provider: {subagent_provider.upper()}"
        
        # Get current API model if using API
        if current_provider != "local":
            current_api_model = Config.get(f"api_model_{current_provider}", "not set")
            api_model_label = f"🤖 API Model: {current_api_model}"
        else:
            api_model_label = None
        
        menu_choices = [
            (provider_label, 'provider'),
            (subagent_label, 'subagent_provider'),
        ]
        
        # Add API model selector if using API
        if api_model_label:
            menu_choices.append((api_model_label, 'api_model'))
        
        menu_choices.extend([
            ('─────────────────', None),
            ('Set Context Limit', 'context'),
            ('Select Active Model', 'list'),
            ('Search & Download New Models', 'search'),
            ('─────────────────', None),
            (theme_label, 'theme'),
            (links_label, 'ux_links'),
            (outputs_label, 'ux_outputs'),
            (terminals_label, 'separate_terminals'),
            (timeout_label, 'subagent_timeout'),
            (tts_label, 'speech_tts'),
            (voice_label, 'voice_menu'),
        ])
            
        menu_choices.extend([
            (auto_label, 'automations'),
            ('─────────────────', None),
            (tools_label, 'tools'),
            (f'Server Persistence {p_label}', 'persist'),
            ('About', 'about'),
            ('Exit Settings', 'exit'),
        ])
        
        questions = [
            inquirer.List('action',
                          message="Settings Menu",
                          choices=menu_choices,
            ),
        ]
        answers = inquirer.prompt(questions)
        if not answers:
            break
        
        action = answers['action']
        
        if action is None:  # Separator
            continue
        
        if action == 'voice_menu':
            voice_settings_menu()
        
        if action == 'speech_tts':
            new_val = not tts_enabled
            Config.set("speech_tts_enabled", new_val)
            status = "enabled" if new_val else "disabled"
            UI.event("Settings", f"Text-to-Speech (TTS) {status}", style="success")
            # Initialize engine if enabled
            if new_val:
                try:
                    from vaf.core.speech import get_speech_manager
                    get_speech_manager().speak("Speech output enabled.")
                except ImportError:
                    UI.warning("Speech libraries not installed. Run: pip install pyttsx3")
            time.sleep(1.0)
            
        if action == 'provider':
            api_provider_menu()
        elif action == 'api_model':
            select_api_model_menu()
        elif action == 'subagent_provider':
            subagent_provider_menu()
        elif action == 'persist':
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
        elif action == 'separate_terminals':
            new_val = not bool(Config.get("sub_agents_in_separate_terminals", False))
            Config.set("sub_agents_in_separate_terminals", new_val)
            status = "enabled" if new_val else "disabled"
            UI.event("Settings", f"Sub-agents in separate terminals {status}", style="success")
            time.sleep(1.0)
        elif action == 'subagent_timeout':
            # Show submenu for timeout configuration
            current_enabled = bool(Config.get("subagent_timeout_enabled", True))
            current_minutes = int(Config.get("subagent_timeout_minutes", 120))
            
            timeout_choices = [
                (f"Toggle timeout [{'ON' if current_enabled else 'OFF'}]", 'toggle'),
                (f"Set timeout duration (current: {current_minutes} min)", 'duration'),
                ('Back', 'back'),
            ]
            
            q = [inquirer.List('timeout_action', message="Sub-Agent Timeout Settings", choices=timeout_choices)]
            ans = inquirer.prompt(q)
            
            if ans and ans['timeout_action'] == 'toggle':
                new_val = not current_enabled
                Config.set("subagent_timeout_enabled", new_val)
                status = f"enabled ({current_minutes} min)" if new_val else "disabled (no timeout)"
                UI.event("Settings", f"Sub-agent timeout {status}", style="success")
                time.sleep(1.0)
            elif ans and ans['timeout_action'] == 'duration':
                try:
                    val = UI.console.input("[bold cyan]Timeout in minutes (1-480, 0 = disable): [/bold cyan]").strip()
                    if val:
                        n = int(val)
                        if n == 0:
                            Config.set("subagent_timeout_enabled", False)
                            UI.event("Settings", "Sub-agent timeout disabled", style="success")
                        else:
                            n = max(1, min(n, 480))  # 1 min to 8 hours
                            Config.set("subagent_timeout_minutes", n)
                            Config.set("subagent_timeout_enabled", True)
                            UI.event("Settings", f"Sub-agent timeout set to {n} minutes", style="success")
                except Exception:
                    UI.error("Invalid input")
            time.sleep(1.0)
