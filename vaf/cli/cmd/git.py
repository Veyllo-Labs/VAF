"""
VAF Git - Git Integration with AI Support
Automated Git operations with intelligent commit messages
"""
import typer
import subprocess
import os
import requests
from pathlib import Path
from typing import Optional, List
from vaf.cli.ui import UI

app = typer.Typer()

# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def run_git(args: List[str], cwd: str = ".") -> tuple[int, str, str]:
    """Execute a git command."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", "Git is not installed."
    except Exception as e:
        return -1, "", str(e)

def is_git_repo(path: str = ".") -> bool:
    """Check if a directory is a git repository."""
    code, _, _ = run_git(["rev-parse", "--is-inside-work-tree"], path)
    return code == 0

def get_staged_diff() -> str:
    """Get the diff of staged changes."""
    _, stdout, _ = run_git(["diff", "--cached", "--no-color"])
    return stdout

def get_unstaged_diff() -> str:
    """Get the diff of unstaged changes."""
    _, stdout, _ = run_git(["diff", "--no-color"])
    return stdout

def get_changed_files() -> tuple[List[str], List[str], List[str]]:
    """Return lists of (staged, unstaged, untracked) files."""
    staged = []
    unstaged = []
    untracked = []
    
    _, stdout, _ = run_git(["status", "--porcelain"])
    
    for line in stdout.splitlines():
        if len(line) < 3:
            continue
        
        index_status = line[0]
        worktree_status = line[1]
        filename = line[3:]
        
        if index_status == "?":
            untracked.append(filename)
        elif index_status != " ":
            staged.append(filename)
        elif worktree_status != " ":
            unstaged.append(filename)
    
    return staged, unstaged, untracked

def call_local_llm(prompt: str, temperature: float = 0.3) -> str:
    """Call the local LLM via VAF server."""
    try:
        response = requests.post(
            "http://127.0.0.1:8080/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 500,
                "stream": False
            },
            timeout=60
        )
        
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return None
    except:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# GIT COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@app.command("init")
def git_init(
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Initialize a new git repository.
    
    Examples:
        vaf git init
        vaf git init --path ~/projects/my-app
    """
    target = Path(path).resolve()
    
    if is_git_repo(str(target)):
        UI.error(f"Already a git repository: {target}")
        raise typer.Exit(1)
    
    UI.event("Git", f"Initializing repository in {target}", style="cyan")
    
    code, stdout, stderr = run_git(["init"], str(target))
    
    if code != 0:
        UI.error(f"Git init failed: {stderr}")
        raise typer.Exit(1)
    
    UI.success("Git repository initialized!")
    
    # Create .gitignore if not present
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        UI.event("Create", ".gitignore created", style="dim")
        gitignore.write_text('''# IDE
.idea/
.vscode/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Dependencies
node_modules/
__pycache__/
venv/
.env

# Build
dist/
build/
*.egg-info/
target/

# Logs
*.log
logs/
''')
    
    UI.print()
    UI.print("[bold]Next steps:[/bold]")
    UI.print("  git add .")
    UI.print("  vaf git commit")

@app.command("status")
def git_status():
    """
    Show git status with colors.
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    staged, unstaged, untracked = get_changed_files()
    
    # Branch
    _, branch, _ = run_git(["branch", "--show-current"])
    UI.event("Branch", branch.strip() or "HEAD detached", style="cyan")
    UI.print()
    
    if staged:
        UI.print("[bold green]Staged for commit:[/bold green]")
        for f in staged:
            UI.print(f"  [green]✓[/green] {f}")
        UI.print()
    
    if unstaged:
        UI.print("[bold yellow]Modified (not staged):[/bold yellow]")
        for f in unstaged:
            UI.print(f"  [yellow]~[/yellow] {f}")
        UI.print()
    
    if untracked:
        UI.print("[bold red]Untracked:[/bold red]")
        for f in untracked:
            UI.print(f"  [red]?[/red] {f}")
        UI.print()
    
    if not staged and not unstaged and not untracked:
        UI.success("Working tree is clean.")

@app.command("add")
def git_add(
    files: List[str] = typer.Argument(None, help="Files to stage"),
    all_files: bool = typer.Option(False, "--all", "-A", help="Stage all changes")
):
    """
    Stage files for the next commit.
    
    Examples:
        vaf git add main.py
        vaf git add --all
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    if all_files:
        args = ["add", "-A"]
    elif files:
        args = ["add"] + list(files)
    else:
        UI.error("No files specified. Use --all for all changes.")
        raise typer.Exit(1)
    
    code, _, stderr = run_git(args)
    
    if code != 0:
        UI.error(f"Git add failed: {stderr}")
        raise typer.Exit(1)
    
    # Show what was staged
    staged, _, _ = get_changed_files()
    
    if staged:
        UI.success(f"{len(staged)} file(s) staged:")
        for f in staged[:10]:
            UI.print(f"  [green]✓[/green] {f}")
        if len(staged) > 10:
            UI.print(f"  ... and {len(staged) - 10} more")
    else:
        UI.print("[dim]No changes to stage.[/dim]")

@app.command("commit")
def git_commit(
    message: str = typer.Option(None, "--message", "-m", help="Commit message"),
    auto: bool = typer.Option(False, "--auto", "-a", help="AI generates message")
):
    """
    Create a commit. With --auto, the message is automatically generated.
    
    Examples:
        vaf git commit -m "Fix login bug"
        vaf git commit --auto
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    staged, _, _ = get_changed_files()
    
    if not staged:
        UI.error("No staged changes. Run 'vaf git add' first.")
        raise typer.Exit(1)
    
    # Determine message
    final_message = message
    
    # Priority: explicit message > auto-generate
    # Only use AI if: auto flag is set AND no explicit message provided
    if auto and not message:
        UI.event("Git", "Generating commit message with AI...", style="cyan")
        
        # Get diff
        diff = get_staged_diff()
        
        if diff:
            prompt = f'''Analyze this git diff and create a precise commit message.

**Diff:**
```diff
{diff[:3000]}
```

**Rules:**
1. First line: Short summary (max 50 characters)
2. Imperative form ("Add", "Fix", "Update", not "Added", "Fixed")
3. Optional: Empty line + details
4. Language: English

**Output:** ONLY the commit message, no explanation.
'''
            
            with UI.console.status("[bold cyan]AI is analyzing changes...[/bold cyan]", spinner="dots"):
                ai_message = call_local_llm(prompt)
            
            if ai_message:
                # Clean up
                final_message = ai_message.strip().strip('"').strip("'")
                
                # Show suggestion
                UI.print()
                UI.print("[bold]Suggested commit message:[/bold]")
                UI.console.print(f"  {final_message}", style="cyan")
                UI.print()
                
                if not message:  # Only confirm if not --auto
                    confirm = typer.confirm("Use this message?", default=True)
                    if not confirm:
                        final_message = typer.prompt("Enter your message")
            else:
                UI.print("[dim]AI not available, please enter message manually.[/dim]")
    
    if not final_message:
        final_message = typer.prompt("Commit message")
    
    if not final_message:
        UI.error("No commit message provided.")
        raise typer.Exit(1)
    
    # Perform commit
    code, stdout, stderr = run_git(["commit", "-m", final_message])
    
    if code != 0:
        UI.error(f"Commit failed: {stderr}")
        raise typer.Exit(1)
    
    UI.success(f"Commit created: {final_message[:50]}...")
    
    # Show hash
    _, hash_output, _ = run_git(["rev-parse", "--short", "HEAD"])
    UI.print(f"[dim]Commit: {hash_output.strip()}[/dim]")

@app.command("log")
def git_log(
    count: int = typer.Option(10, "--count", "-n", help="Number of commits")
):
    """
    Show commit history.
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    code, stdout, stderr = run_git([
        "log",
        f"-{count}",
        "--pretty=format:%h|%s|%an|%ar",
        "--no-color"
    ])
    
    if code != 0:
        UI.error(f"Git log failed: {stderr}")
        raise typer.Exit(1)
    
    UI.panel(f"Last {count} Commits", style="cyan")
    
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            hash_str, message, author, date = parts[0], parts[1], parts[2], parts[3]
            UI.print(f"[cyan]{hash_str}[/cyan] {message[:60]}")
            UI.print(f"        [dim]{author} • {date}[/dim]")
            UI.print()

@app.command("diff")
def git_diff(
    staged: bool = typer.Option(False, "--staged", "-s", help="Only staged changes")
):
    """
    Show changes.
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    
    code, stdout, stderr = run_git(args)
    
    if not stdout:
        UI.print("[dim]No changes.[/dim]")
        return
    
    # Syntax highlighting for diff
    for line in stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            UI.console.print(line, style="green")
        elif line.startswith("-") and not line.startswith("---"):
            UI.console.print(line, style="red")
        elif line.startswith("@@"):
            UI.console.print(line, style="cyan")
        elif line.startswith("diff") or line.startswith("index"):
            UI.console.print(line, style="bold")
        else:
            UI.console.print(line)

@app.command("push")
def git_push(
    force: bool = typer.Option(False, "--force", "-f", help="Force push"),
    set_upstream: bool = typer.Option(False, "--set-upstream", "-u", help="Set upstream")
):
    """
    Push commits to remote.
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    args = ["push"]
    
    if set_upstream:
        _, branch, _ = run_git(["branch", "--show-current"])
        args.extend(["-u", "origin", branch.strip()])
    
    if force:
        args.append("--force")
        UI.print("[yellow]⚠ Force push will be executed![/yellow]")
    
    UI.event("Git", "Pushing...", style="cyan")
    
    code, stdout, stderr = run_git(args)
    
    if code != 0:
        UI.error(f"Push failed: {stderr}")
        raise typer.Exit(1)
    
    UI.success("Push successful!")
    if stdout:
        UI.console.print(stdout, style="dim")

@app.command("pull")
def git_pull():
    """
    Fetch and merge changes from remote.
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    UI.event("Git", "Pulling...", style="cyan")
    
    code, stdout, stderr = run_git(["pull"])
    
    if code != 0:
        UI.error(f"Pull failed: {stderr}")
        raise typer.Exit(1)
    
    UI.success("Pull successful!")
    if stdout:
        UI.console.print(stdout, style="dim")

@app.command("branch")
def git_branch(
    name: str = typer.Argument(None, help="New branch name"),
    delete: bool = typer.Option(False, "--delete", "-d", help="Delete branch"),
    list_all: bool = typer.Option(False, "--list", "-l", help="List all branches")
):
    """
    Branch operations.
    
    Examples:
        vaf git branch               # Current branch
        vaf git branch feature/login # New branch
        vaf git branch --list        # List all
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    if list_all:
        code, stdout, _ = run_git(["branch", "-a"])
        UI.panel("Branches", style="cyan")
        for line in stdout.splitlines():
            if line.startswith("*"):
                UI.print(f"[bold cyan]{line}[/bold cyan]")
            else:
                UI.print(f"  {line.strip()}")
        return
    
    if delete and name:
        code, _, stderr = run_git(["branch", "-d", name])
        if code == 0:
            UI.success(f"Branch '{name}' deleted.")
        else:
            UI.error(f"Delete failed: {stderr}")
        return
    
    if name:
        # Create new branch and switch
        code, _, stderr = run_git(["checkout", "-b", name])
        if code == 0:
            UI.success(f"Branch '{name}' created and switched.")
        else:
            UI.error(f"Branch creation failed: {stderr}")
        return
    
    # Current branch
    _, branch, _ = run_git(["branch", "--show-current"])
    UI.event("Branch", branch.strip(), style="cyan")

@app.command("checkout")
def git_checkout(
    target: str = typer.Argument(..., help="Branch or commit")
):
    """
    Switch to a branch or commit.
    """
    if not is_git_repo():
        UI.error("Not a git repository.")
        raise typer.Exit(1)
    
    code, stdout, stderr = run_git(["checkout", target])
    
    if code != 0:
        UI.error(f"Checkout failed: {stderr}")
        raise typer.Exit(1)
    
    UI.success(f"Switched to: {target}")

@app.callback(invoke_without_command=True)
def git_callback(ctx: typer.Context):
    """
    VAF Git - Git integration with AI support.
    """
    if ctx.invoked_subcommand is None:
        UI.panel("VAF Git - Git with AI", style="cyan")
        UI.print("  [bold]init[/bold]      - Initialize repository")
        UI.print("  [bold]status[/bold]    - Show status")
        UI.print("  [bold]add[/bold]       - Stage files")
        UI.print("  [bold]commit[/bold]    - Create commit (--auto for AI message)")
        UI.print("  [bold]push[/bold]      - Push to remote")
        UI.print("  [bold]pull[/bold]      - Pull from remote")
        UI.print("  [bold]branch[/bold]    - Branch operations")
        UI.print("  [bold]checkout[/bold]  - Switch branch")
        UI.print("  [bold]log[/bold]       - Show history")
        UI.print("  [bold]diff[/bold]      - Show changes")
        UI.print()
        UI.print("[dim]Example: vaf git commit --auto[/dim]")