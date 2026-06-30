# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Automate - Automation (CI/CD-like)
Automate tests, builds, linting
"""
import typer
import subprocess
import os
import sys
import time
from pathlib import Path
from typing import Optional
from vaf.cli.ui import UI
from vaf.core.project_config import ProjectConfig

app = typer.Typer()

# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def detect_package_manager() -> Optional[str]:
    """Detect the package manager."""
    cwd = Path.cwd()
    
    if (cwd / "package-lock.json").exists():
        return "npm"
    if (cwd / "yarn.lock").exists():
        return "yarn"
    if (cwd / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (cwd / "bun.lockb").exists():
        return "bun"
    if (cwd / "package.json").exists():
        return "npm"  # Default
    if (cwd / "requirements.txt").exists() or (cwd / "pyproject.toml").exists():
        return "pip"
    if (cwd / "Cargo.toml").exists():
        return "cargo"
    if (cwd / "go.mod").exists():
        return "go"
    
    return None

def run_command(cmd: str, cwd: str = ".") -> tuple[int, str, str]:
    """Execute a command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout: Command took too long"
    except Exception as e:
        return -1, "", str(e)

# ═══════════════════════════════════════════════════════════════════════════════
# AUTOMATE COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@app.command("test")
def run_tests(
    watch: bool = typer.Option(False, "--watch", "-w", help="Watch mode"),
    coverage: bool = typer.Option(False, "--coverage", "-c", help="Coverage report"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Run tests in the project.
    
    Examples:
        vaf automate test
        vaf automate test --coverage
        vaf automate test --watch
    """
    config = ProjectConfig.load(path)
    
    # Determine test command
    test_cmd = config.get("test_command")
    
    if not test_cmd:
        test_cmd = ProjectConfig.detect_test_command(path)
    
    if not test_cmd:
        # Fallback based on detected language
        language = config.get("language", "unknown")
        
        test_commands = {
            "python": "pytest --cov" if coverage else "pytest",
            "javascript": "npm test",
            "typescript": "npm test",
            "rust": "cargo test",
            "go": "go test ./...",
        }
        
        test_cmd = test_commands.get(language)
    
    if not test_cmd:
        UI.error("Could not determine test command.")
        UI.print("[dim]Set 'test_command' in vaf.config.json[/dim]")
        raise typer.Exit(1)
    
    # Add coverage
    if coverage:
        if "pytest" in test_cmd:
            test_cmd += " --cov"
        elif "npm" in test_cmd or "jest" in test_cmd:
            test_cmd += " -- --coverage"
        elif "cargo" in test_cmd:
            UI.print("[dim]Tip: Use 'cargo tarpaulin' for coverage[/dim]")
    
    # Watch mode
    if watch:
        if "pytest" in test_cmd:
            test_cmd = "pytest-watch"
        elif "npm" in test_cmd:
            test_cmd = test_cmd.replace("npm test", "npm test -- --watch")
        elif "cargo" in test_cmd:
            test_cmd = "cargo watch -x test"
    
    UI.event("Test", f"Running: {test_cmd}", style="cyan")
    UI.print()
    
    start_time = time.time()
    exit_code, stdout, stderr = run_command(test_cmd, path)
    elapsed = time.time() - start_time
    
    # Show output
    if stdout:
        UI.console.print(stdout)
    if stderr:
        UI.console.print(stderr, style="red" if exit_code != 0 else "dim")
    
    UI.print()
    if exit_code == 0:
        UI.success(f"Tests passed ({elapsed:.1f}s)")
    else:
        UI.error(f"Tests failed (Exit: {exit_code}, Time: {elapsed:.1f}s)")
    
    raise typer.Exit(exit_code)

@app.command("build")
def run_build(
    release: bool = typer.Option(False, "--release", "-r", help="Release/Production build"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Run the build process.
    
    Examples:
        vaf automate build
        vaf automate build --release
    """
    config = ProjectConfig.load(path)
    
    build_cmd = config.get("build_command")
    
    if not build_cmd:
        build_cmd = ProjectConfig.detect_build_command(path)
    
    if not build_cmd:
        UI.error("Could not determine build command.")
        UI.print("[dim]Set 'build_command' in vaf.config.json[/dim]")
        raise typer.Exit(1)
    
    # Release mode
    if release:
        if "cargo" in build_cmd:
            build_cmd = build_cmd.replace("cargo build", "cargo build --release")
        elif "npm" in build_cmd:
            # NODE_ENV=production
            build_cmd = f"NODE_ENV=production {build_cmd}"
    
    UI.event("Build", f"Running: {build_cmd}", style="cyan")
    UI.print()
    
    start_time = time.time()
    exit_code, stdout, stderr = run_command(build_cmd, path)
    elapsed = time.time() - start_time
    
    if stdout:
        UI.console.print(stdout)
    if stderr and exit_code != 0:
        UI.console.print(stderr, style="red")
    
    UI.print()
    if exit_code == 0:
        UI.success(f"Build successful ({elapsed:.1f}s)")
    else:
        UI.error(f"Build failed (Exit: {exit_code})")
    
    raise typer.Exit(exit_code)

@app.command("lint")
def run_lint(
    fix: bool = typer.Option(False, "--fix", "-f", help="Auto-fix issues"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Run linting.
    
    Examples:
        vaf automate lint
        vaf automate lint --fix
    """
    config = ProjectConfig.load(path)
    language = config.get("language", "unknown")
    
    lint_commands = {
        "python": "ruff check . --fix" if fix else "ruff check .",
        "javascript": "eslint ." if not fix else "eslint . --fix",
        "typescript": "eslint ." if not fix else "eslint . --fix",
        "rust": "cargo clippy" if not fix else "cargo clippy --fix",
        "go": "golangci-lint run" if not fix else "golangci-lint run --fix",
    }
    
    # Fallback for Python without ruff
    if language == "python":
        # Check if ruff is available
        check = subprocess.run("ruff --version", shell=True, capture_output=True)
        if check.returncode != 0:
            lint_commands["python"] = "flake8 ." if not fix else "black . && flake8 ."
    
    lint_cmd = lint_commands.get(language)
    
    if not lint_cmd:
        UI.error(f"No linter configured for {language}.")
        raise typer.Exit(1)
    
    UI.event("Lint", f"Running: {lint_cmd}", style="cyan")
    UI.print()
    
    exit_code, stdout, stderr = run_command(lint_cmd, path)
    
    if stdout:
        UI.console.print(stdout)
    if stderr:
        UI.console.print(stderr, style="yellow" if exit_code == 0 else "red")
    
    if exit_code == 0:
        UI.success("Linting passed")
    else:
        UI.error("Linting found issues")
    
    raise typer.Exit(exit_code)

@app.command("format")
def run_format(
    check: bool = typer.Option(False, "--check", "-c", help="Check only, don't modify"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Format code.
    
    Examples:
        vaf automate format
        vaf automate format --check
    """
    config = ProjectConfig.load(path)
    language = config.get("language", "unknown")
    
    format_commands = {
        "python": "ruff format . --check" if check else "ruff format .",
        "javascript": "prettier --check ." if check else "prettier --write .",
        "typescript": "prettier --check ." if check else "prettier --write .",
        "rust": "cargo fmt --check" if check else "cargo fmt",
        "go": "gofmt -d ." if check else "gofmt -w .",
    }
    
    format_cmd = format_commands.get(language)
    
    if not format_cmd:
        UI.error(f"No formatter configured for {language}.")
        raise typer.Exit(1)
    
    UI.event("Format", f"Running: {format_cmd}", style="cyan")
    UI.print()
    
    exit_code, stdout, stderr = run_command(format_cmd, path)
    
    if stdout:
        UI.console.print(stdout)
    if stderr:
        UI.console.print(stderr, style="yellow")
    
    if exit_code == 0:
        if check:
            UI.success("Code is properly formatted")
        else:
            UI.success("Code formatted")
    else:
        UI.error("Formatting failed or changes needed")
    
    raise typer.Exit(exit_code)

@app.command("install")
def run_install(
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Install project dependencies.
    
    Examples:
        vaf automate install
    """
    pm = detect_package_manager()
    
    if not pm:
        UI.error("Could not detect package manager.")
        raise typer.Exit(1)
    
    install_commands = {
        "npm": "npm install",
        "yarn": "yarn install",
        "pnpm": "pnpm install",
        "bun": "bun install",
        "pip": "pip install -r requirements.txt",
        "cargo": "cargo build",
        "go": "go mod download",
    }
    
    install_cmd = install_commands.get(pm)
    
    UI.event("Install", f"Running: {install_cmd}", style="cyan")
    UI.print()
    
    exit_code, stdout, stderr = run_command(install_cmd, path)
    
    if stdout:
        UI.console.print(stdout)
    if stderr and exit_code != 0:
        UI.console.print(stderr, style="red")
    
    if exit_code == 0:
        UI.success("Dependencies installed")
    else:
        UI.error("Installation failed")
    
    raise typer.Exit(exit_code)

@app.command("check")
def run_check(
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Run all checks (lint, format-check, test).
    
    Examples:
        vaf automate check
    """
    UI.panel("VAF Check - Full Project Check", style="cyan")
    
    results = []
    
    # 1. Format Check
    UI.event("Step 1", "Format Check...", style="dim")
    try:
        ctx = typer.Context(run_format)
        run_format(check=True, path=path)
        results.append(("Format", True))
    except SystemExit as e:
        results.append(("Format", e.code == 0))
    
    # 2. Lint
    UI.event("Step 2", "Linting...", style="dim")
    try:
        run_lint(fix=False, path=path)
        results.append(("Lint", True))
    except SystemExit as e:
        results.append(("Lint", e.code == 0))
    
    # 3. Tests
    UI.event("Step 3", "Tests...", style="dim")
    try:
        run_tests(watch=False, coverage=False, path=path)
        results.append(("Tests", True))
    except SystemExit as e:
        results.append(("Tests", e.code == 0))
    
    # Summary
    UI.print()
    UI.panel("Results", style="cyan")
    
    all_passed = True
    for name, passed in results:
        status = "[green]✓[/green]" if passed else "[red]✗[/red]"
        UI.print(f"  {status} {name}")
        if not passed:
            all_passed = False
    
    UI.print()
    if all_passed:
        UI.success("All checks passed!")
    else:
        UI.error("Some checks failed.")
    
    raise typer.Exit(0 if all_passed else 1)

@app.callback(invoke_without_command=True)
def automate_callback(ctx: typer.Context):
    """
    VAF Automate - Development automation.
    """
    if ctx.invoked_subcommand is None:
        UI.panel("VAF Automate - Automation", style="cyan")
        UI.print("  [bold]test[/bold]     - Run tests")
        UI.print("  [bold]build[/bold]    - Build project")
        UI.print("  [bold]lint[/bold]     - Code linting")
        UI.print("  [bold]format[/bold]   - Format code")
        UI.print("  [bold]install[/bold]  - Install dependencies")
        UI.print("  [bold]check[/bold]    - Run all checks")
        UI.print()
        UI.print("[dim]Example: vaf automate test --coverage[/dim]")
